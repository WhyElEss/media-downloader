"""The media library on the host: listing, previews, ZIP, delete. Every path
from the client goes through resolve_rel(), which refuses anything that
resolves outside the library root (../, absolute paths, symlinks out)."""
import asyncio
import hashlib
import io
import os
import time
import zipfile
from pathlib import Path

from .config import LIBRARY, THUMBS
from .engines import media_of_ext

PARTIAL_MARKERS = (".part", ".ytdl", ".temp", ".tmp")
_thumb_lock = asyncio.Semaphore(2)


def root() -> Path:
    return LIBRARY.resolve()


def resolve_rel(rel: str) -> Path:
    if not isinstance(rel, str) or not rel or "\x00" in rel or len(rel) > 4096:
        raise ValueError("Bad path")
    base = root()
    path = (base / rel.lstrip("/")).resolve()
    if path == base or not path.is_relative_to(base):
        raise ValueError("Path is outside the library")
    return path


def _is_partial(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(PARTIAL_MARKERS) or ".part-frag" in lower or ".temp." in lower


def scan() -> list[dict]:
    base = root()
    found = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith(".") or _is_partial(name):
                continue
            path = Path(dirpath) / name
            try:
                st = path.stat()
            except OSError:
                continue
            rel = path.relative_to(base)
            found.append({
                "path": str(rel),
                "name": name,
                "dir": str(rel.parent) if str(rel.parent) != "." else "",
                "platform": rel.parts[0] if len(rel.parts) > 1 else "",
                "kind": media_of_ext(path.suffix.lstrip(".")),
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
    found.sort(key=lambda f: f["mtime"], reverse=True)
    return found


def listing(platform: str, kind: str, q: str, offset: int, limit: int) -> dict:
    files = scan()
    platforms = sorted({f["platform"] for f in files if f["platform"]})
    if platform:
        files = [f for f in files if f["platform"] == platform]
    if kind:
        files = [f for f in files if f["kind"] == kind]
    if q:
        needle = q.lower()
        files = [f for f in files if needle in f["path"].lower()]
    return {"total": len(files), "platforms": platforms, "files": files[offset:offset + limit]}


async def thumbnail(path: Path) -> Path | None:
    """JPEG preview made by ffmpeg, cached by path + mtime + size."""
    st = path.stat()
    key = hashlib.sha1(f"{path}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()
    out = THUMBS / f"{key}.jpg"
    failed = THUMBS / f"{key}.none"
    if out.is_file():
        return out
    if failed.exists():
        return None
    kind = media_of_ext(path.suffix.lstrip("."))
    if kind == "video":
        attempts = [["-ss", "1", "-i", str(path)], ["-i", str(path)]]
    elif kind in ("image", "audio"):
        attempts = [["-i", str(path)]]  # audio: the embedded cover art, if any
    else:
        return None
    THUMBS.mkdir(parents=True, exist_ok=True)
    tmp = THUMBS / f"{key}.{os.getpid()}.tmp.jpg"
    async with _thumb_lock:
        for inputs in attempts:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-v", "error", "-y", *inputs, "-map", "0:v:0", "-frames:v", "1",
                "-vf", "scale=w='min(480,iw)':h=-2", "-q:v", "5", str(tmp),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                rc = await asyncio.wait_for(proc.wait(), 60)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                rc = -1
            if rc == 0 and tmp.is_file() and tmp.stat().st_size > 0:
                tmp.replace(out)
                return out
    tmp.unlink(missing_ok=True)
    failed.touch()
    return None


class _Sink(io.RawIOBase):
    """Write-only, non-seekable buffer: zipfile then streams with data descriptors."""

    def __init__(self):
        self.buffer = bytearray()

    def writable(self):
        return True

    def write(self, data):
        self.buffer += data
        return len(data)

    def take(self) -> bytes:
        data = bytes(self.buffer)
        self.buffer.clear()
        return data


def zip_stream(paths: list[Path]):
    """Yields a ZIP (stored, no compression: media is already compressed)."""
    base = root()
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path in paths:
            stamp = max(time.localtime(path.stat().st_mtime)[:6], (1980, 1, 1, 0, 0, 0))  # ZIP cannot store earlier dates
            info = zipfile.ZipInfo(str(path.relative_to(base)), stamp)
            info.compress_type = zipfile.ZIP_STORED
            with archive.open(info, "w", force_zip64=True) as dst, open(path, "rb") as src:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
                    yield sink.take()
            yield sink.take()
    yield sink.take()


def delete(rels: list[str]) -> tuple[int, list[str]]:
    base = root()
    deleted, errors = 0, []
    for rel in rels:
        try:
            path = resolve_rel(rel)
            if not path.is_file():
                raise ValueError("not a file")
            path.unlink()
            deleted += 1
        except (ValueError, OSError) as exc:
            errors.append(f"{rel}: {exc}")
            continue
        parent = path.parent
        while parent != base and parent.is_relative_to(base):
            try:
                parent.rmdir()  # only succeeds when empty
            except OSError:
                break
            parent = parent.parent
    return deleted, errors
