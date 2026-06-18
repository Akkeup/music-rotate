from dotenv import load_dotenv
load_dotenv()

import spotify
import yandex


def pick(items, label_fn, prompt):
    for i, item in enumerate(items):
        print(f"  {i + 1}. {label_fn(item)}")
    while True:
        try:
            idx = int(input(prompt)) - 1
            if 0 <= idx < len(items):
                return items[idx]
        except ValueError:
            pass
        print("Неверный выбор, попробуй снова")


def spotify_to_yandex():
    print("\nПодключение к Spotify...")
    sp = spotify.get_client()

    print("Подключение к Яндекс Музыке...")
    ym = yandex.get_client()

    print("\nТвои плейлисты в Spotify:")
    playlists = spotify.get_playlists(sp)
    if not playlists:
        print("Плейлисты не найдены.")
        return

    pl = pick(playlists, lambda p: f"{p['name']} ({p['tracks']['total']} треков)", "\nВыбери номер: ")

    print(f"\nЧитаю треки из «{pl['name']}»...")
    tracks = spotify.get_playlist_tracks(sp, pl["id"])
    print(f"Найдено {len(tracks)} треков")

    new_name = f"{pl['name']} (from Spotify)"
    print(f"Создаю плейлист «{new_name}» в Яндекс Музыке...")
    kind = yandex.create_playlist(ym, new_name)

    found, not_found = [], []
    print()
    for i, track in enumerate(tracks, 1):
        label = f"{track['artist']} — {track['title']}"
        print(f"  [{i}/{len(tracks)}] {label}", end=" ", flush=True)
        result = yandex.search_track(ym, track["artist"], track["title"])
        if result:
            found.append(result)
            print("✓")
        else:
            not_found.append(track)
            print("✗")

    if found:
        print(f"\nДобавляю {len(found)} треков...")
        yandex.add_tracks(ym, kind, found)

    _print_summary(found, not_found)


def yandex_to_spotify():
    print("\nПодключение к Яндекс Музыке...")
    ym = yandex.get_client()

    print("Подключение к Spotify...")
    sp = spotify.get_client()

    print("\nТвои плейлисты в Яндекс Музыке:")
    playlists = yandex.get_playlists(ym)
    if not playlists:
        print("Плейлисты не найдены.")
        return

    pl = pick(playlists, lambda p: f"{p.title} ({p.track_count} треков)", "\nВыбери номер: ")

    user_id = ym.me.account.uid
    print(f"\nЧитаю треки из «{pl.title}»...")
    tracks = yandex.get_playlist_tracks(ym, pl.kind, user_id)
    print(f"Найдено {len(tracks)} треков")

    new_name = f"{pl.title} (from Yandex)"
    print(f"Создаю плейлист «{new_name}» в Spotify...")
    playlist_id = spotify.create_playlist(sp, new_name)

    found, not_found = [], []
    print()
    for i, track in enumerate(tracks, 1):
        label = f"{track['artist']} — {track['title']}"
        print(f"  [{i}/{len(tracks)}] {label}", end=" ", flush=True)
        track_id = spotify.search_track(sp, track["artist"], track["title"])
        if track_id:
            found.append(track_id)
            print("✓")
        else:
            not_found.append(track)
            print("✗")

    if found:
        print(f"\nДобавляю {len(found)} треков...")
        spotify.add_tracks(sp, playlist_id, found)

    _print_summary(found, not_found)


def _print_summary(found, not_found):
    print(f"\n{'='*40}")
    print(f"Готово! Добавлено: {len(found)}, не найдено: {len(not_found)}")
    if not_found:
        print("\nНе найденные треки:")
        for t in not_found:
            print(f"  - {t['artist']} — {t['title']}")


def main():
    print("=== Music Rotate ===")
    print("\n  1. Spotify → Яндекс Музыка")
    print("  2. Яндекс Музыка → Spotify")

    while True:
        choice = input("\nВведи 1 или 2: ").strip()
        if choice == "1":
            spotify_to_yandex()
            break
        elif choice == "2":
            yandex_to_spotify()
            break
        print("Неверный выбор")


if __name__ == "__main__":
    main()
