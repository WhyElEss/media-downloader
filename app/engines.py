"""yt-dlp / gallery-dl / ffmpeg glue. Engines always run as argument lists,
never through a shell, and every user URL is passed after "--"."""
import asyncio
import ipaddress
import json
import os
import re
import shutil
import signal
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from .config import COOKIES_DIR, DATA, ENGINES_BIN, ENGINES_REQUIREMENTS, LIBRARY, TMP

YTDLP = str(ENGINES_BIN / "yt-dlp")
GALLERY_DL = str(ENGINES_BIN / "gallery-dl")
PIP = str(ENGINES_BIN / "pip")

IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif", "avif", "heic", "heif", "bmp", "tif", "tiff", "jxl"}
VIDEO_EXT = {"mp4", "webm", "mkv", "mov", "m4v", "avi", "flv", "ts", "3gp"}
AUDIO_EXT = {"mp3", "m4a", "aac", "ogg", "opus", "flac", "wav", "wma", "alac"}

# host suffix -> platform folder name
PLATFORM_ALIASES = {
    "youtube.com": "youtube", "youtu.be": "youtube", "youtube-nocookie.com": "youtube",
    "instagram.com": "instagram", "instagr.am": "instagram",
    "x.com": "twitter", "twitter.com": "twitter", "fxtwitter.com": "twitter", "vxtwitter.com": "twitter",
    "tiktok.com": "tiktok", "soundcloud.com": "soundcloud",
    "facebook.com": "facebook", "fb.watch": "facebook",
    "vk.com": "vk", "vk.ru": "vk", "vkvideo.ru": "vk",
    "reddit.com": "reddit", "redd.it": "reddit",
    "bsky.app": "bluesky", "threads.net": "threads", "threads.com": "threads",
}
YTDLP_FIRST = {
    "youtube", "soundcloud", "tiktok", "vimeo", "twitch", "dailymotion", "bandcamp", "mixcloud",
    "rutube", "vk", "facebook", "ok", "bilibili", "rumble", "odysee", "streamable", "kick",
}
GALLERY_FIRST = {
    "pinterest", "imgur", "deviantart", "artstation", "flickr", "pixiv", "danbooru", "gelbooru",
    "tumblr", "bluesky", "kemono", "wallhaven", "behance", "weibo", "newgrounds", "furaffinity",
    "twitter", "reddit", "threads",
}
AUDIO_PLATFORMS = {"soundcloud", "bandcamp", "mixcloud"}
# yt-dlp fields naming the author, best first, per platform.
AUTHOR_FIELDS = {
    "instagram": "channel,uploader_id,uploader",
    "tiktok": "uploader,channel,uploader_id",
    "twitter": "uploader_id,uploader",
}
DEFAULT_AUTHOR_FIELDS = "uploader,channel,uploader_id,creator,artist"
STANDARD_HEIGHTS = [2160, 1440, 1080, 720, 480, 360]
MP3_BITRATES = (96, 128, 160, 192, 256, 320)


class EngineError(Exception):
    pass


def engine_env() -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{ENGINES_BIN}:{env.get('PATH', '')}"
    env["HOME"] = str(DATA / "home")
    env["XDG_CACHE_HOME"] = str(DATA / "cache")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def validate_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url or len(url) > 2048 or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("Enter a single http(s) link.")
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("Only http:// and https:// links are supported.")
    if host == "localhost" or host.endswith((".localhost", ".local", ".lan", ".internal")):
        raise ValueError("Links to local network hosts are not allowed.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and not ip.is_global:
        raise ValueError("Links to private or local IP addresses are not allowed.")
    return url


def platform_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    labels = host.split(".")
    for i in range(len(labels) - 1):
        name = PLATFORM_ALIASES.get(".".join(labels[i:]))
        if name:
            return name
    name = labels[-2] if len(labels) >= 2 else labels[0]
    return re.sub(r"[^a-z0-9-]", "", name) or "other"


def engine_order(url: str, platform: str) -> tuple[list[str], bool]:
    """Engines to try in order, and whether to check the content type
    (gallery-dl result made only of videos -> prefer yt-dlp)."""
    path = urlparse(url).path.lower()
    if platform == "instagram":
        if re.match(r"^/(reels?|tv)/", path) or "/reel/" in path:
            return ["yt-dlp", "gallery-dl"], False
        return ["gallery-dl", "yt-dlp"], True
    if platform in YTDLP_FIRST:
        return ["yt-dlp", "gallery-dl"], False
    if platform in GALLERY_FIRST:
        return ["gallery-dl", "yt-dlp"], True
    return ["yt-dlp", "gallery-dl"], False


def cookie_path(platform: str) -> Path:
    return COOKIES_DIR / f"{platform}.txt"


class TempCookies:
    """Engines write cookie jars back on exit, so each run gets a private copy."""

    def __init__(self, platform: str):
        self.platform = platform
        self.dir = None

    def __enter__(self) -> list[str]:
        src = cookie_path(self.platform)
        if not src.is_file():
            return []
        TMP.mkdir(parents=True, exist_ok=True)
        self.dir = tempfile.mkdtemp(dir=TMP, prefix="cookies-")
        dst = Path(self.dir) / "cookies.txt"
        shutil.copyfile(src, dst)
        return ["--cookies", str(dst)]

    def __exit__(self, *exc):
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)


