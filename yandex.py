import json
import os
from yandex_music import Client
from matcher import normalize


def get_client():
    token = os.getenv("YANDEX_TOKEN")
    return Client(token).init()


def get_playlists(client):
    return client.users_playlists_list()


def get_playlist_tracks(client, kind, user_id):
    playlists = client.users_playlists(kind, user_id)
    if not playlists:
        return []
    playlist = playlists[0]
    short_tracks = playlist.tracks or []
    if not short_tracks:
        return []

    full_tracks = client.tracks([s.id for s in short_tracks])
    tracks = []
    for track in full_tracks or []:
        if not track:
            continue
        tracks.append({
            "title": track.title,
            "artist": track.artists[0].name if track.artists else "",
            "id": track.id,
            "album_id": track.albums[0].id if track.albums else None,
        })
    return tracks


def search_track(client, artist, title):
    query = f"{normalize(artist)} {normalize(title)}"
    result = client.search(query, type_="track")
    if result and result.tracks and result.tracks.results:
        track = result.tracks.results[0]
        album_id = track.albums[0].id if track.albums else None
        return {"id": track.id, "album_id": album_id}
    return None


def create_playlist(client, name):
    user_id = client.me.account.uid
    playlist = client.users_playlists_create(name, user_id=user_id)
    return playlist.kind


def add_tracks(client, kind, tracks_info):
    user_id = client.me.account.uid
    playlist = client.users_playlists(kind, user_id)[0]

    diff = json.dumps([{
        "op": "insert",
        "at": 0,
        "tracks": [{"id": t["id"], "albumId": t["album_id"]} for t in tracks_info],
    }])

    client.users_playlists_change(kind, diff, playlist.revision, user_id=user_id)
