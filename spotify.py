import os
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from matcher import normalize

SCOPE = "playlist-read-private playlist-modify-private playlist-modify-public"


def get_client():
    return spotipy.Spotify(auth_manager=SpotifyOAuth(
        client_id=os.getenv("SPOTIFY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
        redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI"),
        scope=SCOPE,
        cache_path=".spotify_cache",
        open_browser=True,
    ))


def get_playlists(sp):
    playlists = []
    response = sp.current_user_playlists()
    while response:
        playlists.extend(response["items"])
        response = sp.next(response) if response["next"] else None
    return playlists


def get_playlist_tracks(sp, playlist_id):
    tracks = []
    response = sp.playlist_tracks(playlist_id)
    while response:
        for item in response["items"]:
            track = item.get("track")
            if track and track.get("name") and not track.get("is_local"):
                tracks.append({
                    "title": track["name"],
                    "artist": track["artists"][0]["name"] if track["artists"] else "",
                    "id": track["id"],
                })
        response = sp.next(response) if response["next"] else None
    return tracks


def search_track(sp, artist, title):
    a, t = normalize(artist), normalize(title)
    query = f'track:"{t}" artist:"{a}"'
    result = sp.search(q=query, type="track", limit=1)
    items = result["tracks"]["items"]
    if items:
        return items[0]["id"]

    result = sp.search(q=f"{a} {t}", type="track", limit=1)
    items = result["tracks"]["items"]
    return items[0]["id"] if items else None


def create_playlist(sp, name):
    user_id = sp.current_user()["id"]
    playlist = sp.user_playlist_create(user_id, name, public=False)
    return playlist["id"]


def add_tracks(sp, playlist_id, track_ids):
    for i in range(0, len(track_ids), 100):
        sp.playlist_add_items(playlist_id, track_ids[i:i + 100])