def with_cookies(args: list[str], cookie_args: list[str]) -> list[str]:
    cut = args.index("--")
    return args[:cut] + cookie_args + args[cut:]


def kill_tree(proc: asyncio.subprocess.Process, sig=signal.SIGTERM) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def run_capture(args: list[str], timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL, env=engine_env(), start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        kill_tree(proc, signal.SIGKILL)
        await proc.wait()
        raise EngineError(f"{Path(args[0]).name} did not finish within {int(timeout)} s.")
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def safe_name(value, limit: int = 80) -> str:
    """One path component: keeps letters (any script), digits and a few
    punctuation marks; drops separators, braces and "$" (gallery-dl expands
    those in paths and formats)."""
    text = re.sub(r"[^\w .,@()\[\]+&!'#-]", "_", str(value or "")).strip(" ._")
    text = re.sub(r"_{2,}", "_", text)
    return text[:limit].rstrip(" ._") or "unknown"


def media_of_ext(ext: str) -> str:
    ext = (ext or "").lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return "other"


def summarize_media(entries: list[dict]) -> str:
    kinds = {e["media"] for e in entries}
    return kinds.pop() if len(kinds) == 1 else "mixed"


# ---------------------------------------------------------------- yt-dlp

def _ytdlp_thumb(info: dict) -> str | None:
    thumbs = [t for t in info.get("thumbnails") or [] if isinstance(t, dict) and t.get("url")]
    sized = [t for t in thumbs if (t.get("width") or 0) >= 320]
    if sized:
        return min(sized, key=lambda t: t["width"])["url"]
    if info.get("thumbnail"):
        return info["thumbnail"]
    return thumbs[-1]["url"] if thumbs else None


def _ytdlp_media(info: dict, platform: str) -> str:
    if platform in AUDIO_PLATFORMS:
        return "audio"
    formats = info.get("formats") or []
    if formats and all(f.get("vcodec") == "none" for f in formats):
        return "audio"
    if info.get("vcodec") == "none" and info.get("acodec") not in (None, "none"):
        return "audio"
    return "video"


def _heights(info: dict) -> set[int]:
    return {
        int(f["height"]) for f in info.get("formats") or []
        if f.get("vcodec") != "none" and isinstance(f.get("height"), (int, float)) and f["height"] > 0
    }


async def analyze_ytdlp(url: str, platform: str) -> dict:
    with TempCookies(platform) as cookie_args:
        rc, out, err = await run_capture(
            with_cookies([YTDLP, "-J", "--flat-playlist", "--", url], cookie_args), 240)
    if rc != 0 or not out.strip():
        raise EngineError(err.strip() or f"yt-dlp exited with code {rc} and no output.")
    try:
        info = json.loads(out)
    except json.JSONDecodeError:
        raise EngineError(err.strip() or "yt-dlp printed something that is not JSON.")

    title = info.get("title") or url
    uploader = info.get("uploader") or info.get("channel") or info.get("uploader_id")
    entries = []
    heights: set[int] = set()
    if info.get("_type") in ("playlist", "multi_video"):
        for pos, entry in enumerate(info.get("entries") or [], 1):
            if not isinstance(entry, dict):
                continue
            own = entry.get("webpage_url") or entry.get("url")
            # Entries without a page of their own (carousels) are picked by index.
            own_page = isinstance(own, str) and own.startswith("http") and own.rstrip("/") != url.rstrip("/")
            heights |= _heights(entry)
            entries.append({
                "idx": pos,
                "title": entry.get("title") or entry.get("id") or f"Item {pos}",
                "media": _ytdlp_media(entry, platform),
                "duration": entry.get("duration"),
                "thumb_url": _ytdlp_thumb(entry),
                "spec": {"engine": "yt-dlp", "url": own if own_page else url,
                         "playlist_item": None if own_page else pos, "platform": platform},
            })
        if not entries:
            raise EngineError("yt-dlp found a playlist with no entries:\n" + err.strip())
    else:
        heights = _heights(info)
        entries.append({
            "idx": 1,
            "title": title,
            "media": _ytdlp_media(info, platform),
            "duration": info.get("duration"),
            "thumb_url": _ytdlp_thumb(info),
            "spec": {"engine": "yt-dlp", "url": url, "playlist_item": None, "platform": platform},
        })
    media = summarize_media(entries)
    if media != "audio" and not heights:
        heights = set(STANDARD_HEIGHTS)
    return {
        "engine": "yt-dlp", "platform": platform, "url": url, "title": title,
        "uploader": uploader, "media": media, "entries": entries,
        "heights": sorted(heights, reverse=True) if media != "audio" else [],
        "heights_exact": info.get("_type") not in ("playlist",) and bool(_heights(info)),
    }


def ytdlp_args(spec: dict, fmt: dict) -> list[str]:
    platform = spec["platform"]
    author = f"%({AUTHOR_FIELDS.get(platform, DEFAULT_AUTHOR_FIELDS)}|unknown)s"
    template = f"{platform}/{author}/%(upload_date>%Y-%m-%d|undated)s_{author}_%(id)s.%(ext)s"
    args = [
        YTDLP, "--newline", "--progress", "--no-mtime", "--no-overwrites",
        "--progress-template",
        "download:@@P %(progress.downloaded_bytes)s %(progress.total_bytes)s %(progress.total_bytes_estimate)s",
        "--print", "after_move:@@F %(filepath)s",
        "-P", str(LIBRARY), "-o", template,
    ]
    if spec.get("playlist_item"):
        args += ["--yes-playlist", "--playlist-items", str(spec["playlist_item"])]
    else:
        args += ["--no-playlist"]

    mode = fmt["mode"]
    if mode == "video":
        height = fmt.get("height")
        if height:
            args += ["-f", f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b"]
        else:
            args += ["-f", "bv*+ba/b"]
        args += ["-S", "res,ext:mp4:m4a", "--merge-output-format", "mp4", "--remux-video", "mp4"]
    elif mode == "mp3":
        args += ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", f"{fmt['bitrate']}K",
                 "--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg"]
    elif mode == "m4a":
        args += ["-f", "ba[ext=m4a]/ba/b", "-x", "--audio-format", "m4a",
                 "--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg"]
    elif mode == "original":
        # -x without --audio-format keeps the source stream, no re-encoding.
        args += ["-f", "ba/b", "-x", "--embed-metadata"]
    else:
        raise ValueError(f"format {mode!r} does not apply to yt-dlp")
    return args + ["--", spec["url"]]


# ------------------------------------------------------------ gallery-dl

def _author(kw: dict) -> str:
    for key in ("username", "uploader", "author", "user", "artist", "owner", "creator", "blog", "channel", "account"):
        value = kw.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("username") or value.get("nick") or value.get("handle")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _date(kw: dict) -> str:
    for key in ("date", "post_date", "created_at", "published"):
        value = kw.get(key)
        if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value):
            return value[:10]
    return "undated"


