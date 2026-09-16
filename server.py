import json
import os
import secrets
import uuid
import asyncio
from urllib.parse import urlencode
from dotenv import load_dotenv

load_dotenv()

import httpx
import spotipy
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from yandex_music import Client as YMClient

from matcher import normalize

app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", secrets.token_hex(32)),
    max_age=60 * 60 * 24 * 7,
)


@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8000/auth/spotify/callback")
SPOTIFY_SCOPE = "playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public"
SPOTIFY_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

YANDEX_CLIENT_ID = os.getenv("YANDEX_CLIENT_ID", "")
YANDEX_CLIENT_SECRET = os.getenv("YANDEX_CLIENT_SECRET", "")
YANDEX_REDIRECT_URI = os.getenv("YANDEX_REDIRECT_URI", "http://127.0.0.1:8000/auth/yandex/callback")

jobs: dict = {}
device_auth_jobs: dict = {}  # job_id -> {status, code_info, token, error}


def _error_redirect(message: str, status_code: int = 303):
    return RedirectResponse(f"/?{urlencode({'error': message})}", status_code=status_code)


def _response_json(resp: httpx.Response, label: str):
    try:
        return resp.json()
    except ValueError:
        body = (resp.text or "").strip()
        print(f"[{label}] non-json response: status={resp.status_code}, body={body[:500]}", flush=True)
        return None


def _oauth_error(resp: httpx.Response, data):
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return err.get("message") or err.get("reason") or str(err)
        if isinstance(err, str):
            desc = data.get("error_description") or data.get("message")
            return f"{err}: {desc}" if desc else err
        if data.get("error_description"):
            return data["error_description"]
    body = (resp.text or "").strip()
    return body[:300] if body else f"HTTP {resp.status_code}"


def _clear_spotify_session(request: Request):
    for key in ("spotify_token", "spotify_refresh", "spotify_user"):
        request.session.pop(key, None)


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "spotify_connected": "spotify_token" in request.session,
        "yandex_connected": "yandex_token" in request.session,
        "spotify_user": request.session.get("spotify_user"),
        "yandex_user": request.session.get("yandex_user"),
        "yandex_oauth_enabled": bool(YANDEX_CLIENT_ID),
        "yandex_session_ready": "yandex_session_id" in request.session or "yandex_music_token" in request.session,
        "error": request.query_params.get("error", ""),
    })


# ── Spotify token refresh ─────────────────────────────────────────────────────

async def _refresh_spotify_token(request: Request):
    refresh = request.session.get("spotify_refresh")
    if not refresh:
        return None
    try:
        async with httpx.AsyncClient(timeout=SPOTIFY_HTTP_TIMEOUT) as client:
            resp = await client.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "refresh_token", "refresh_token": refresh},
                auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
            )
    except httpx.RequestError as e:
        print(f"[SPOTIFY REFRESH] request failed: {e}", flush=True)
        return None
    data = _response_json(resp, "SPOTIFY REFRESH")
    if not data:
        return None
    if resp.status_code >= 400:
        print(f"[SPOTIFY REFRESH] failed: {_oauth_error(resp, data)}", flush=True)
        return None
    if "access_token" not in data:
        return None
    request.session["spotify_token"] = data["access_token"]
    if "refresh_token" in data:
        request.session["spotify_refresh"] = data["refresh_token"]
    return data["access_token"]


# ── Spotify OAuth ──────────────────────────────────────────────────────────────

@app.get("/auth/spotify")
async def spotify_auth(request: Request):
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return _error_redirect("spotify_not_configured")
    state = secrets.token_hex(16)
    request.session["spotify_state"] = state
    params = urlencode({
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "scope": SPOTIFY_SCOPE,
        "state": state,
        "show_dialog": "true",
    })
    return RedirectResponse(f"https://accounts.spotify.com/authorize?{params}")


