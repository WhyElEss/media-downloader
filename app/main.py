import asyncio
import hashlib
import logging
import mimetypes
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, cookies, db, engines, jobs, library
from .config import CONFIG, DATA, LIBRARY, THUMBS, TMP, USERNAME, WEB

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mediadl")


class _SkipHealthz(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(_SkipHealthz())

for ext, mime in {".m4a": "audio/mp4", ".opus": "audio/ogg", ".webp": "image/webp", ".mkv": "video/x-matroska",
                  ".webm": "video/webm", ".avif": "image/avif", ".heic": "image/heic", ".flac": "audio/flac"}.items():
    mimetypes.add_type(mime, ext)

runner = jobs.Runner()
limiter = auth.LoginLimiter()
http: httpx.AsyncClient | None = None

# Analysis results stay server-side; the client only gets an id, so the
# thumbnail proxy never fetches a URL chosen by the browser.
ANALYSES: dict[str, dict] = {}
ANALYSIS_TTL = 3 * 3600
REMOTE_THUMB_MAX = 10 * 1024 * 1024
_analyze_lock = asyncio.Semaphore(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http
    for path in (CONFIG, DATA, TMP, THUMBS):
        path.mkdir(parents=True, exist_ok=True)
    db.init()
    if not auth.password_configured():
        log.error("APP_PASSWORD_HASH is missing or invalid: nobody can sign in. "
                  "Generate one with: docker compose run --rm mediadl hashpw")
    http = httpx.AsyncClient(timeout=20, follow_redirects=True, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/126.0 Safari/537.36"})
    runner.start()
    log.info("library=%s", LIBRARY)
    yield
    await runner.stop()
    await http.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; "
       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")  # revalidate after updates
    return response


@app.exception_handler(ValueError)
async def value_error(request: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


def client_ip(request: Request) -> str:
    # Cloudflare appends to a client-supplied X-Forwarded-For but always
    # overwrites CF-Connecting-IP, so that one is the reliable visitor address.
    cf_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_ip:
        return cf_ip[:64]
    return request.client.host if request.client else "?"


# ------------------------------------------------------------------ auth

class LoginBody(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=1000)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/api/login")
async def login(body: LoginBody, request: Request):
    if not auth.password_configured():
        raise HTTPException(503, "Login is not configured: set APP_PASSWORD_HASH in .env (see README).")
    ip = client_ip(request)
    wait = limiter.retry_after(ip)
    if wait:
        raise HTTPException(429, f"Too many failed attempts. Try again in {wait // 60 + 1} min.")
    ok = await asyncio.to_thread(auth.check_credentials, body.username, body.password)
    if not ok:
        limiter.failed(ip)
        log.warning("failed login for %r from %s", body.username[:50], ip)
        await asyncio.sleep(1)
        raise HTTPException(401, "Wrong username or password.")
    limiter.succeeded(ip)
    token, csrf = auth.create_session()
    response = JSONResponse({"user": USERNAME, "csrf": csrf})
    response.set_cookie(auth.COOKIE, token, max_age=int(auth.config.SESSION_DAYS * 86400), httponly=True,
                        samesite="lax", secure=auth.cookie_secure(request), path="/")
    log.info("login from %s", ip)
    return response


@app.post("/api/logout")
async def logout(request: Request, session=Depends(auth.require_csrf)):
    auth.delete_session(request)
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE, path="/")
    return response


@app.get("/api/me")
async def me(session=Depends(auth.require_session)):
    return {"user": USERNAME, "csrf": session["csrf"]}


# --------------------------------------------------------------- analyze

class AnalyzeBody(BaseModel):
    url: str = Field(max_length=2048)
    engine: str = "auto"


def _prune_analyses() -> None:
    now = time.time()
    for key in [k for k, v in ANALYSES.items() if now - v["created"] > ANALYSIS_TTL]:
        ANALYSES.pop(key, None)
    while len(ANALYSES) > 100:
        ANALYSES.pop(next(iter(ANALYSES)))


def public_analysis(aid: str, result: dict) -> dict:
    return {
        "id": aid,
        "engine": result["engine"],
        "platform": result["platform"],
        "url": result["url"],
        "title": result.get("title"),
        "uploader": result.get("uploader"),
        "media": result["media"],
        "heights": result.get("heights", []),
        "heights_exact": result.get("heights_exact", False),
        "has_audio": result.get("has_audio", True),
        "note": result.get("note"),
        "fallback_error": result.get("fallback_error"),
        "has_cookies": engines.cookie_path(result["platform"]).is_file(),
        "entries": [{"idx": e["idx"], "title": e["title"], "media": e["media"], "duration": e.get("duration"),
                     "thumb": bool(e.get("thumb_url")), "error": e.get("error")} for e in result["entries"]],
    }


@app.post("/api/analyze")
async def analyze(body: AnalyzeBody, session=Depends(auth.require_csrf)):
    url = engines.validate_url(body.url)
    if body.engine not in ("auto", "yt-dlp", "gallery-dl"):
        raise HTTPException(400, "Unknown engine")
    async with _analyze_lock:
        try:
            result = await engines.analyze(url, body.engine)
        except engines.EngineError as exc:
            raise HTTPException(422, str(exc))
    _prune_analyses()
    aid = secrets.token_urlsafe(12)
    result["created"] = time.time()
    ANALYSES[aid] = result
    return public_analysis(aid, result)


@app.get("/api/analysis/{aid}/thumb/{idx}")
async def analysis_thumb(aid: str, idx: int, session=Depends(auth.require_session)):
    result = ANALYSES.get(aid)
    entry = next((e for e in result["entries"] if e["idx"] == idx), None) if result else None
    if not entry or not entry.get("thumb_url"):
        raise HTTPException(404, "No thumbnail")
    url = entry["thumb_url"]
    cache = THUMBS / "remote" / hashlib.sha1(url.encode()).hexdigest()
    if not cache.is_file():
        try:
            async with http.stream("GET", url) as upstream:
                if upstream.status_code != 200:
                    raise HTTPException(502, f"Thumbnail source answered {upstream.status_code}")
                ctype = upstream.headers.get("content-type", "")
                if not ctype.startswith("image/"):
                    raise HTTPException(502, "Thumbnail source did not return an image")
                data = bytearray()
                async for chunk in upstream.aiter_bytes():
                    data += chunk
                    if len(data) > REMOTE_THUMB_MAX:
                        raise HTTPException(502, "Thumbnail too large")
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Thumbnail fetch failed: {exc}")
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_bytes(bytes(data))
        tmp.replace(cache)
        (cache.with_suffix(".type")).write_text(ctype.split(";")[0])
    type_file = cache.with_suffix(".type")
    ctype = type_file.read_text() if type_file.is_file() else "image/jpeg"
    return FileResponse(cache, media_type=ctype, headers={"Cache-Control": "private, max-age=86400"})


# ------------------------------------------------------------------ jobs

class JobBody(BaseModel):
    analysis_id: str
    selected: list[int] = Field(max_length=2000)
    format: dict
    deliver: bool = False


def normalize_format(fmt: dict, result: dict) -> dict:
    if result["engine"] == "gallery-dl":
        return {"mode": "files"}
    mode = fmt.get("mode")
    if mode == "video":
        if result["media"] == "audio":
            raise ValueError("This link has no video.")
        height = fmt.get("height")
        height = int(height) if height else None
        if height is not None and not 100 <= height <= 8640:
            raise ValueError("Bad resolution")
        return {"mode": "video", "height": height, "compat": bool(fmt.get("compat"))}
    if mode in ("mp3", "m4a", "original") and result.get("has_audio") is False:
        raise ValueError("This video has no audio track, so there is nothing to extract.")
    if mode == "mp3":
        bitrate = int(fmt.get("bitrate") or 192)
        if bitrate not in engines.MP3_BITRATES:
            raise ValueError("Bad MP3 bitrate")
        return {"mode": "mp3", "bitrate": bitrate}
    if mode in ("m4a", "original"):
        return {"mode": mode}
    raise ValueError("Pick a format")


@app.post("/api/jobs")
async def create_job(body: JobBody, session=Depends(auth.require_csrf)):
    result = ANALYSES.get(body.analysis_id)
    if not result:
        raise HTTPException(410, "This analysis has expired, press Analyze again.")
    wanted = set(body.selected)
    entries = [e for e in result["entries"] if e["idx"] in wanted and not e.get("error")]
    if not entries:
        raise HTTPException(400, "Select at least one item.")
    fmt = normalize_format(body.format, result)
    job_id = jobs.create_job(result, entries, fmt, body.deliver)
    runner.kick()
    return {"id": job_id}


@app.get("/api/jobs")
async def list_jobs(session=Depends(auth.require_session)):
    return {"jobs": jobs.list_jobs()}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, session=Depends(auth.require_csrf)):
    runner.cancel(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: int, session=Depends(auth.require_csrf)):
    runner.retry(job_id)
    return {"ok": True}


@app.post("/api/jobs/clear")
async def clear_jobs(session=Depends(auth.require_csrf)):
    return {"removed": runner.clear_finished()}


def _existing(rels: list[str]) -> list[Path]:
    paths = []
    for rel in rels:
        try:
            path = library.resolve_rel(rel)
        except ValueError:
            continue
        if path.is_file() and path not in paths:
            paths.append(path)
    return paths


def _zip_response(paths: list[Path], name: str) -> StreamingResponse:
    return StreamingResponse(library.zip_stream(paths), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/jobs/{job_id}/zip")
async def job_zip(job_id: int, session=Depends(auth.require_session)):
    paths = _existing(jobs.job_files(job_id))
    if not paths:
        raise HTTPException(404, "This job has no files in the library.")
    return _zip_response(paths, f"mediadl-job-{job_id}.zip")


# --------------------------------------------------------------- library

@app.get("/api/library")
async def library_list(platform: str = "", kind: str = "", q: str = "", offset: int = 0, limit: int = 120,
                       session=Depends(auth.require_session)):
    limit = max(1, min(limit, 500))
    return await asyncio.to_thread(library.listing, platform, kind, q[:200], max(0, offset), limit)


@app.get("/api/file")
async def get_file(path: str, dl: int = 0, session=Depends(auth.require_session)):
    target = library.resolve_rel(path)
    if not target.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(target, filename=target.name, content_disposition_type="attachment" if dl else "inline")


@app.get("/api/thumb")
async def get_thumb(path: str, session=Depends(auth.require_session)):
    target = library.resolve_rel(path)
    if not target.is_file():
        raise HTTPException(404, "File not found")
    thumb = await library.thumbnail(target)
    if not thumb:
        raise HTTPException(404, "No preview")
    return FileResponse(thumb, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


class PathsBody(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=5000)


@app.post("/api/library/delete")
async def library_delete(body: PathsBody, session=Depends(auth.require_csrf)):
    deleted, errors = await asyncio.to_thread(library.delete, body.paths)
    log.info("deleted %d file(s)", deleted)
    return {"deleted": deleted, "errors": errors}


@app.post("/api/library/zip")
async def library_zip(request: Request, paths: list[str] = Form(...), csrf: str = Form(...),
                      session=Depends(auth.require_session)):
    # Submitted as a plain HTML form so the browser streams the file itself.
    auth.check_csrf(session, csrf)
    found = _existing(paths)
    if not found:
        raise HTTPException(404, "None of the selected files exist.")
    return _zip_response(found, f"mediadl-{time.strftime('%Y%m%d-%H%M%S')}.zip")


# --------------------------------------------------------------- cookies

@app.get("/api/cookies")
async def cookies_list(session=Depends(auth.require_session)):
    return {"cookies": cookies.list_all()}


@app.post("/api/cookies")
async def cookies_upload(platform: str = Form(...), file: UploadFile = File(...),
                         session=Depends(auth.require_csrf)):
    platform = platform.strip().lower()
    raw = await file.read(cookies.MAX_SAFARI_SIZE + 1)
    if cookies.is_safari(raw):
        result = await cookies.import_safari(platform, raw)
        log.info("cookies for %s imported from Safari (%d entries)", platform, result["entries"])
        return {**result, "source": "safari"}
    if len(raw) > cookies.MAX_SIZE:
        raise ValueError("File is larger than 2 MB, that is not a cookies.txt.")
    count = cookies.save(platform, raw)
    log.info("cookies for %s uploaded (%d entries)", platform, count)
    return {"platform": platform, "entries": count, "source": "cookies.txt"}


@app.delete("/api/cookies/{platform}")
async def cookies_delete(platform: str, session=Depends(auth.require_csrf)):
    if not cookies.remove(platform):
        raise HTTPException(404, "No cookies for this platform")
    return {"ok": True}


# --------------------------------------------------------------- engines

_update_lock = asyncio.Lock()


@app.get("/api/engines")
async def engines_info(session=Depends(auth.require_session)):
    return {"versions": await engines.versions(), "running": runner.running_count()}


@app.post("/api/engines/update")
async def engines_update(session=Depends(auth.require_csrf)):
    if _update_lock.locked():
        raise HTTPException(409, "An update is already running.")
    async with _update_lock:
        runner.paused = True
        try:
            for _ in range(60):  # wait up to 30 s for workers that just claimed an item
                if runner.running_count() == 0:
                    break
                await asyncio.sleep(0.5)
            if runner.running_count():
                raise HTTPException(409, "Downloads are running. Wait for them to finish, then update.")
            log.info("updating engines")
            rc, output = await engines.update_engines()
        finally:
            runner.paused = False
            runner.kick()
    return {"ok": rc == 0, "output": output, "versions": await engines.versions()}


# ----------------------------------------------------------------- pages

@app.get("/")
async def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=WEB), name="static")
