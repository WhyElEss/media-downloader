"""Download queue. A job is one "Download" click; each selected entry is an
item run by its own engine process. State lives in SQLite, so the queue
survives a container restart (items that were running are queued again)."""
import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

from . import db, engines
from .config import LIBRARY, MAX_CONCURRENT

log = logging.getLogger("mediadl.jobs")
ERROR_LIMIT = 64 * 1024


def format_label(fmt: dict) -> str:
    mode = fmt.get("mode")
    if mode == "video":
        return f"MP4 · up to {fmt['height']}p" if fmt.get("height") else "MP4 · best"
    if mode == "mp3":
        return f"MP3 · {fmt['bitrate']} kbps"
    if mode == "m4a":
        return "M4A"
    if mode == "original":
        return "Original audio"
    if mode == "files":
        return "Original files"
    return mode or "?"


def job_status(items: list[dict]) -> str:
    states = {i["status"] for i in items}
    if "running" in states:
        return "running"
    if "queued" in states:
        return "queued"
    if states == {"done"}:
        return "done"
    if "error" in states:
        return "partial" if "done" in states else "error"
    return "canceled"


def list_jobs(limit: int = 100) -> list[dict]:
    jobs = db.query("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))
    if not jobs:
        return []
    ids = [j["id"] for j in jobs]
    marks = ",".join("?" * len(ids))
    items = db.query(
        f"SELECT id, job_id, idx, title, status, progress, downloaded, total, files, error, started, finished "
        f"FROM items WHERE job_id IN ({marks}) ORDER BY job_id, idx", ids)
    by_job: dict[int, list] = {}
    for item in items:
        item["files"] = json.loads(item["files"])
        by_job.setdefault(item["job_id"], []).append(item)
    for job in jobs:
        job["items"] = by_job.get(job["id"], [])
        job["status"] = job_status(job["items"])
        job["format"] = format_label(json.loads(job["format"]))
        job["deliver"] = bool(job["deliver"])
    return jobs


def job_files(job_id: int) -> list[str]:
    files = []
    for row in db.query("SELECT files FROM items WHERE job_id = ? AND status = 'done' ORDER BY idx", (job_id,)):
        files += json.loads(row["files"])
    return files


def create_job(analysis: dict, entries: list[dict], fmt: dict, deliver: bool) -> int:
    def fn(conn):
        cur = conn.execute(
            "INSERT INTO jobs(created, url, engine, platform, title, format, deliver) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), analysis["url"], analysis["engine"], analysis["platform"],
             analysis.get("title"), json.dumps(fmt), int(deliver)))
        job_id = cur.lastrowid
        for entry in entries:
            spec = dict(entry["spec"], format=fmt)
            conn.execute("INSERT INTO items(job_id, idx, title, spec) VALUES (?, ?, ?, ?)",
                         (job_id, entry["idx"], entry["title"], json.dumps(spec)))
        return job_id
    return db.transaction(fn)