@app.get("/auth/spotify/callback")
async def spotify_callback(request: Request, code: str = None, error: str = None, state: str = None):
    if error or not code:
        return _error_redirect("spotify_denied")

    expected_state = request.session.pop("spotify_state", None)
    if not expected_state or state != expected_state:
        return _error_redirect("spotify_state_mismatch")

    try:
        async with httpx.AsyncClient(timeout=SPOTIFY_HTTP_TIMEOUT) as client:
            resp = await client.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "authorization_code", "code": code, "redirect_uri": SPOTIFY_REDIRECT_URI},
                auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
            )
    except httpx.TimeoutException as e:
        print(f"[SPOTIFY TOKEN] timeout: {e}", flush=True)
        return _error_redirect("spotify_token_timeout")
    except httpx.RequestError as e:
        print(f"[SPOTIFY TOKEN] request failed: {e}", flush=True)
        return _error_redirect("spotify_token_request_failed")

    tokens = _response_json(resp, "SPOTIFY TOKEN")
    if not tokens:
        return _error_redirect("spotify_token_bad_response")
    if resp.status_code >= 400:
        message = _oauth_error(resp, tokens)
        print(f"[SPOTIFY TOKEN] failed: {message}", flush=True)
        return _error_redirect(f"spotify_token_failed: {message}")
    if "access_token" not in tokens:
        print(f"[SPOTIFY TOKEN] access_token missing: {tokens}", flush=True)
        return _error_redirect("spotify_token_missing")

    token = tokens["access_token"]

    try:
        async with httpx.AsyncClient(timeout=SPOTIFY_HTTP_TIMEOUT) as client:
            me_resp = await client.get(
                "https://api.spotify.com/v1/me",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.TimeoutException as e:
        print(f"[SPOTIFY ME] timeout: {e}", flush=True)
        _clear_spotify_session(request)
        return _error_redirect("spotify_profile_timeout")
    except httpx.RequestError as e:
        print(f"[SPOTIFY ME] request failed: {e}", flush=True)
        _clear_spotify_session(request)
        return _error_redirect("spotify_profile_request_failed")

    me = _response_json(me_resp, "SPOTIFY ME")
    if not me:
        _clear_spotify_session(request)
        message = _oauth_error(me_resp, None)
        return _error_redirect(f"spotify_profile_failed: {message}")
    if me_resp.status_code >= 400:
        message = _oauth_error(me_resp, me)
        print(f"[SPOTIFY ME] failed: {message}", flush=True)
        _clear_spotify_session(request)
        return _error_redirect(f"spotify_profile_failed: {message}")
    request.session["spotify_token"] = token
    request.session["spotify_refresh"] = tokens.get("refresh_token", "")
    request.session["spotify_user"] = me.get("display_name") or me.get("id", "Spotify")
    return RedirectResponse("/", status_code=303)


@app.get("/auth/spotify/logout")
async def spotify_logout(request: Request):
    for k in ("spotify_token", "spotify_refresh", "spotify_user"):
        request.session.pop(k, None)
    return RedirectResponse("/")


# ── Yandex OAuth ───────────────────────────────────────────────────────────────

@app.get("/auth/yandex")
async def yandex_auth(request: Request):
    if not YANDEX_CLIENT_ID:
        return RedirectResponse("/?error=yandex_not_configured")
    state = secrets.token_hex(16)
    request.session["yandex_state"] = state
    params = f"response_type=code&client_id={YANDEX_CLIENT_ID}&redirect_uri={YANDEX_REDIRECT_URI}&state={state}"
    return RedirectResponse(f"https://oauth.yandex.ru/authorize?{params}")


@app.get("/auth/yandex/callback")
async def yandex_callback(request: Request, code: str = None, error: str = None):
    if error or not code:
        return RedirectResponse("/?error=yandex_denied")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth.yandex.ru/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": YANDEX_CLIENT_ID,
                "client_secret": YANDEX_CLIENT_SECRET,
            },
        )
    tokens = resp.json()
    if "access_token" not in tokens:
        return RedirectResponse("/?error=yandex_token_failed")

    await _save_yandex_session(request, tokens["access_token"])
    return RedirectResponse("/")


@app.post("/auth/yandex/token")
async def yandex_token_direct(request: Request):
    form = await request.form()
    raw = (form.get("token") or "").strip()

    # strip "OAuth " or "Bearer " prefix
    for prefix in ("oauth ", "bearer "):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):]

    # user may paste the whole fragment: access_token=TOKEN&token_type=...
    if "access_token=" in raw:
        import re
        m = re.search(r"access_token=([^&]+)", raw)
        raw = m.group(1) if m else raw

    token = raw.strip()
    try:
        await _save_yandex_session(request, token)
        return RedirectResponse("/", status_code=303)
    except Exception as e:
        import traceback
        print(f"[YANDEX TOKEN ERROR] {e}")
        print(traceback.format_exc())
        from urllib.parse import quote
        return RedirectResponse(f"/?error={quote(str(e)[:200])}", status_code=303)


