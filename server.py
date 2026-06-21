import json
import os
import secrets
import uuid
import asyncio
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
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://localhost:8000/auth/spotify/callback")
SPOTIFY_SCOPE = "playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public"

YANDEX_CLIENT_ID = os.getenv("YANDEX_CLIENT_ID", "")
YANDEX_CLIENT_SECRET = os.getenv("YANDEX_CLIENT_SECRET", "")
YANDEX_REDIRECT_URI = os.getenv("YANDEX_REDIRECT_URI", "http://127.0.0.1:8000/auth/yandex/callback")

jobs: dict = {}


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
        "yandex_session_ready": "yandex_session_id" in request.session,
        "error": request.query_params.get("error", ""),
    })


# ── Spotify token refresh ─────────────────────────────────────────────────────

async def _refresh_spotify_token(request: Request):
    refresh = request.session.get("spotify_refresh")
    if not refresh:
        return None
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "refresh_token", "refresh_token": refresh},
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        )
    data = resp.json()
    if "access_token" not in data:
        return None
    request.session["spotify_token"] = data["access_token"]
    if "refresh_token" in data:
        request.session["spotify_refresh"] = data["refresh_token"]
    return data["access_token"]


# ── Spotify OAuth ──────────────────────────────────────────────────────────────

@app.get("/auth/spotify")
async def spotify_auth(request: Request):
    state = secrets.token_hex(16)
    request.session["spotify_state"] = state
    params = "&".join([
        f"client_id={SPOTIFY_CLIENT_ID}",
        "response_type=code",
        f"redirect_uri={SPOTIFY_REDIRECT_URI}",
        f"scope={SPOTIFY_SCOPE.replace(' ', '%20')}",
        f"state={state}",
        "show_dialog=true",
    ])
    return RedirectResponse(f"https://accounts.spotify.com/authorize?{params}")


@app.get("/auth/spotify/callback")
async def spotify_callback(request: Request, code: str = None, error: str = None):
    if error or not code:
        return RedirectResponse("/?error=spotify_denied")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": SPOTIFY_REDIRECT_URI},
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        )
    tokens = resp.json()
    if "access_token" not in tokens:
        return RedirectResponse("/?error=spotify_token_failed")

    token = tokens["access_token"]
    request.session["spotify_token"] = token
    request.session["spotify_refresh"] = tokens.get("refresh_token", "")

    async with httpx.AsyncClient() as client:
        me = (await client.get("https://api.spotify.com/v1/me", headers={"Authorization": f"Bearer {token}"})).json()
    request.session["spotify_user"] = me.get("display_name") or me.get("id", "Spotify")
    return RedirectResponse("/")


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


async def _save_yandex_session(request: Request, token: str):
    def _init():
        return YMClient(token).init()
    ym = await asyncio.to_thread(_init)
    request.session["yandex_token"] = token

    uid = ym.me.account.uid if ym.me and ym.me.account else None
    login = ym.me.account.login if ym.me and ym.me.account else None

    if not uid:
        async with httpx.AsyncClient(timeout=10) as client:
            info = (await client.get(
                "https://login.yandex.ru/info?format=json",
                headers={"Authorization": f"OAuth {token}"}
            )).json()
        uid = info.get("id")
        login = login or info.get("login") or info.get("display_name")

    request.session["yandex_uid"] = str(uid) if uid else None
    request.session["yandex_user"] = login or "Яндекс"


