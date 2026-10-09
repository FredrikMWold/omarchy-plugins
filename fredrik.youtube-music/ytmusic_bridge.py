#!/usr/bin/env python

import argparse
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

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
CONFIG_DIR = CONFIG_HOME / PLUGIN_ID
PLUGIN_DIR = Path(__file__).resolve().parent
CLIENT_PATH = CONFIG_DIR / "client.json"
OAUTH_PATH = CONFIG_DIR / "oauth.json"
BROWSER_PATH = CONFIG_DIR / "browser.json"
COOKIES_PATH = CONFIG_DIR / "cookies.txt"


def emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def private_file(path: Path) -> bool:
    if not path.is_file():
        return False
    return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def import_ready() -> bool:
    try:
        import ytmusicapi  # noqa: F401
    except ImportError:
        return False
    return True


def cookie_import_ready() -> bool:
    try:
        import browser_cookie3  # noqa: F401
    except ImportError:
        return False
    return True


def check() -> int:
    package_ready = import_ready()
    importer_ready = cookie_import_ready()
    chrome_ready = any(
        shutil.which(command)
        for command in ("google-chrome-stable", "google-chrome", "chromium")
    )
    playback_ready = all(
        (
            shutil.which("node"),
            chrome_ready,
            (PLUGIN_DIR / "node_modules/puppeteer-core/package.json").is_file(),
        )
    )
    browser_ready = private_file(BROWSER_PATH)
    browser_status = "Not connected"
    browser_detail = str(BROWSER_PATH)
    if browser_ready and package_ready:
        try:
            validate_browser_session()
            browser_status = "Connected"
        except (
            RequestException,
            YTMusicError,
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
        ):
            browser_ready = False
            browser_status = "Reconnect"
            browser_detail = "The saved YouTube Music session is no longer valid"
    checks = [
        {
            "id": "package",
            "label": "YouTube Music API",
            "detail": "python-ytmusicapi",
            "ready": package_ready,
            "status": "Ready" if package_ready else "Missing",
        },
        {
            "id": "importer",
            "label": "Chrome session importer",
            "detail": "python-browser-cookie3",
            "ready": importer_ready,
            "status": "Ready" if importer_ready else "Missing",
        },
        {
            "id": "playback",
            "label": "Browser audio player",
            "detail": "Chrome · Node.js · Puppeteer",
            "ready": playback_ready,
            "status": "Ready" if playback_ready else "Missing",
        },
        {
            "id": "browser",
            "label": "YouTube Music account",
            "detail": browser_detail,
            "ready": browser_ready,
            "status": browser_status,
        },
    ]
    emit(
        {
            "schema": 1,
            "ready": all(item["ready"] for item in checks),
            "checks": checks,
        }
    )
    return 0


def valid_cookie_domain(domain: str) -> bool:
    normalized = domain.lstrip(".").lower()
    host = "music.youtube.com"
    return normalized == host or host.endswith("." + normalized)


def browser_headers_from_chrome() -> dict[str, str]:
    try:
        import browser_cookie3
        from ytmusicapi.helpers import get_authorization, initialize_headers
    except ImportError as error:
        raise RuntimeError(
            "python-browser-cookie3 is not installed"
        ) from error

    try:
        cookie_jar = browser_cookie3.chrome(domain_name="youtube.com")
    except (
        browser_cookie3.BrowserCookieError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        KeyError,
        ValueError,
    ) as error:
        raise RuntimeError(
            "Could not read Chrome's YouTube cookies from the local keyring: "
            + str(error)
        ) from error

    cookies: dict[str, str] = {}
    for cookie in cookie_jar:
        domain = str(getattr(cookie, "domain", "") or "")
        name = str(getattr(cookie, "name", "") or "")
        value = str(getattr(cookie, "value", "") or "")
        if valid_cookie_domain(domain) and name and value:
            cookies[name] = value

    sapisid = cookies.get("__Secure-3PAPISID", "")
    if not sapisid:
        raise RuntimeError(
            "Chrome has no signed-in YouTube Music session. "
            "Open music.youtube.com, sign in, and run setup again."
        )

    origin = "https://music.youtube.com"
    headers = dict(initialize_headers())
    headers.update(
        {
            "X-Goog-AuthUser": "0",
            "x-origin": origin,
            "Authorization": get_authorization(f"{sapisid} {origin}"),
            "Cookie": "; ".join(
                f"{name}={value}" for name, value in sorted(cookies.items())
            ),
        }
    )
    return headers


