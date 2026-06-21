# Music Rotate

Веб-приложение для переноса плейлистов между Spotify и Яндекс Музыкой в обе стороны.

## Возможности

- Spotify → Яндекс Музыка и обратно
- Прогресс переноса в реальном времени через SSE
- Отчёт о найденных и не найденных треках
- Авторизация Яндекс Музыки через Device Auth Flow (без ручного копирования Cookie)

## Требования

- Python 3.9+
- Аккаунт Spotify (Premium не обязателен для чтения плейлистов)
- Аккаунт Яндекс Музыки
- Зарегистрированное приложение на [developer.spotify.com](https://developer.spotify.com)

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
SECRET_KEY=<случайная строка, минимум 32 символа>

SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8000/auth/spotify/callback

YANDEX_CLIENT_ID=...
YANDEX_CLIENT_SECRET=...
```

### Настройка Spotify

1. Создай приложение на [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
2. В настройках добавь Redirect URI: `http://127.0.0.1:8000/auth/spotify/callback`
3. Скопируй Client ID и Client Secret в `.env`

### Настройка Яндекс

1. Создай приложение на [oauth.yandex.ru](https://oauth.yandex.ru)
2. Права: `login:info`, `music:read`, `music:write` (или `music:all`)
3. Скопируй Client ID и Client Secret в `.env`

## Запуск

```bash
venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8000
```

Открой [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Авторизация

### Spotify
Стандартный OAuth 2.0. Нажми «Подключить» и войди через браузер.

### Яндекс Музыка
Используется Device Auth Flow (RFC 8628) — не нужно копировать Cookie вручную:

1. Нажми «Подключить» на странице подключения
2. Приложение покажет короткий код и ссылку для подтверждения
3. Открой ссылку в браузере, войди в Яндекс и введи код
4. Страница автоматически подтвердит подключение

Токен сохраняется локально в `.ym_music_tokens.json` (файл не попадает в git).

## Безопасность

- Токены хранятся в сессии и локальном файле с правами `600`
- `SECRET_KEY` используется для подписи сессий — держи его в тайне
- Файл `.env` не попадает в git (добавлен в `.gitignore`)
