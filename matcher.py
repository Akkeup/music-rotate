import re
import difflib


def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\(feat\..*?\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\(ft\..*?\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"feat\..*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\(.*?(remix|version|edit|remaster|live).*?\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def is_good_match(source_artist: str, source_title: str, found_artist: str, found_title: str, threshold: float = 0.6) -> bool:
    title_score = similarity(source_title, found_title)
    artist_score = similarity(source_artist, found_artist)
    return title_score >= threshold and artist_score >= threshold
