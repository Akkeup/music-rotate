import requests

# Публичные credentials Yandex Music Android-приложения
CLIENT_ID = "23cabbbdc6cd418abb4b39c32c41195d"
CLIENT_SECRET = "53bc75238f0c4d08a118e51fe9203300"

login = input("Яндекс логин (email или телефон): ").strip()
password = input("Пароль: ").strip()

response = requests.post(
    "https://oauth.yandex.ru/token",
    data={
        "grant_type": "password",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "username": login,
        "password": password,
    },
)

data = response.json()

if "access_token" in data:
    token = data["access_token"]
    from yandex_music import Client
    try:
        client = Client(token).init()
        print(f"\nУспешно! Аккаунт: {client.me.account.login}")
        print(f"\nТвой токен:\n{token}")
        print("\nСкопируй его в .env → YANDEX_TOKEN=")
    except Exception as e:
        print(f"Токен получен, но ошибка при подключении: {e}")
        print(f"Токен: {token}")
else:
    print(f"\nОшибка: {data}")