@app.get("/auth/yandex/login")
async def yandex_login_form(request: Request):
    return templates.TemplateResponse("yandex_login.html", {
        "request": request,
        "error": request.query_params.get("error", ""),
    })


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
        # Accept either full Cookie string or just Session_id value
        if "Session_id=" in raw or "sessionid2=" in raw:
            # Full cookie string provided — use as-is
            request.session["yandex_session_id"] = raw
        else:
            # Just the value — wrap it
            request.session["yandex_session_id"] = f"Session_id={raw}"
    return RedirectResponse("/", status_code=303)


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
        async with httpx.AsyncClient(timeout=15) as client:
            while url:
                headers = {"Authorization": f"Bearer {token}"}
                resp = await client.get(url, headers=headers)
                data = resp.json()
                if "error" in data:
                    err = data["error"]
                    status = err.get("status") if isinstance(err, dict) else resp.status_code
                    if status == 401 and not refreshed:
                        token = await _refresh_spotify_token(request)
                        if not token:
                            return JSONResponse({"error": "Токен истёк, войди заново через Spotify"}, status_code=401)
                        refreshed = True
                        continue
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    return JSONResponse({"error": msg}, status_code=400)
                for p in data.get("items", []):
                    if p:
                        tracks_info = p.get("tracks") or p.get("items")
                        playlists.append({"id": p["id"], "name": p["name"], "count": (tracks_info or {}).get("total", 0)})
                url = data.get("next")
        return {"playlists": playlists}
    except Exception as e:
        print(f"[spotify_playlists] ERROR: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/playlists/yandex")
