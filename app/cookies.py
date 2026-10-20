"""Per-platform cookies.txt (Netscape format) in the config volume."""
import asyncio
import json
import os
import re
import time
from pathlib import Path

from .config import COOKIES_DIR, ENGINES_BIN
from .engines import cookie_path, engine_env

PLATFORM_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
HEADER = "# Netscape HTTP Cookie File"
MAX_SIZE = 2 * 1024 * 1024
# Safari keeps every site's cookies in one file; a busy profile is a few MB.
MAX_SAFARI_SIZE = 20 * 1024 * 1024
SAFARI_MAGIC = b"cook"


def is_safari(raw: bytes) -> bool:
    return raw.startswith(SAFARI_MAGIC)


async def import_safari(platform: str, raw: bytes) -> dict:
    """Keep one platform's cookies from Safari's Cookies.binarycookies.

    yt-dlp's parser lives in the engines venv, so the import runs there as
    app.safari_cookies; the file goes through stdin and never touches disk."""
    if not PLATFORM_RE.match(platform or ""):
        raise ValueError("Platform must be lowercase letters, digits or '-', e.g. instagram.")
    if len(raw) > MAX_SAFARI_SIZE:
        raise ValueError("This Safari cookie file is larger than 20 MB.")
    proc = await asyncio.create_subprocess_exec(
        str(ENGINES_BIN / "python"), "-m", "app.safari_cookies", platform, "--json",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=str(Path(__file__).resolve().parent.parent), env=engine_env())
    try:
        out, err = await asyncio.wait_for(proc.communicate(raw), 60)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ValueError("Reading the Safari cookie file took longer than 60 s.")
    if proc.returncode != 0:
        raise ValueError(err.decode("utf-8", "replace").strip() or "Could not import Safari cookies.")
    return json.loads(out.decode().strip().splitlines()[-1])


def _entries(text: str) -> int:
    count = 0
    for line in text.splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#") or not line.strip():
            continue
        if len(line.split("\t")) == 7:
            count += 1
    return count


def list_all() -> list[dict]:
    result = []
    if not COOKIES_DIR.is_dir():
        return result
    for path in sorted(COOKIES_DIR.glob("*.txt")):
        try:
            text = path.read_text("utf-8", "replace")
        except OSError:
            continue
        result.append({"platform": path.stem, "entries": _entries(text), "updated": path.stat().st_mtime})
    return result


def save(platform: str, raw: bytes) -> int:
    if not PLATFORM_RE.match(platform or ""):
        raise ValueError("Platform must be lowercase letters, digits or '-', e.g. instagram.")
    if len(raw) > MAX_SIZE:
        raise ValueError("File is larger than 2 MB, that is not a cookies.txt.")
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    count = _entries(text)
    if count == 0:
        raise ValueError("No cookies found. Export cookies.txt in Netscape format (7 tab-separated columns).")
    if not text.startswith("# Netscape HTTP Cookie File") and not text.startswith("# HTTP Cookie File"):
        text = f"{HEADER}\n{text}"
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    target = cookie_path(platform)
    tmp = target.with_suffix(f".{int(time.time() * 1000)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    os.replace(tmp, target)
    return count


def remove(platform: str) -> bool:
    if not PLATFORM_RE.match(platform or ""):
        raise ValueError("Bad platform name")
    path = cookie_path(platform)
    if path.is_file():
        path.unlink()
        return True
    return False
