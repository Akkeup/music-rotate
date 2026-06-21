# Music Rotate

Веб-приложение для переноса плейлистов между Spotify и Яндекс Музыкой в обе стороны.

## Возможности

- Spotify → Яндекс Музыка
- Яндекс Музыка → Spotify
- Прогресс переноса в реальном времени
- Отчёт о найденных и не найденных треках

## Требования

- Python 3.9+
- Аккаунт Spotify Premium
- Аккаунт Яндекс Музыки
- Зарегистрированное приложение на [developer.spotify.com](https://developer.spotify.com)
- Зарегистрированное приложение на [oauth.yandex.ru](https://oauth.yandex.ru)

## Установка

```bash
git clone <repo>
cd music-rotate
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Заполни `.env`:

```env
SECRET_KEY=случайная-строка

SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8000/auth/spotify/callback

YANDEX_CLIENT_ID=...
YANDEX_CLIENT_SECRET=...
YANDEX_REDIRECT_URI=http://127.0.0.1:8000/auth/yandex/callback
```

## Запуск

```bash
venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8000
```

Открой [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Авторизация Яндекс Музыки

Яндекс закрыл Music API для сторонних приложений, поэтому после входа через OAuth нужно дополнительно вставить строку Cookie из браузера:

1. Открой [music.yandex.ru](https://music.yandex.ru) и войди в аккаунт
2. F12 → Network → кликни на любой запрос к `api.music.yandex.ru`
3. В заголовках запроса найди `Cookie:` и скопируй всё значение целиком
4. Вставь в поле на сайте

Куки действуют пока активна сессия в браузере.
