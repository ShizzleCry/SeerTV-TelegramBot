"""
Minimal Sonarr v3 API helper.

Overseerr can only request whole series or whole seasons, never a single
episode. Sonarr can: set one episode to "monitored" and trigger an
EpisodeSearch command for it. That is what this module does.
"""

import logging
import os
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

SONARR_URL = os.environ.get("SONARR_URL", "").rstrip("/")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")

HEADERS = {"X-Api-Key": SONARR_API_KEY, "Content-Type": "application/json"}


def is_configured() -> bool:
    return bool(SONARR_URL and SONARR_API_KEY)


class SonarrError(Exception):
    """Raised with a user-presentable message."""


def _get(path: str, params: dict | None = None):
    try:
        resp = requests.get(f"{SONARR_URL}/api/v3{path}", headers=HEADERS, params=params, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.exception("Sonarr GET %s failed", path)
        raise SonarrError(f"Kan Sonarr niet bereiken: {e}") from e


def _send(method: str, path: str, payload: dict):
    try:
        resp = requests.request(
            method, f"{SONARR_URL}/api/v3{path}", headers=HEADERS, json=payload, timeout=20
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}
    except requests.RequestException as e:
        logger.exception("Sonarr %s %s failed", method, path)
        raise SonarrError(f"Sonarr gaf een fout: {e}") from e


def find_series(title: str) -> list[dict]:
    """Series already in the Sonarr library whose title matches `title`.

    Only series that are already added to Sonarr can have episodes searched.
    If a show is not in Sonarr yet, the user should request it via the normal
    flow (Overseerr) first.
    """
    needle = title.casefold().strip()
    series = _get("/series")
    exact = [s for s in series if s.get("title", "").casefold() == needle]
    if exact:
        return exact
    return [
        s for s in series
        if needle in s.get("title", "").casefold()
        or any(needle in (alt.get("title", "") or "").casefold() for alt in s.get("alternateTitles", []))
    ]


def find_series_by_tmdb(tmdb_id: int) -> dict | None:
    for s in _get("/series"):
        if s.get("tmdbId") == tmdb_id:
            return s
    return None


def get_episodes(series_id: int) -> list[dict]:
    return _get("/episode", params={"seriesId": series_id})


def find_episode(series_id: int, season: int, episode: int) -> dict | None:
    for ep in get_episodes(series_id):
        if ep.get("seasonNumber") == season and ep.get("episodeNumber") == episode:
            return ep
    return None


def missing_episodes(series_id: int) -> list[dict]:
    """Episodes that have aired but have no file yet (specials excluded)."""
    from datetime import date

    today = date.today().isoformat()
    result = []
    for ep in get_episodes(series_id):
        if ep.get("seasonNumber", 0) == 0:
            continue
        if ep.get("hasFile"):
            continue
        air = ep.get("airDate")
        if air and air <= today:
            result.append(ep)
    result.sort(key=lambda e: (e["seasonNumber"], e["episodeNumber"]))
    return result


def search_episode(episode: dict) -> None:
    """Mark the episode monitored and kick off a release search for it."""
    if not episode.get("monitored"):
        episode = dict(episode)
        episode["monitored"] = True
        _send("PUT", f"/episode/{episode['id']}", episode)
    _send("POST", "/command", {"name": "EpisodeSearch", "episodeIds": [episode["id"]]})


def parse_episode_arg(text: str) -> tuple[str, int, int] | None:
    """Split 'Severance s4e7' / 'Severance S04E07' / 'Severance 4x7' into (title, season, episode)."""
    import re

    m = re.search(r"\b[sS](\d{1,2})\s*[eE](\d{1,3})\b|\b(\d{1,2})[xX](\d{1,3})\b", text)
    if not m:
        return None
    if m.group(1):
        season, episode = int(m.group(1)), int(m.group(2))
    else:
        season, episode = int(m.group(3)), int(m.group(4))
    title = (text[: m.start()] + " " + text[m.end():]).strip(" -:,")
    title = re.sub(r"\s+", " ", title)
    if not title:
        return None
    return title, season, episode