async def yandex_playlists(request: Request):
    token = request.session.get("yandex_token")
    if not token:
        return JSONResponse({"error": "not_connected"}, status_code=401)

    session_id = request.session.get("yandex_session_id")
    uid = request.session.get("yandex_uid")

    if session_id and uid:
        try:
            import requests as req
            r = req.get(
                f"https://api.music.yandex.ru/users/{uid}/playlists/list",
                headers=_ym_headers(session_id),
                timeout=15,
            ).json()
            if "result" in r:
                return {"playlists": [
                    {"id": str(p["kind"]), "name": p["title"], "count": p.get("trackCount", 0)}
                    for p in r["result"]
                ]}
        except Exception as e:
            print(f"[yandex_playlists] Session_id failed: {e}")

    try:
        def _fetch():
            ym = YMClient(token).init()
            return [
                {"id": str(p.kind), "name": p.title, "count": p.track_count}
                for p in ym.users_playlists_list()
            ]
        playlists = await asyncio.to_thread(_fetch)
        return {"playlists": playlists}
    except Exception as e:
        err = str(e)
        if "Unauthorized" in err or "401" in err:
            for k in ("yandex_token", "yandex_user"):
                request.session.pop(k, None)
            return JSONResponse({"error": "Токен Яндекса истёк, войди заново"}, status_code=401)
        return JSONResponse({"error": err}, status_code=500)


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
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"events": [], "done": False, "found": 0, "not_found": 0, "missing": []}
    background_tasks.add_task(
        _run_transfer,
        job_id, body["direction"], body["playlist_id"], body["playlist_name"],
        spotify_token, yandex_token, yandex_uid, yandex_session_id,
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

def _ym_search_track(query, session_id):
    """Search for a track on Yandex Music using cookie auth. Returns {id, album_id} or None."""
    import requests as req
    r = req.get(
        "https://api.music.yandex.ru/search",
        params={"text": query, "type": "track", "page": 0},
        headers=_ym_headers(session_id),
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


def _ym_create_playlist(uid, title, session_id):
    import requests as req
    r = req.post(
        f"https://api.music.yandex.ru/users/{uid}/playlists/create",
        data={"title": title, "visibility": "public"},
        headers=_ym_headers(session_id),
        timeout=15,
    ).json()
    result = r.get("result")
    if not result:
        raise Exception(f"Ошибка создания плейлиста: {r.get('error', r)}")
    return result["kind"]


def _ym_add_tracks(uid, kind, tracks, session_id):
    import requests as req
    pl = req.get(
        f"https://api.music.yandex.ru/users/{uid}/playlists/{kind}",
        headers=_ym_headers(session_id),
        timeout=15,
    ).json().get("result", {})
    revision = pl.get("revision", 1)
    diff = json.dumps([{"op": "insert", "at": 0,
                        "tracks": [{"id": t["id"], "albumId": t["album_id"]} for t in tracks]}])
    r = req.post(
        f"https://api.music.yandex.ru/users/{uid}/playlists/{kind}/change",
        data={"diff": diff, "revision": revision},
        headers=_ym_headers(session_id),
        timeout=15,
    ).json()
    if "error" in r:
        raise Exception(f"Ошибка добавления треков: {r['error']}")


def _run_transfer(job_id, direction, playlist_id, playlist_name, spotify_token, yandex_token, yandex_uid=None, yandex_session_id=None):
    job = jobs[job_id]

    def push(event_type, **kwargs):
        job["events"].append({"type": event_type, **kwargs})

    try:
        sp = spotipy.Spotify(auth=spotify_token)
        ym = YMClient(yandex_token).init()
        uid = (ym.me.account.uid if (ym.me and ym.me.account and ym.me.account.uid) else None) or yandex_uid

        if not uid:
            push("error", message="Не удалось определить ID пользователя Яндекс. Выйди и войди заново.")
            job["done"] = True
            return

        if direction == "spotify_to_yandex":
            tracks = _get_spotify_tracks(spotify_token, playlist_id)
            push("total", count=len(tracks))

            if yandex_session_id:
                kind = _ym_create_playlist(uid, f"{playlist_name} (from Spotify)", yandex_session_id)
            else:
                kind = ym.users_playlists_create(f"{playlist_name} (from Spotify)", user_id=uid).kind

            found = []
            for i, track in enumerate(tracks, 1):
                query = f"{normalize(track['artist'])} {normalize(track['title'])}"
                if yandex_session_id:
                    t = _ym_search_track(query, yandex_session_id)
                else:
                    result = ym.search(query, type_="track")
                    t = None
                    if result and result.tracks and result.tracks.results:
                        r0 = result.tracks.results[0]
                        t = {"id": r0.id, "album_id": r0.albums[0].id if r0.albums else None}
                if t:
                    found.append(t)
                    job["found"] += 1
                    push("track", i=i, status="ok", artist=track["artist"], title=track["title"])
                else:
                    job["not_found"] += 1
                    job["missing"].append(f"{track['artist']} — {track['title']}")
                    push("track", i=i, status="miss", artist=track["artist"], title=track["title"])

            if found:
                if yandex_session_id:
                    _ym_add_tracks(uid, kind, found, yandex_session_id)
                else:
                    pl = ym.users_playlists(kind, uid)[0]
                    diff = json.dumps([{"op": "insert", "at": 0,
                                        "tracks": [{"id": t["id"], "albumId": t["album_id"]} for t in found]}])
                    ym.users_playlists_change(kind, diff, pl.revision, user_id=uid)

        else:  # yandex_to_spotify
            if yandex_session_id:
                import requests as req
                cookie_h = _ym_headers(yandex_session_id)
                pl_resp = req.get(
                    f"https://api.music.yandex.ru/users/{uid}/playlists/{playlist_id}",
                    headers=cookie_h, timeout=15,
                ).json()
                pl_result = pl_resp.get("result")
                if not pl_result:
                    push("error", message=f"Плейлист не найден: {pl_resp.get('error', pl_resp)}")
                    job["done"] = True
                    return
                short_tracks = pl_result.get("tracks", [])
                if short_tracks:
                    track_ids = ",".join(str(t["id"]) for t in short_tracks)
                    tracks_resp = req.get(
                        "https://api.music.yandex.ru/tracks",
                        params={"track-ids": track_ids},
                        headers=cookie_h, timeout=30,
                    ).json()
                    tracks = [
                        {"title": t["title"], "artist": t["artists"][0]["name"] if t.get("artists") else ""}
                        for t in (tracks_resp.get("result") or []) if t
                    ]
                else:
                    tracks = []
            else:
                pl = ym.users_playlists(int(playlist_id), uid)
                if not pl:
                    push("error", message="Плейлист не найден")
                    job["done"] = True
                    return
                short = pl[0].tracks or []
                full = ym.tracks([s.id for s in short]) if short else []
                tracks = [
                    {"title": t.title, "artist": t.artists[0].name if t.artists else ""}
                    for t in (full or []) if t
                ]
            push("total", count=len(tracks))

            import requests as req
            new_pl = req.post(
                "https://api.spotify.com/v1/me/playlists",
                json={"name": f"{playlist_name} (from Yandex)", "public": False},
                headers={"Authorization": f"Bearer {spotify_token}", "Content-Type": "application/json"},
                timeout=15,
            ).json()
            if "id" not in new_pl:
                raise Exception(f"Spotify: не удалось создать плейлист: {new_pl}")
            new_pl_id = new_pl["id"]

            found_ids = []
            for i, track in enumerate(tracks, 1):
                a, t_title = normalize(track["artist"]), normalize(track["title"])
                res = sp.search(q=f'track:"{t_title}" artist:"{a}"', type="track", limit=1)
                items = res["tracks"]["items"]
                if not items:
                    res = sp.search(q=f"{a} {t_title}", type="track", limit=1)
                    items = res["tracks"]["items"]

                if items:
                    found_ids.append(items[0]["id"])
                    job["found"] += 1
                    push("track", i=i, status="ok", artist=track["artist"], title=track["title"])
                else:
                    job["not_found"] += 1
                    job["missing"].append(f"{track['artist']} — {track['title']}")
                    push("track", i=i, status="miss", artist=track["artist"], title=track["title"])

            sp_headers = {"Authorization": f"Bearer {spotify_token}", "Content-Type": "application/json"}
            for i in range(0, len(found_ids), 100):
                batch = [f"spotify:track:{tid}" for tid in found_ids[i:i + 100]]
                r = req.post(
                    f"https://api.spotify.com/v1/playlists/{new_pl_id}/items",
                    json={"uris": batch},
                    headers=sp_headers,
                    timeout=15,
                ).json()
                if "error" in r:
                    raise Exception(f"Spotify add tracks: {r['error']}")

    except Exception as e:
        import traceback
        print(f"[TRANSFER ERROR] {e}")
        print(traceback.format_exc())
        push("error", message=str(e))

    job["done"] = True


@app.get("/debug/test")
async def debug_test(request: Request):
    import requests as req
    result = {}

    # Test Spotify token
    sp_token = request.session.get("spotify_token")
    result["spotify_token_present"] = bool(sp_token)
    if sp_token:
        r = req.get("https://api.spotify.com/v1/me", headers={"Authorization": f"Bearer {sp_token}"}, timeout=10)
        result["spotify_me"] = r.status_code

    # Test Yandex session cookie
    session_id = request.session.get("yandex_session_id")
    uid = request.session.get("yandex_uid")
    result["yandex_session_id_present"] = bool(session_id)
    result["yandex_uid"] = uid
    if session_id and uid:
        r = req.get(
            f"https://api.music.yandex.ru/users/{uid}/playlists/list",
            headers=_ym_headers(session_id),
            timeout=10,
        )
        result["yandex_playlists_status"] = r.status_code
        result["yandex_playlists_response"] = r.json()

    return result


def _get_spotify_tracks(token, playlist_id):
    import requests as req
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}/items?limit=100"
    tracks = []
    resp = req.get(url, headers=headers, timeout=15)
    data = resp.json()
    if "error" in data:
        raise Exception(f"Spotify: {data['error'].get('message', data['error'])}")
    while True:
        for item in data.get("items", []):
            t = item.get("track") or item.get("item")
            if t and t.get("name") and not item.get("is_local"):
                tracks.append({"title": t["name"], "artist": t["artists"][0]["name"] if t.get("artists") else ""})
        next_url = data.get("next")
        if not next_url:
            break
        data = req.get(next_url, headers=headers, timeout=15).json()
        if "error" in data:
            raise Exception(f"Spotify pagination: {data['error'].get('message', data['error'])}")
    print(f"[DEBUG] total tracks fetched: {len(tracks)}", flush=True)
    return tracks


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