def _post_id(kw: dict) -> str | None:
    for key in ("post_shortcode", "shortcode", "post_id", "tweet_id", "id", "filename"):
        value = kw.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    return None


async def analyze_gallery(url: str, platform: str) -> dict:
    with TempCookies(platform) as cookie_args:
        rc, out, err = await run_capture(
            with_cookies([GALLERY_DL, "-j", "--range", "1-500", "--", url], cookie_args), 240)
    try:
        messages = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        raise EngineError(err.strip() or out.strip()[-4000:] or f"gallery-dl exited with code {rc}.")

    files, queued, problems = [], [], []
    for msg in messages if isinstance(messages, list) else []:
        if not isinstance(msg, list) or not msg or not isinstance(msg[0], int):
            continue
        if msg[0] == 3 and len(msg) >= 3 and isinstance(msg[2], dict):
            files.append((msg[1], msg[2]))
        elif msg[0] == 6 and len(msg) >= 2:
            queued.append((msg[1], msg[2] if len(msg) > 2 and isinstance(msg[2], dict) else {}))
        elif msg[0] < 0:
            problems.append(json.dumps(msg[1], ensure_ascii=False) if len(msg) > 1 else str(msg))
    if not files and not queued:
        detail = "\n".join(x for x in [err.strip(), *problems] if x)
        raise EngineError(detail or "gallery-dl found nothing to download at this link.")

    entries = []
    post_counts = Counter(_post_id(kw) for _, kw in files)
    for pos, (file_url, kw) in enumerate(files, 1):
        ext = str(kw.get("extension") or Path(urlparse(str(file_url)).path).suffix.lstrip(".")).lower()
        media = "video" if str(file_url).startswith("ytdl:") else media_of_ext(ext)
        author = _author(kw)
        post = _post_id(kw) or str(pos)
        num = kw.get("num") if isinstance(kw.get("num"), int) else pos
        name = f"{_date(kw)}_{safe_name(author, 60)}_{safe_name(post, 60)}"
        if (kw.get("count") or 0) > 1 or post_counts[_post_id(kw)] > 1:
            name += f"_{num:02d}"
        thumb = kw.get("display_url") or kw.get("thumbnail")
        if not thumb and media == "image" and str(file_url).startswith("http"):
            thumb = file_url
        entries.append({
            "idx": pos,
            "title": str(kw.get("description") or kw.get("content") or kw.get("title") or name)[:120],
            "media": media,
            "duration": None,
            "thumb_url": thumb if isinstance(thumb, str) and thumb.startswith("http") else None,
            "author": author,
            "spec": {"engine": "gallery-dl", "url": url, "range": pos, "platform": platform,
                     "dir": f"{platform}/{safe_name(author)}", "filename": name},
        })
    for child_url, kw in queued:
        pos = len(entries) + 1
        author = _author(kw)
        entries.append({
            "idx": pos,
            "title": str(child_url)[:120],
            "media": "mixed",
            "duration": None,
            "thumb_url": None,
            "author": author,
            "spec": {"engine": "gallery-dl", "url": str(child_url), "range": None, "platform": platform,
                     "dir": f"{platform}/{safe_name(author)}", "filename": None},
        })

    authors = Counter(e["author"] for e in entries if e["author"] != "unknown")
    first_kw = files[0][1] if files else {}
    title = first_kw.get("description") or first_kw.get("content") or first_kw.get("title") or url
    return {
        "engine": "gallery-dl", "platform": platform, "url": url,
        "title": str(title).split("\n")[0][:200],
        "uploader": authors.most_common(1)[0][0] if authors else None,
        "media": summarize_media(entries), "entries": entries, "heights": [], "heights_exact": False,
    }