async def _get_yandex_account_info(token: str):
    """
    Получает базовую информацию аккаунта через Passport API.
    Не использует yandex-music SDK.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://login.yandex.ru/info",
            params={"format": "json"},
            headers={"Authorization": f"OAuth {token}"},
        )

    try:
        data = resp.json()
    except ValueError:
        data = {}

    if resp.status_code >= 400:
        message = (
            data.get("error_description")
            or data.get("error")
            or f"HTTP {resp.status_code}"
        )
        raise RuntimeError(f"Yandex OAuth: {message}")

    return data


async def _save_yandex_session(request: Request, token: str):
    """
    Сохраняет обычный Yandex OAuth token.

    ВАЖНО:
    здесь больше нет YMClient(token).init().
    Это специально, чтобы не попадать в ошибку
    common_period_duration.
    """
    token = (token or "").strip()

    if not token:
        raise ValueError("Пустой токен Яндекса")

    info = await _get_yandex_account_info(token)

    uid = info.get("id")
    login = (
        info.get("login")
        or info.get("display_name")
        or info.get("real_name")
    )

    if not uid:
        raise RuntimeError(
            "Не удалось определить ID пользователя Яндекса"
        )

    uid = str(uid)

    request.session["yandex_token"] = token
    request.session["yandex_uid"] = uid
    request.session["yandex_user"] = login or "Яндекс"

    # Если для этого аккаунта уже сохранён токен Яндекс Музыки —
    # восстанавливаем его.
    saved_tokens = _load_ym_tokens()
    saved_music_token = saved_tokens.get(uid)

    if saved_music_token:
        request.session["yandex_music_token"] = saved_music_token
        request.session.pop("yandex_session_id", None)


@app.post("/auth/yandex/login")
async def yandex_login_submit(request: Request):
    form = await request.form()
    login = (form.get("login") or "").strip()
    password = (form.get("password") or "").strip()
    if not login or not password:
        from urllib.parse import quote
        return RedirectResponse(f"/auth/yandex/login?error={quote('Введи логин и пароль')}", status_code=303)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://oauth.yandex.ru/token",
                data={
                    "grant_type": "password",
                    "client_id": "23cabbbdc6cd418abb4b39c32c41195d",
                    "client_secret": "53bc75238f0c4d08a118e51fe9203300",
                    "username": login,
                    "password": password,
                },
            )
        data = resp.json()
        if "access_token" not in data:
            err = data.get("error_description") or data.get("error") or str(data)
            from urllib.parse import quote
            return RedirectResponse(f"/auth/yandex/login?error={quote(err)}", status_code=303)
        await _save_yandex_session(request, data["access_token"])
        return RedirectResponse("/", status_code=303)
    except Exception as e:
        from urllib.parse import quote
        return RedirectResponse(f"/auth/yandex/login?error={quote(str(e)[:200])}", status_code=303)


@app.get("/auth/yandex/get-token")
async def yandex_get_token_page():
    return RedirectResponse("/auth/yandex/login")


@app.post("/auth/yandex/session-cookie")
async def yandex_set_session_cookie(request: Request):
    form = await request.form()
    raw = (form.get("session_id") or "").strip()
    if raw:
        if "Session_id=" in raw or "sessionid2=" in raw:
            request.session["yandex_session_id"] = raw
        else:
            request.session["yandex_session_id"] = f"Session_id={raw}"
    return RedirectResponse("/", status_code=303)


YM_TOKEN_FILE = os.path.join(os.path.dirname(__file__), ".ym_music_tokens.json")

def _load_ym_tokens() -> dict:
    try:
        with open(YM_TOKEN_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_ym_token(uid: str, token: str):
    tokens = _load_ym_tokens()
    tokens[uid] = token
    fd = os.open(YM_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tokens, f)


@app.get("/auth/yandex/music-oauth-url")
async def yandex_music_oauth_url():
    """Returns the Yandex Music implicit OAuth URL (token goes to music.yandex.ru#access_token=...)."""
    url = "https://oauth.yandex.ru/authorize?response_type=token&client_id=23cabbbdc6cd418abb4b39c32c41195d"
    return {"url": url}


@app.get("/auth/yandex/music-oauth-open")
async def yandex_music_oauth_open():
    url = "https://oauth.yandex.ru/authorize?response_type=token&client_id=23cabbbdc6cd418abb4b39c32c41195d"
    return RedirectResponse(url)


@app.post("/auth/yandex/music-token")
async def yandex_set_music_token(request: Request):
    form = await request.form()
    raw = (form.get("token") or "").strip()

    # Можно вставить:
    # access_token=XXXX&token_type=bearer...
    import re

    match = re.search(r"access_token=([^&\s]+)", raw)
    token = match.group(1) if match else raw

    token = token.strip()

    if not token:
        return RedirectResponse(
            "/?error=empty_token",
            status_code=303,
        )

    try:
        # Обычно uid уже есть после авторизации Яндекса.
        uid = request.session.get("yandex_uid")
        login = request.session.get("yandex_user")

        # Если uid ещё нет — пробуем определить его
        # через обычный Yandex OAuth token.
        if not uid:
            yandex_oauth_token = request.session.get("yandex_token")

            if yandex_oauth_token:
                info = await _get_yandex_account_info(
                    yandex_oauth_token
                )

                uid = info.get("id")
                login = (
                    info.get("login")
                    or info.get("display_name")
                    or login
                )

        if not uid:
            raise RuntimeError(
                "Сначала войди в аккаунт Яндекса, "
                "затем добавь токен Яндекс Музыки."
            )

        uid = str(uid)

        # Сохраняем Music OAuth token.
        request.session["yandex_music_token"] = token
        request.session["yandex_uid"] = uid
        request.session["yandex_user"] = login or "Яндекс"

        # Для совместимости с существующей логикой приложения.
        request.session["yandex_token"] = (
            request.session.get("yandex_token") or token
        )

        request.session.pop("yandex_session_id", None)

        _save_ym_token(uid, token)

        return RedirectResponse(
            "/",
            status_code=303,
        )

    except Exception as e:
        import traceback
        from urllib.parse import quote

        print(f"[YANDEX MUSIC TOKEN ERROR] {e}", flush=True)
        print(traceback.format_exc(), flush=True)

        return RedirectResponse(
            f"/?error={quote(str(e)[:300])}",
            status_code=303,
        )


@app.post("/auth/yandex/device/start")
async def yandex_device_start():
    """Start Yandex Music device auth flow. Returns job_id + code info when available."""
    job_id = str(uuid.uuid4())
    device_auth_jobs[job_id] = {"status": "starting", "code_info": None, "token": None, "error": None}

    def run():
        def on_code(code):
            device_auth_jobs[job_id]["code_info"] = {
                "user_code": code.user_code,
                "verification_url": code.verification_url,
                "expires_in": getattr(code, "expires_in", 300),
            }
            device_auth_jobs[job_id]["status"] = "awaiting_user"

        try:
            result = YMClient().device_auth(on_code=on_code)
            device_auth_jobs[job_id]["token"] = result.access_token
            device_auth_jobs[job_id]["status"] = "done"
        except Exception as e:
            device_auth_jobs[job_id]["error"] = str(e)
            device_auth_jobs[job_id]["status"] = "error"

    asyncio.get_event_loop().run_in_executor(None, run)
    return {"job_id": job_id}


@app.get("/auth/yandex/device/status/{job_id}")
async def yandex_device_status(job_id: str):
    job = device_auth_jobs.get(job_id)
    if not job:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return {
        "status": job["status"],
        "code_info": job["code_info"],
        "error": job["error"],
    }


@app.post("/auth/yandex/device/confirm/{job_id}")
async def yandex_device_confirm(job_id: str, request: Request):
    """Called by frontend when polling detects token is ready."""
    job = device_auth_jobs.get(job_id)
    if not job or job["status"] != "done" or not job["token"]:
        return JSONResponse({"error": "not_ready"}, status_code=400)

    token = job["token"]
    try:
        await _save_yandex_session(request, token)
        uid = request.session.get("yandex_uid")
        if uid:
            _save_ym_token(uid, token)
            request.session["yandex_music_token"] = token
        device_auth_jobs.pop(job_id, None)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/auth/yandex/logout")
async def yandex_logout(request: Request):
    for k in ("yandex_token", "yandex_user", "yandex_uid", "yandex_session_id"):
        request.session.pop(k, None)
    return RedirectResponse("/")


# ── API: Playlists ─────────────────────────────────────────────────────────────

@app.get("/api/playlists/spotify")
async def spotify_playlists(request: Request):
    token = request.session.get("spotify_token")
    if not token:
        return JSONResponse({"error": "not_connected"}, status_code=401)

    try:
        playlists, url, refreshed = [], "https://api.spotify.com/v1/me/playlists?limit=50", False
        async with httpx.AsyncClient(timeout=SPOTIFY_HTTP_TIMEOUT) as client:
            while url:
                headers = {"Authorization": f"Bearer {token}"}
                resp = await client.get(url, headers=headers)
                data = _response_json(resp, "SPOTIFY PLAYLISTS")
                if not data:
                    message = _oauth_error(resp, None)
                    if resp.status_code in (401, 403):
                        _clear_spotify_session(request)
                        return JSONResponse({"error": message}, status_code=401)
                    return JSONResponse({"error": message}, status_code=502)
                if resp.status_code >= 400 or "error" in data:
                    err = data.get("error") if isinstance(data, dict) else None
                    status = err.get("status") if isinstance(err, dict) else resp.status_code
                    if status == 401 and not refreshed:
                        token = await _refresh_spotify_token(request)
                        if not token:
                            _clear_spotify_session(request)
                            return JSONResponse({"error": "Токен истёк, войди заново через Spotify"}, status_code=401)
                        refreshed = True
                        continue
                    msg = _oauth_error(resp, data)
                    if status in (401, 403):
                        _clear_spotify_session(request)
                    return JSONResponse({"error": msg}, status_code=400)
                for p in data.get("items", []):
                    if p:
                        tracks_info = p.get("tracks") or p.get("items")
                        playlists.append({"id": p["id"], "name": p["name"], "count": (tracks_info or {}).get("total", 0)})
                url = data.get("next")
        return {"playlists": playlists}
    except httpx.TimeoutException as e:
        print(f"[spotify_playlists] TIMEOUT: {e}", flush=True)
        return JSONResponse({"error": "Spotify API timeout"}, status_code=504)
    except httpx.RequestError as e:
        print(f"[spotify_playlists] REQUEST ERROR: {e}", flush=True)
        return JSONResponse({"error": f"Spotify API request failed: {e}"}, status_code=502)
    except Exception as e:
        print(f"[spotify_playlists] ERROR: {e}", flush=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/playlists/yandex")
async def yandex_playlists(request: Request):
    token = request.session.get("yandex_token")

    if not token:
        return JSONResponse(
            {"error": "not_connected"},
            status_code=401,
        )

    music_token = request.session.get("yandex_music_token")
    session_id = request.session.get("yandex_session_id")
    uid = request.session.get("yandex_uid")

    if not uid:
        return JSONResponse(
            {
                "error": (
                    "Не удалось определить пользователя Яндекса. "
                    "Войди заново."
                )
            },
            status_code=401,
        )

    if not music_token and not session_id:
        return JSONResponse(
            {
                "error": (
                    "Не подключён Яндекс Музыка. "
                    "Добавь отдельный токен Яндекс Музыки."
                )
            },
            status_code=401,
        )

    try:
        import requests as req

        headers = _ym_auth_headers(
            music_token,
            session_id,
        )

        response = req.get(
            f"https://api.music.yandex.ru/users/{uid}/playlists/list",
            headers=headers,
            timeout=15,
        )

        try:
            data = response.json()
        except ValueError:
            return JSONResponse(
                {
                    "error": (
                        f"Яндекс вернул не JSON "
                        f"(HTTP {response.status_code})"
                    )
                },
                status_code=502,
            )

        if response.status_code >= 400:
            return JSONResponse(
                {
                    "error": (
                        data.get("error")
                        or data.get("message")
                        or f"Yandex HTTP {response.status_code}"
                    )
                },
                status_code=502,
            )

        result = data.get("result")

        if result is None:
            return JSONResponse(
                {
                    "error": (
                        data.get("error")
                        or "Яндекс не вернул список плейлистов"
                    )
                },
                status_code=502,
            )

        playlists = []

        for playlist in result:
            if not playlist:
                continue

            playlists.append({
                "id": str(playlist.get("kind")),
                "name": playlist.get("title", "Без названия"),
                "count": playlist.get("trackCount", 0),
            })

        return {"playlists": playlists}

    except req.RequestException as e:
        print(
            f"[yandex_playlists] request failed: {e}",
            flush=True,
        )
        return JSONResponse(
            {"error": f"Ошибка соединения с Яндексом: {e}"},
            status_code=502,
        )

    except Exception as e:
        import traceback

        print(
            f"[yandex_playlists] ERROR: {e}",
            flush=True,
        )
        print(
            traceback.format_exc(),
            flush=True,
        )

        return JSONResponse(
            {"error": str(e)},
            status_code=500,
        )


# ── API: Transfer ──────────────────────────────────────────────────────────────

@app.post("/api/transfer")
async def start_transfer(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    spotify_token = request.session.get("spotify_token")
    yandex_token = request.session.get("yandex_token")
    yandex_uid = request.session.get("yandex_uid")
    if not spotify_token or not yandex_token:
        return JSONResponse({"error": "not_connected"}, status_code=401)

    yandex_session_id = request.session.get("yandex_session_id")
    yandex_music_token = request.session.get("yandex_music_token")
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"events": [], "done": False, "found": 0, "not_found": 0, "missing": []}
    background_tasks.add_task(
        _run_transfer,
        job_id, body["direction"], body["playlist_id"], body["playlist_name"],
        spotify_token, yandex_token, yandex_uid, yandex_session_id, yandex_music_token,
    )
    return {"job_id": job_id}


@app.get("/api/transfer/{job_id}/stream")
async def transfer_stream(job_id: str):
    async def stream():
        cursor = 0
        while True:
            job = jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Job not found'})}\n\n"
                return
            while cursor < len(job["events"]):
                yield f"data: {json.dumps(job['events'][cursor])}\n\n"
                cursor += 1
            if job["done"]:
                yield f"data: {json.dumps({'type': 'done', 'found': job['found'], 'not_found': job['not_found'], 'missing': job['missing']})}\n\n"
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(stream(), media_type="text/event-stream",
                              headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Transfer worker (runs in thread via BackgroundTasks) ───────────────────────

def _ym_search_track(query, music_token=None, session_id=None):
    """Search for a track on Yandex Music. Returns {id, album_id} or None."""
    import requests as req
    r = req.get(
        "https://api.music.yandex.ru/search",
        params={"text": query, "type": "track", "page": 0},
        headers=_ym_auth_headers(music_token, session_id),
        timeout=15,
    ).json()
    results = (r.get("result") or {}).get("tracks") or {}
    items = results.get("results") or []
    if items:
        t = items[0]
        albums = t.get("albums") or []
        album_id = albums[0]["id"] if albums else None
        return {"id": t["id"], "album_id": album_id}
    return None


def _ym_headers(session_id):
    return {
        "Cookie": session_id,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Origin": "https://music.yandex.ru",
        "Referer": "https://music.yandex.ru/",
    }


def _ym_auth_headers(music_token=None, session_id=None):
    """Returns auth headers preferring music_token (Bearer) over session_id (Cookie)."""
    if music_token:
        return {
            "Authorization": f"OAuth {music_token}",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
    return _ym_headers(session_id)


def _ym_create_playlist(uid, title, music_token=None, session_id=None):
    import requests as req
    r = req.post(
        f"https://api.music.yandex.ru/users/{uid}/playlists/create",
        data={"title": title, "visibility": "public"},
        headers=_ym_auth_headers(music_token, session_id),
        timeout=15,
    ).json()
    result = r.get("result")
    if not result:
        raise Exception(f"Ошибка создания плейлиста: {r.get('error', r)}")
    return result["kind"]


def _ym_add_tracks(uid, kind, tracks, music_token=None, session_id=None):
    import requests as req
    h = _ym_auth_headers(music_token, session_id)
    pl = req.get(
        f"https://api.music.yandex.ru/users/{uid}/playlists/{kind}",
        headers=h,
        timeout=15,
    ).json().get("result", {})
    revision = pl.get("revision", 1)
    diff = json.dumps([{"op": "insert", "at": 0,
                        "tracks": [{"id": t["id"], "albumId": t["album_id"]} for t in tracks]}])
    r = req.post(
        f"https://api.music.yandex.ru/users/{uid}/playlists/{kind}/change",
        data={"diff": diff, "revision": revision},
        headers=h,
        timeout=15,
    ).json()
    if "error" in r:
        raise Exception(f"Ошибка добавления треков: {r['error']}")


def _run_transfer(
    job_id,
    direction,
    playlist_id,
    playlist_name,
    spotify_token,
    yandex_token,
    yandex_uid=None,
    yandex_session_id=None,
    yandex_music_token=None,
):
    """
    Выполняет перенос плейлиста:

        spotify_to_yandex
        yandex_to_spotify

    Важно:
    - yandex-music SDK здесь НЕ используется;
    - YMClient(...).init() здесь НЕ вызывается;
    - вся работа с Yandex Music идёт через HTTP API;
    - Spotify можно использовать через spotipy.
    """

    job = jobs[job_id]

    def push(event_type, **kwargs):
        job["events"].append({
            "type": event_type,
            **kwargs,
        })

    try:
        import requests as req

        # ---------------------------------------------------------
        # 1. Проверяем Spotify token
        # ---------------------------------------------------------

        if not spotify_token:
            raise RuntimeError(
                "Не найден Spotify access token."
            )

        # Spotipy нужен только для поиска треков.
        sp = spotipy.Spotify(
            auth=spotify_token
        )

        # ---------------------------------------------------------
        # 2. Определяем Yandex UID
        # ---------------------------------------------------------

        uid = yandex_uid

        if not uid:
            raise RuntimeError(
                "Не удалось определить ID пользователя Яндекс. "
                "Выйди из аккаунта Яндекса и войди заново."
            )

        uid = str(uid)

        # ---------------------------------------------------------
        # 3. Определяем способ доступа к Yandex Music
        # ---------------------------------------------------------

        ym_mt = (
            yandex_music_token
            or yandex_token
        )

        ym_sid = yandex_session_id

        if not ym_mt and not ym_sid:
            raise RuntimeError(
                "Не найден токен Яндекс Музыки "
                "или Yandex Music session."
            )

        ym_headers = _ym_auth_headers(
            ym_mt,
            ym_sid,
        )

        # =========================================================
        # SPOTIFY -> YANDEX MUSIC
        # =========================================================

        if direction == "spotify_to_yandex":

            # -----------------------------------------------------
            # Получаем треки Spotify
            # -----------------------------------------------------

            push(
                "status",
                message="Получаю треки из Spotify..."
            )

            tracks = _get_spotify_tracks(
                spotify_token,
                playlist_id,
            )

            push(
                "total",
                count=len(tracks),
            )

            if not tracks:
                push(
                    "error",
                    message=(
                        "В Spotify-плейлисте нет доступных треков."
                    ),
                )
                return

            # -----------------------------------------------------
            # Создаём новый плейлист Yandex Music
            # -----------------------------------------------------

            push(
                "status",
                message="Создаю плейлист в Яндекс Музыке..."
            )

            new_playlist_name = (
                f"{playlist_name} (from Spotify)"
            )

            kind = _ym_create_playlist(
                uid,
                new_playlist_name,
                ym_mt,
                ym_sid,
            )

            print(
                f"[TRANSFER] Yandex playlist created: "
                f"uid={uid}, kind={kind}",
                flush=True,
            )

            # -----------------------------------------------------
            # Ищем каждый трек
            # -----------------------------------------------------

            found = []

            for i, track in enumerate(tracks, 1):

                artist = (
                    track.get("artist")
                    or ""
                ).strip()

                title = (
                    track.get("title")
                    or ""
                ).strip()

                if not title:
                    job["not_found"] += 1

                    job["missing"].append(
                        f"{artist} — {title}"
                    )

                    push(
                        "track",
                        i=i,
                        status="miss",
                        artist=artist,
                        title=title,
                    )

                    continue

                # Нормализуем только для поиска.
                normalized_artist = normalize(
                    artist
                )

                normalized_title = normalize(
                    title
                )

                query = (
                    f"{normalized_artist} "
                    f"{normalized_title}"
                ).strip()

                try:
                    found_track = _ym_search_track(
                        query,
                        ym_mt,
                        ym_sid,
                    )

                except Exception as e:
                    print(
                        f"[YANDEX SEARCH ERROR] "
                        f"{artist} - {title}: {e}",
                        flush=True,
                    )

                    found_track = None

                if found_track:
                    found.append(
                        found_track
                    )

                    job["found"] += 1

                    push(
                        "track",
                        i=i,
                        status="ok",
                        artist=artist,
                        title=title,
                    )

                else:
                    job["not_found"] += 1

                    job["missing"].append(
                        f"{artist} — {title}"
                    )

                    push(
                        "track",
                        i=i,
                        status="miss",
                        artist=artist,
                        title=title,
                    )

            # -----------------------------------------------------
            # Добавляем найденные треки в Yandex playlist
            # -----------------------------------------------------

            if found:

                push(
                    "status",
                    message=(
                        f"Добавляю {len(found)} "
                        f"треков в Яндекс Музыку..."
                    ),
                )

                _ym_add_tracks(
                    uid,
                    kind,
                    found,
                    ym_mt,
                    ym_sid,
                )

                push(
                    "status",
                    message=(
                        f"Добавлено треков: {len(found)}"
                    ),
                )

            else:
                push(
                    "status",
                    message=(
                        "Не найдено ни одного трека "
                        "в Яндекс Музыке."
                    ),
                )

        # =========================================================
        # YANDEX MUSIC -> SPOTIFY
        # =========================================================

        elif direction == "yandex_to_spotify":

            # -----------------------------------------------------
            # Получаем Yandex playlist
            # -----------------------------------------------------

            push(
                "status",
                message=(
                    "Получаю плейлист "
                    "из Яндекс Музыки..."
                ),
            )

            playlist_url = (
                f"https://api.music.yandex.ru/users/"
                f"{uid}/playlists/{playlist_id}"
            )

            playlist_resp = req.get(
                playlist_url,
                headers=ym_headers,
                timeout=20,
            )

            try:
                playlist_data = (
                    playlist_resp.json()
                )
            except ValueError:
                raise RuntimeError(
                    "Яндекс Музыка вернула "
                    "некорректный ответ при получении "
                    "плейлиста."
                )

            if playlist_resp.status_code >= 400:
                error = (
                    playlist_data.get("error")
                    or playlist_data.get("message")
                    or playlist_data
                )

                raise RuntimeError(
                    f"Yandex Music HTTP "
                    f"{playlist_resp.status_code}: "
                    f"{error}"
                )

            playlist_result = (
                playlist_data.get("result")
            )

            if not playlist_result:
                raise RuntimeError(
                    "Плейлист Яндекс Музыки "
                    "не найден."
                )

            # -----------------------------------------------------
            # Получаем короткие записи треков
            # -----------------------------------------------------

            short_tracks = (
                playlist_result.get("tracks")
                or []
            )

            if not short_tracks:
                push(
                    "total",
                    count=0,
                )

                push(
                    "status",
                    message=(
                        "Плейлист Яндекс Музыки пуст."
                    ),
                )

                return

            # -----------------------------------------------------
            # Получаем ID треков
            # -----------------------------------------------------

            track_ids = []

            for item in short_tracks:
                if not item:
                    continue

                track_id = item.get("id")

                if track_id is None:
                    continue

                track_ids.append(
                    str(track_id)
                )

            if not track_ids:
                raise RuntimeError(
                    "В плейлисте не найдено "
                    "ни одного ID трека."
                )

            # -----------------------------------------------------
            # Yandex API может принять список track-ids
            # -----------------------------------------------------

            tracks = []

            # Делаем небольшие пачки, чтобы URL/запрос
            # не становился слишком большим.
            for start in range(
                0,
                len(track_ids),
                100,
            ):

                batch_ids = track_ids[
                    start:start + 100
                ]

                tracks_resp = req.get(
                    "https://api.music.yandex.ru/tracks",
                    params={
                        "track-ids": ",".join(
                            batch_ids
                        )
                    },
                    headers=ym_headers,
                    timeout=30,
                )

                try:
                    tracks_data = (
                        tracks_resp.json()
                    )
                except ValueError:
                    raise RuntimeError(
                        "Яндекс Музыка вернула "
                        "некорректный ответ при "
                        "получении треков."
                    )

                if tracks_resp.status_code >= 400:
                    error = (
                        tracks_data.get("error")
                        or tracks_data.get("message")
                        or tracks_data
                    )

                    raise RuntimeError(
                        f"Yandex Music HTTP "
                        f"{tracks_resp.status_code}: "
                        f"{error}"
                    )

                result_tracks = (
                    tracks_data.get("result")
                    or []
                )

                for track in result_tracks:

                    if not track:
                        continue

                    title = (
                        track.get("title")
                        or ""
                    )

                    artists = (
                        track.get("artists")
                        or []
                    )

                    artist = ""

                    if artists:
                        artist = (
                            artists[0].get("name")
                            or ""
                        )

                    if not title:
                        continue

                    tracks.append({
                        "title": title,
                        "artist": artist,
                    })

            # -----------------------------------------------------
            # Сообщаем количество
            # -----------------------------------------------------

            push(
                "total",
                count=len(tracks),
            )

            if not tracks:
                push(
                    "status",
                    message=(
                        "Не удалось получить "
                        "информацию о треках."
                    ),
                )

                return

            # -----------------------------------------------------
            # Создаём Spotify playlist
            # -----------------------------------------------------

            push(
                "status",
                message=(
                    "Создаю плейлист в Spotify..."
                ),
            )

            spotify_headers = {
                "Authorization": (
                    f"Bearer {spotify_token}"
                ),
                "Content-Type": (
                    "application/json"
                ),
            }

            create_resp = req.post(
                "https://api.spotify.com/v1/me/playlists",
                json={
                    "name": (
                        f"{playlist_name} "
                        f"(from Yandex)"
                    ),
                    "public": False,
                },
                headers=spotify_headers,
                timeout=20,
            )

            try:
                new_playlist = (
                    create_resp.json()
                )
            except ValueError:
                raise RuntimeError(
                    "Spotify вернул "
                    "некорректный ответ при "
                    "создании плейлиста."
                )

            if create_resp.status_code >= 400:
                error = (
                    new_playlist.get("error")
                    or new_playlist
                )

                raise RuntimeError(
                    f"Spotify HTTP "
                    f"{create_resp.status_code}: "
                    f"{error}"
                )

            new_playlist_id = (
                new_playlist.get("id")
            )

            if not new_playlist_id:
                raise RuntimeError(
                    "Spotify не вернул ID "
                    "созданного плейлиста."
                )

            # -----------------------------------------------------
            # Ищем каждый Yandex track в Spotify
            # -----------------------------------------------------

            found_ids = []

            for i, track in enumerate(
                tracks,
                1,
            ):

                artist = (
                    track.get("artist")
                    or ""
                ).strip()

                title = (
                    track.get("title")
                    or ""
                ).strip()

                if not title:
                    job["not_found"] += 1

                    job["missing"].append(
                        f"{artist} — {title}"
                    )

                    push(
                        "track",
                        i=i,
                        status="miss",
                        artist=artist,
                        title=title,
                    )

                    continue

                normalized_artist = normalize(
                    artist
                )

                normalized_title = normalize(
                    title
                )

                # -------------------------------------------------
                # Сначала точный поиск
                # -------------------------------------------------

                try:
                    result = sp.search(
                        q=(
                            f'track:"'
                            f'{normalized_title}'
                            f'" artist:"'
                            f'{normalized_artist}'
                            f'"'
                        ),
                        type="track",
                        limit=1,
                    )

                    items = (
                        result
                        .get("tracks", {})
                        .get("items", [])
                    )

                except Exception as e:
                    print(
                        f"[SPOTIFY SEARCH] "
                        f"{artist} - {title}: {e}",
                        flush=True,
                    )

                    items = []

                # -------------------------------------------------
                # Если точный поиск не дал результата —
                # более свободный поиск
                # -------------------------------------------------

                if not items:

                    try:
                        result = sp.search(
                            q=(
                                f"{normalized_artist} "
                                f"{normalized_title}"
                            ).strip(),
                            type="track",
                            limit=1,
                        )

                        items = (
                            result
                            .get("tracks", {})
                            .get("items", [])
                        )

                    except Exception as e:
                        print(
                            f"[SPOTIFY FALLBACK SEARCH] "
                            f"{artist} - {title}: {e}",
                            flush=True,
                        )

                        items = []

                # -------------------------------------------------
                # Нашли
                # -------------------------------------------------

                if items:

                    spotify_track_id = (
                        items[0].get("id")
                    )

                    if spotify_track_id:

                        found_ids.append(
                            spotify_track_id
                        )

                        job["found"] += 1

                        push(
                            "track",
                            i=i,
                            status="ok",
                            artist=artist,
                            title=title,
                        )

                        continue

                # -------------------------------------------------
                # Не нашли
                # -------------------------------------------------

                job["not_found"] += 1

                job["missing"].append(
                    f"{artist} — {title}"
                )

                push(
                    "track",
                    i=i,
                    status="miss",
                    artist=artist,
                    title=title,
                )

            # -----------------------------------------------------
            # Добавляем Spotify tracks пачками по 100
            # -----------------------------------------------------

            if found_ids:

                push(
                    "status",
                    message=(
                        f"Добавляю {len(found_ids)} "
                        f"треков в Spotify..."
                    ),
                )

                for start in range(
                    0,
                    len(found_ids),
                    100,
                ):

                    batch = found_ids[
                        start:start + 100
                    ]

                    add_resp = req.post(
                        (
                            "https://api.spotify.com/v1/"
                            f"playlists/{new_playlist_id}/items"
                        ),
                        json={
                            "uris": [
                                f"spotify:track:{track_id}"
                                for track_id in batch
                            ]
                        },
                        headers=spotify_headers,
                        timeout=20,
                    )

                    try:
                        add_data = (
                            add_resp.json()
                        )
                    except ValueError:
                        add_data = {}

                    if add_resp.status_code >= 400:
                        error = (
                            add_data.get("error")
                            or add_data
                        )

                        raise RuntimeError(
                            f"Spotify HTTP "
                            f"{add_resp.status_code} "
                            f"при добавлении треков: "
                            f"{error}"
                        )

                    if (
                        isinstance(add_data, dict)
                        and add_data.get("error")
                    ):
                        raise RuntimeError(
                            "Spotify add tracks: "
                            f"{add_data['error']}"
                        )

                push(
                    "status",
                    message=(
                        f"Добавлено в Spotify: "
                        f"{len(found_ids)}"
                    ),
                )

            else:

                push(
                    "status",
                    message=(
                        "В Spotify не найден "
                        "ни один трек."
                    ),
                )

        # =========================================================
        # НЕИЗВЕСТНОЕ НАПРАВЛЕНИЕ
        # =========================================================

        else:
            raise RuntimeError(
                f"Неизвестное направление переноса: "
                f"{direction}"
            )

    except Exception as e:

        import traceback

        print(
            f"[TRANSFER ERROR] {e}",
            flush=True,
        )

        print(
            traceback.format_exc(),
            flush=True,
        )

        push(
            "error",
            message=str(e),
        )

    finally:
        job["done"] = True



def _parse_spotify_items(items):
    tracks = []
    skipped_null = 0
    skipped_local = 0
    for item in items:
        t = item.get("track") or item.get("item")  # Spotify uses "item" key in newer API responses
        if not t or not t.get("name"):
            skipped_null += 1
            continue
        if item.get("is_local") or t.get("is_local"):
            skipped_local += 1
            continue
        artists = t.get("artists") or []
        tracks.append({
            "title": t["name"],
            "artist": artists[0]["name"] if artists else "",
        })
    if skipped_null or skipped_local:
        print(f"[SPOTIFY] skipped: {skipped_null} null tracks, {skipped_local} local tracks", flush=True)
    return tracks


def _get_spotify_tracks(token, playlist_id):
    import requests as req
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}/items?limit=100&market=from_token"
    tracks = []
    resp = req.get(url, headers=headers, timeout=15)

    # On 403 (Forbidden for followed/saved playlists), try spotipy as fallback
    if resp.status_code == 403:
        print(f"[SPOTIFY] 403 for playlist {playlist_id}, trying spotipy fallback", flush=True)
        try:
            sp = spotipy.Spotify(auth=token)
            results = sp.playlist_items(playlist_id, limit=100)
            while results:
                tracks.extend(_parse_spotify_items(results.get("items", [])))
                if results.get("next"):
                    results = sp.next(results)
                else:
                    break
            print(f"[DEBUG] spotipy fallback: {len(tracks)} tracks", flush=True)
            return tracks
        except Exception as e:
            raise Exception(f"Плейлист недоступен (403). Возможно, это чужой приватный плейлист. Детали: {e}")

    data = resp.json()
    if "error" in data:
        raise Exception(f"Spotify: {data['error'].get('message', data['error'])}")

    total_items = data.get("total", "?")
    items = data.get("items", [])
    print(f"[SPOTIFY] playlist total={total_items}, items in page={len(items)}", flush=True)
    if items:
        first = items[0]
        track = first.get("track")
        print(f"[SPOTIFY] first item keys={list(first.keys())}, track={track is not None}, is_local={first.get('is_local')}", flush=True)
        if track:
            print(f"[SPOTIFY] first track name={track.get('name')}, type={track.get('type')}, id={track.get('id')}", flush=True)

    while True:
        tracks.extend(_parse_spotify_items(data.get("items", [])))
        next_url = data.get("next")
        if not next_url:
            break
        data = req.get(next_url, headers=headers, timeout=15).json()
        if "error" in data:
            raise Exception(f"Spotify: {data['error'].get('message', data['error'])}")

    print(f"[SPOTIFY] total tracks fetched: {len(tracks)}", flush=True)
    return tracks


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
