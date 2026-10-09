#!/usr/bin/env python

import argparse
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

try:
    from requests import RequestException
except ImportError:
    class RequestException(Exception):
        pass

try:
    from ytmusicapi.exceptions import YTMusicError
except ImportError:
    class YTMusicError(Exception):
        pass


PLUGIN_ID = "fredrik.youtube-music"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
STATE_HOME = Path(
    os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
)
BROWSER_PATH = CONFIG_HOME / PLUGIN_ID / "browser.json"
STATE_DIR = STATE_HOME / PLUGIN_ID
QUEUE_CACHE_PATH = STATE_DIR / "queue-cache.json"
SOURCE_PATH = STATE_DIR / "source-url"
QUEUE_CACHE_SECONDS = 15 * 60
BROWSER_SERVER = "http://127.0.0.1:43829"


def validate_url(value: str) -> tuple[str, str]:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "music.youtube.com":
        raise ValueError("Only music.youtube.com playback URLs are allowed")
    if parsed.path not in {"/watch", "/playlist"} and not parsed.path.startswith(
        "/browse/"
    ):
        raise ValueError("This YouTube Music item is not directly playable")
    return parsed.path, parsed.query


def music_client():
    try:
        from ytmusicapi import YTMusic
    except ImportError as error:
        raise RuntimeError("python-ytmusicapi is not installed") from error

    if not BROWSER_PATH.is_file():
        raise RuntimeError("YouTube Music account setup is incomplete")
    return YTMusic(str(BROWSER_PATH))