def gallery_args(spec: dict) -> list[str]:
    args = [GALLERY_DL, "--no-mtime", "-D", str(LIBRARY / spec["dir"])]
    if spec.get("range"):
        args += ["--range", str(spec["range"])]
    if spec.get("filename"):
        args += ["-f", spec["filename"] + ".{extension}"]
    return args + ["--", spec["url"]]


# ---------------------------------------------------------------- common

async def analyze(url: str, engine: str = "auto") -> dict:
    platform = platform_of(url)
    if engine in ("yt-dlp", "gallery-dl"):
        order, check = [engine], False
    else:
        order, check = engine_order(url, platform)
    runners = {"yt-dlp": analyze_ytdlp, "gallery-dl": analyze_gallery}
    errors = []
    for eng in order:
        try:
            result = await runners[eng](url, platform)
        except EngineError as exc:
            errors.append(f"[{eng}]\n{exc}")
            continue
        if check and eng == "gallery-dl" and result["media"] == "video" and "yt-dlp" in order:
            try:
                alt = await analyze_ytdlp(url, platform)
                alt["note"] = "Every item is a video, so yt-dlp was picked to allow format choice."
                return alt
            except EngineError as exc:
                result["note"] = "Every item is a video, but yt-dlp could not read this link, so gallery-dl is used."
                result["fallback_error"] = f"[yt-dlp]\n{exc}"
        if errors:
            result["note"] = f"{order[0]} could not read this link, so {eng} is used."
            result["fallback_error"] = errors[0]
        return result
    raise EngineError("\n\n".join(errors))


def build_args(spec: dict, fmt: dict) -> list[str]:
    if spec["engine"] == "yt-dlp":
        return ytdlp_args(spec, fmt)
    return gallery_args(spec)


async def versions() -> dict:
    async def first_line(args):
        try:
            rc, out, err = await run_capture(args, 30)
            return (out or err).strip().splitlines()[0] if (out or err).strip() else "?"
        except (EngineError, OSError) as exc:
            return f"unavailable ({exc})"
    names = ["yt-dlp", "gallery-dl", "ffmpeg", "deno"]
    results = await asyncio.gather(
        first_line([YTDLP, "--version"]),
        first_line([GALLERY_DL, "--version"]),
        first_line(["ffmpeg", "-hide_banner", "-version"]),
        first_line([str(ENGINES_BIN / "deno"), "--version"]),
    )
    return dict(zip(names, results))


async def update_engines() -> tuple[int, str]:
    rc, out, err = await run_capture([PIP, "install", "--upgrade", "-r", ENGINES_REQUIREMENTS], 900)
    return rc, (out + ("\n" + err if err.strip() else "")).strip()