class Runner:
    def __init__(self):
        self.procs: dict[int, asyncio.subprocess.Process] = {}
        self.canceled: set[int] = set()
        self.wake = asyncio.Event()
        self.paused = False
        self.tasks: list[asyncio.Task] = []

    def start(self) -> None:
        requeued = db.execute(
            "UPDATE items SET status = 'queued', progress = 0, downloaded = NULL, total = NULL WHERE status = 'running'")
        log.info("queue started with %d worker(s)", MAX_CONCURRENT)
        for _ in range(MAX_CONCURRENT):
            self.tasks.append(asyncio.create_task(self._worker()))

    async def stop(self) -> None:
        for proc in list(self.procs.values()):
            engines.kill_tree(proc)
        for task in self.tasks:
            task.cancel()

    def kick(self) -> None:
        self.wake.set()

    def running_count(self) -> int:
        return len(self.procs)

    def _claim(self) -> dict | None:
        def fn(conn):
            row = conn.execute(
                "SELECT * FROM items WHERE status = 'queued' ORDER BY job_id, idx LIMIT 1").fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE items SET status = 'running', started = ?, finished = NULL, error = NULL, "
                "progress = 0, downloaded = NULL, total = NULL WHERE id = ?", (time.time(), row["id"]))
            return dict(row)
        return db.transaction(fn)

    async def _worker(self) -> None:
        while True:
            item = None if self.paused else self._claim()
            if item is None:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), 5)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self._run(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("item %s failed internally", item["id"])
                self._finish(item["id"], "error", error=f"Internal error: {exc!r}")
            finally:
                self.procs.pop(item["id"], None)
                self.canceled.discard(item["id"])

    def _finish(self, item_id: int, status: str, files=None, error=None) -> None:
        db.execute(
            "UPDATE items SET status = ?, finished = ?, error = ?, files = COALESCE(?, files), "
            "progress = CASE WHEN ? = 'done' THEN 100 ELSE progress END WHERE id = ?",
            (status, time.time(), error, json.dumps(files) if files is not None else None, status, item_id))

    async def _run(self, item: dict) -> None:
        item_id = item["id"]
        spec = json.loads(item["spec"])
        args = engines.build_args(spec, spec["format"])
        files: list[str] = []
        output: deque[str] = deque(maxlen=300)
        stderr: list[str] = []
        stderr_size = 0
        state = {"last": 0.0}
        root = LIBRARY.resolve()

        def add_file(raw: str) -> None:
            try:
                path = Path(raw.strip()).resolve()
                rel = str(path.relative_to(root))
            except (ValueError, OSError):
                return
            if path.is_file() and rel not in files:
                files.append(rel)

        def progress(parts: list[str]) -> None:
            def num(text):
                try:
                    return int(float(text))
                except (TypeError, ValueError):
                    return None
            done = num(parts[0]) if parts else None
            total = (num(parts[1]) if len(parts) > 1 else None) or (num(parts[2]) if len(parts) > 2 else None)
            now = time.monotonic()
            if now - state["last"] < 1 or done is None:
                return
            state["last"] = now
            pct = min(99.0, done * 100 / total) if total else 0
            db.execute("UPDATE items SET progress = ?, downloaded = ?, total = ? WHERE id = ?",
                       (pct, done, total, item_id))

        async def read_stdout(stream) -> None:
            async for raw in stream:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("@@P "):
                    progress(line[4:].split())
                elif line.startswith("@@F "):
                    add_file(line[4:])
                elif spec["engine"] == "gallery-dl" and (line.startswith("/") or line.startswith("# /")):
                    add_file(line[2:] if line.startswith("# ") else line)
                    db.execute("UPDATE items SET progress = ? WHERE id = ?",
                               (min(99.0, 10.0 * len(files)), item_id))
                elif line.strip():
                    output.append(line)

        async def read_stderr(stream) -> None:
            nonlocal stderr_size
            async for raw in stream:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.strip():
                    stderr.append(line)
                    stderr_size += len(line) + 1
                    while stderr_size > ERROR_LIMIT and len(stderr) > 1:
                        stderr_size -= len(stderr.pop(0)) + 1

        with engines.TempCookies(spec["platform"]) as cookie_args:
            full_args = engines.with_cookies(args, cookie_args)
            log.info("item %s: starting %s", item_id, spec["engine"])
            proc = await asyncio.create_subprocess_exec(
                *full_args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL, env=engines.engine_env(),
                start_new_session=True, limit=1024 * 1024)
            self.procs[item_id] = proc
            await asyncio.gather(read_stdout(proc.stdout), read_stderr(proc.stderr))
            rc = await proc.wait()

        if item_id in self.canceled:
            self._finish(item_id, "canceled", files=files)
        elif rc == 0 and files:
            self._finish(item_id, "done", files=files)
        else:
            details = "\n".join(stderr) or "\n".join(output)
            if rc == 0:
                message = "The engine finished but reported no saved files."
            else:
                message = f"{Path(full_args[0]).name} exited with code {rc}."
            self._finish(item_id, "error", files=files, error=f"{message}\n\n{details}".strip())
        log.info("item %s finished: rc=%s files=%d", item_id, rc, len(files))

    def cancel(self, job_id: int) -> None:
        db.execute("UPDATE items SET status = 'canceled', finished = ? WHERE job_id = ? AND status = 'queued'",
                   (time.time(), job_id))
        for row in db.query("SELECT id FROM items WHERE job_id = ? AND status = 'running'", (job_id,)):
            self.canceled.add(row["id"])
            proc = self.procs.get(row["id"])
            if proc:
                engines.kill_tree(proc)

    def retry(self, job_id: int) -> None:
        db.execute("UPDATE items SET status = 'queued', progress = 0, error = NULL, finished = NULL "
                   "WHERE job_id = ? AND status IN ('error', 'canceled')", (job_id,))
        self.kick()

    def clear_finished(self) -> int:
        active = "SELECT job_id FROM items WHERE status IN ('queued', 'running')"
        return db.transaction(lambda conn: conn.execute(
            f"DELETE FROM jobs WHERE id NOT IN ({active})").rowcount)