def write_private_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=True, indent=2, sort_keys=True)
            output.write("\n")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_private_text(path: Path, payload: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(payload)
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_playback_cookies() -> None:
    with BROWSER_PATH.open(encoding="utf-8") as source:
        headers = json.load(source)
    raw_cookie = str(headers.get("Cookie") or headers.get("cookie") or "")
    parsed = SimpleCookie()
    parsed.load(raw_cookie.replace('"', ""))
    if "__Secure-3PAPISID" not in parsed:
        raise RuntimeError(
            "The browser session is missing the playback authentication cookie"
        )

    lines = ["# Netscape HTTP Cookie File"]
    for name in sorted(parsed):
        value = parsed[name].value
        if "\t" in name or "\n" in name or "\t" in value or "\n" in value:
            continue
        lines.append(
            "\t".join(
                (".youtube.com", "TRUE", "/", "TRUE", "0", name, value)
            )
        )
    write_private_text(COOKIES_PATH, "\n".join(lines) + "\n")


def validate_browser_session() -> None:
    from ytmusicapi import YTMusic

    YTMusic(str(BROWSER_PATH)).get_library_playlists(limit=1)


def setup_manual() -> int:
    from ytmusicapi.setup import setup as setup_browser

    print()
    print("Manual fallback")
    print("---------------")
    print("Copy authenticated request headers from a successful /browse POST")
    print("in the YouTube Music Network developer tools, paste them here,")
    print("then press Ctrl+D.")
    print()
    setup_browser(filepath=str(BROWSER_PATH))
    if not BROWSER_PATH.is_file():
        print("Setup completed without creating a browser session.", file=sys.stderr)
        return 1
    os.chmod(BROWSER_PATH, 0o600)
    return finish_setup("copied")


def finish_setup(source: str) -> int:
    print(f"Checking the {source} browser session...")
    try:
        validate_browser_session()
    except (RequestException, YTMusicError, OSError, ValueError) as error:
        BROWSER_PATH.unlink(missing_ok=True)
        print(f"Session check failed: {error}", file=sys.stderr)
        return 1

    try:
        write_playback_cookies()
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        COOKIES_PATH.unlink(missing_ok=True)
        print(f"Playback cookie setup failed: {error}", file=sys.stderr)
        return 1

    CLIENT_PATH.unlink(missing_ok=True)
    OAUTH_PATH.unlink(missing_ok=True)
    print()
    print("YouTube Music is connected. You can close this terminal.")
    return 0


def setup(manual: bool = False) -> int:
    try:
        import ytmusicapi  # noqa: F401
    except ImportError:
        print(
            "python-ytmusicapi is not installed. Run the plugin setup again.",
            file=sys.stderr,
        )
        return 1

    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)

    if manual:
        return setup_manual()

    print("YouTube Music automatic account setup")
    print("=====================================")
    print()
    print("Importing only YouTube cookies from your local Chrome profile.")
    print("Cookies never leave this computer.")
    print()
    try:
        write_private_json(BROWSER_PATH, browser_headers_from_chrome())
    except RuntimeError as error:
        print(f"Automatic import failed: {error}", file=sys.stderr)
        answer = input("Use manual request-header setup instead? [y/N] ").strip().lower()
        if answer in {"y", "yes"}:
            return setup_manual()
        return 1

    return finish_setup("imported")


def client():
    try:
        from ytmusicapi import YTMusic
    except ImportError as error:
        raise RuntimeError("python-ytmusicapi is not installed") from error

    if not BROWSER_PATH.is_file():
        raise RuntimeError("YouTube Music account setup is incomplete")
    return YTMusic(str(BROWSER_PATH))


def thumbnail(item: dict[str, Any]) -> str:
    thumbnails = item.get("thumbnails") or []
    if isinstance(thumbnails, list):
        for candidate in reversed(thumbnails):
            if isinstance(candidate, dict) and candidate.get("url"):
                return str(candidate["url"])
    return ""