def track_urls(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    urls = []
    for item in items:
        if not isinstance(item, dict):
            continue
        video_id = str(item.get("videoId") or "").strip()
        if video_id:
            urls.append(f"https://music.youtube.com/watch?v={video_id}")
    return urls


def cached_queue(url: str) -> list[str]:
    try:
        if time.time() - QUEUE_CACHE_PATH.stat().st_mtime > QUEUE_CACHE_SECONDS:
            return []
        payload = json.loads(QUEUE_CACHE_PATH.read_text(encoding="utf-8"))
        queue = payload.get(url) if isinstance(payload, dict) else None
        if not isinstance(queue, list):
            return []
        return [
            item for item in queue
            if isinstance(item, str) and item.startswith(
                "https://music.youtube.com/watch?v="
            )
        ]
    except (OSError, ValueError, json.JSONDecodeError):
        return []


def cache_queue(url: str, queue: list[str]) -> None:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: dict[str, Any] = {}
    try:
        existing = json.loads(QUEUE_CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            payload = existing
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    payload[url] = queue
    descriptor, temporary_name = tempfile.mkstemp(
        dir=STATE_DIR,
        prefix=".queue-cache.",
        suffix=".json",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, separators=(",", ":"))
        os.chmod(temporary, 0o600)
        temporary.replace(QUEUE_CACHE_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def build_queue(item_type: str, url: str, client: Any = None) -> list[str]:
    path, query = validate_url(url)
    if path == "/watch":
        return [url]

    queue = cached_queue(url)
    if queue:
        return queue

    ytmusic = client or music_client()
    if path == "/playlist":
        playlist_ids = parse_qs(query).get("list") or []
        playlist_id = playlist_ids[0] if playlist_ids else ""
        if not playlist_id:
            raise ValueError("The selected playlist has no playlist ID")
        if playlist_id.startswith("RD"):
            payload = ytmusic.get_watch_playlist(
                playlistId=playlist_id,
                limit=50,
            )
        else:
            payload = ytmusic.get_playlist(playlist_id, limit=None)
        queue = track_urls(payload.get("tracks") if isinstance(payload, dict) else None)
    elif item_type.lower() == "album":
        browse_id = path.removeprefix("/browse/").strip()
        if not browse_id:
            raise ValueError("The selected album has no browse ID")
        payload = ytmusic.get_album(browse_id)
        queue = track_urls(payload.get("tracks") if isinstance(payload, dict) else None)
    else:
        raise ValueError("This YouTube Music item is not directly playable")

    if not queue:
        raise RuntimeError("The selected item has no playable songs")
    cache_queue(url, queue)
    return queue


def inactive_status() -> dict[str, Any]:
    return {
        "schema": 1,
        "running": False,
        "playlistCount": 0,
        "playlistPosition": -1,
        "canGoPrevious": False,
        "canGoNext": False,
        "loopMode": "none",
        "position": 0,
        "duration": 0,
        "canSeek": False,
        "playing": False,
        "canTogglePlaying": False,
        "title": "",
        "artist": "",
        "album": "",
        "artUrl": "",
        "sourceUrl": read_source_url(),
    }


def read_source_url() -> str:
    try:
        return SOURCE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_source_url(url: str) -> None:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    SOURCE_PATH.write_text(url + "\n", encoding="utf-8")
    os.chmod(SOURCE_PATH, 0o600)


def browser_request(
    endpoint: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None
    headers = {}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
        method = "POST"
    try:
        with urlopen(
            Request(
                BROWSER_SERVER + endpoint,
                data=data,
                headers=headers,
                method=method,
            ),
            timeout=30,
        ) as response:
            result = json.load(response)
    except HTTPError as error:
        try:
            detail = json.load(error).get("error")
        except (AttributeError, ValueError, json.JSONDecodeError):
            detail = None
        raise RuntimeError(
            detail or f"The browser player returned HTTP {error.code}"
        ) from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError("The YouTube Music browser player is not ready") from error
    if not isinstance(result, dict):
        raise RuntimeError("The browser player returned invalid data")
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    return result


def contextual_play_url(source_url: str, queue: list[str]) -> str:
    path, query = validate_url(source_url)
    if path == "/watch":
        return source_url
    first = queue[0]
    if path == "/playlist":
        playlist_ids = parse_qs(query).get("list") or []
        if playlist_ids:
            return f"{first}&list={quote(playlist_ids[0], safe='')}"
    return first


def normalize_status(payload: dict[str, Any]) -> dict[str, Any]:
    count = max(0, int(payload.get("playlistCount") or 0))
    playlist_position = int(payload.get("playlistPosition", -1))
    return {
        "schema": 1,
        "running": payload.get("running") is True,
        "playing": payload.get("playing") is True,
        "playlistCount": count,
        "playlistPosition": playlist_position,
        "canGoPrevious": payload.get("canGoPrevious") is True,
        "canGoNext": payload.get("canGoNext") is True,
        "canTogglePlaying": payload.get("running") is True,
        "loopMode": (
            payload.get("loopMode")
            if payload.get("loopMode") in {"none", "playlist", "song"}
            else "none"
        ),
        "position": max(0, float(payload.get("position") or 0)),
        "duration": max(0, float(payload.get("duration") or 0)),
        "canSeek": payload.get("canSeek") is True,
        "title": str(payload.get("title") or ""),
        "artist": str(payload.get("artist") or ""),
        "album": str(payload.get("album") or ""),
        "artUrl": str(payload.get("artUrl") or ""),
        "sourceUrl": str(payload.get("sourceUrl") or read_source_url()),
    }


def playback_status() -> dict[str, Any]:
    try:
        return normalize_status(browser_request("/status"))
    except RuntimeError:
        return inactive_status()


def emit_status(payload: dict[str, Any] | None = None) -> None:
    print(
        json.dumps(
            normalize_status(payload) if payload is not None else playback_status(),
            separators=(",", ":"),
        )
    )


def play(item_type: str, url: str) -> int:
    queue = build_queue(item_type, url)
    payload = browser_request(
        "/play",
        {
            "url": contextual_play_url(url, queue),
            "sourceUrl": url,
            "queueVideoIds": [
                (parse_qs(urlparse(item).query).get("v") or [""])[0]
                for item in queue
            ],
        },
    )
    write_source_url(url)
    emit_status(payload)
    return 0


def control(action: str, value: str | None = None) -> int:
    if action == "seek":
        if value is None:
            raise ValueError("Seek requires a position")
        position = float(value)
        if not math.isfinite(position) or position < 0:
            raise ValueError("Seek position must be a positive number")
    else:
        position = None
    browser_action = "play-pause" if action == "playPause" else action
    browser_request(
        "/control",
        {"action": browser_action, "value": position},
    )
    emit_status(browser_request("/status"))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local YouTube Music player")
    subparsers = parser.add_subparsers(dest="command", required=True)
    play_parser = subparsers.add_parser("play")
    play_parser.add_argument("item_type")
    play_parser.add_argument("url")
    control_parser = subparsers.add_parser("control")
    control_parser.add_argument(
        "action",
        choices=(
            "previous",
            "next",
            "restart",
            "seek",
            "cycle-loop",
            "playPause",
        ),
    )
    control_parser.add_argument("value", nargs="?")
    subparsers.add_parser("status")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "play":
            return play(args.item_type, args.url)
        if args.command == "control":
            return control(args.action, args.value)
        if args.command == "status":
            emit_status()
            return 0
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        RequestException,
        YTMusicError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