def names(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("name") or value.get("artist") or "")
    if isinstance(value, list):
        result = []
        for entry in value:
            name = names(entry).strip()
            if name:
                result.append(name)
        return ", ".join(result)
    return ""


def user_facing_error(error: Exception) -> str:
    message = str(error)
    if (
        "twoColumnBrowseResultsRenderer" in message
        and ("Sign in" in message or "sign in" in message)
    ):
        return (
            "Your saved YouTube Music session has expired. "
            "Start account setup again to reconnect."
        )
    return message


def item_url(item: dict[str, Any]) -> str:
    video_id = item.get("videoId")
    if video_id:
        return f"https://music.youtube.com/watch?v={video_id}"
    playlist_id = item.get("playlistId")
    if playlist_id:
        return f"https://music.youtube.com/playlist?list={playlist_id}"
    browse_id = item.get("browseId")
    if browse_id:
        return f"https://music.youtube.com/browse/{browse_id}"
    return "https://music.youtube.com/"


def normalize(item: dict[str, Any], section: str, fallback_type: str) -> dict[str, Any]:
    result_type = str(item.get("resultType") or item.get("type") or fallback_type).lower()
    title = str(item.get("title") or item.get("artist") or item.get("name") or "Untitled")
    subtitle = names(item.get("artists") or item.get("artist") or item.get("author"))
    if not subtitle:
        subtitle = names(item.get("channel"))
    if not subtitle:
        subtitle = str(item.get("description") or "")
    metadata = (
        item.get("duration")
        or item.get("count")
        or item.get("itemCount")
        or item.get("year")
        or item.get("subscribers")
        or ""
    )
    return {
        "section": section,
        "type": result_type,
        "title": title,
        "subtitle": str(subtitle),
        "metadata": str(metadata),
        "thumbnail": thumbnail(item),
        "url": item_url(item),
    }


def get_speed_dial(ytmusic: Any) -> list[dict[str, Any]]:
    rows = ytmusic.get_home(limit=12)
    speed_dial_row = None
    for row in rows:
        title = str(row.get("title") or "").casefold()
        if title == "speed dial":
            speed_dial_row = row
            break
        if title == "listen again" and speed_dial_row is None:
            speed_dial_row = row

    if not speed_dial_row:
        return []
    return [
        normalize(item, "Speed dial", "recommendation")
        for item in speed_dial_row.get("contents", [])
        if isinstance(item, dict)
    ]


def library() -> int:
    ytmusic = client()
    speed_dial = get_speed_dial(ytmusic)
    playlists = [
        normalize(item, "Playlists", "playlist")
        for item in ytmusic.get_library_playlists(limit=50)
    ]
    liked_payload = ytmusic.get_liked_songs(limit=50)
    liked = [
        normalize(item, "Liked songs", "song")
        for item in liked_payload.get("tracks", [])
    ]
    albums = [
        normalize(item, "Albums", "album")
        for item in ytmusic.get_library_albums(limit=40)
    ]
    artists = [
        normalize(item, "Artists", "artist")
        for item in ytmusic.get_library_artists(limit=40)
    ]
    emit(
        {
            "schema": 1,
            "speedDial": speed_dial,
            "playlists": playlists,
            "library": liked + albums + artists,
        }
    )
    return 0


def search(query: str) -> int:
    query = query.strip()
    if not query:
        emit({"schema": 1, "query": "", "items": []})
        return 0
    results = client().search(query, limit=30)
    emit(
        {
            "schema": 1,
            "query": query,
            "items": [
                normalize(
                    item,
                    str(item.get("category") or item.get("resultType") or "Results"),
                    "result",
                )
                for item in results
            ],
        }
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="YouTube Music bridge for Omarchy")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")
    setup_parser = subparsers.add_parser("setup")
    setup_parser.add_argument(
        "--manual",
        action="store_true",
        help="paste browser request headers instead of importing Chrome cookies",
    )
    subparsers.add_parser("library")
    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("query")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "check":
            return check()
        if args.command == "setup":
            return setup(args.manual)
        if args.command == "library":
            return library()
        if args.command == "search":
            return search(args.query)
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        RequestException,
        YTMusicError,
    ) as error:
        print(user_facing_error(error), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
