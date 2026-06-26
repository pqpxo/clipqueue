# version 8
"""ClipQueue: a local, persistent, FFmpeg-backed video cut queue."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

APP_TITLE = "ClipQueue"
APP_VERSION = "1.2.0"
SUPPORTED_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ts",
    ".webm",
}

# CRF 23 is a balanced H.264 quality level. If it would create a larger file,
# ClipQueue falls back to a two-pass bitrate encode. Two-pass H.264 prevents an
# overly strict one-pass VBV cap from starving the opening keyframe.
H264_CRF = "23"
H264_AUDIO_BITRATE = 128_000
DEFAULT_OUTPUT_SIZE_RATIO = 0.98
COMPATIBILITY_DECODE_SECONDS = 5.0

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = Path(os.getenv("APP_DATA", "/app/data")).resolve()
INPUT_ROOT = Path(os.getenv("INPUT_ROOT", "/media/input")).resolve()
OUTPUT_ROOT = Path(os.getenv("OUTPUT_ROOT", "/media/output")).resolve()
DB_PATH = DATA_DIR / "clipqueue.db"
THUMBNAIL_DIR = DATA_DIR / "thumbnails"
TEMP_DIR = DATA_DIR / "tmp"


class QueueJobCreate(BaseModel):
    """The section of source media that should be removed."""

    video_id: str
    cut_start: float = Field(ge=0, description="Cut starts at this second.")
    cut_end: float = Field(gt=0, description="Cut ends at this second.")
    delete_source_on_success: bool = Field(
        default=True,
        description="Delete the input video only after a non-empty output is safely saved.",
    )

    @model_validator(mode="after")
    def ensure_non_empty_cut(self) -> "QueueJobCreate":
        if self.cut_end <= self.cut_start:
            raise ValueError("Cut end must be later than cut start.")
        return self


class SkipUpdate(BaseModel):
    skipped: bool


class JobCancelledError(RuntimeError):
    """Raised when a user cancellation or a queue clear safely stops a job."""


class QueueWorker:
    """Claims one queued row at a time and performs a loss-safe FFmpeg edit."""

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="clipqueue-worker", daemon=True)
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancel_requests: set[str] = set()
        self._process_lock = threading.Lock()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._process_lock:
            for job_id, process in self._processes.items():
                self._cancel_requests.add(job_id)
                if process.poll() is None:
                    process.terminate()
        self._thread.join(timeout=10)

    def cancel_active_job(self, job_id: str) -> bool:
        """Request a safe stop, even if FFmpeg has not registered its process yet."""
        with self._process_lock:
            self._cancel_requests.add(job_id)
            process = self._processes.get(job_id)
            if process and process.poll() is None:
                process.terminate()
                return True
        return False

    def cancellation_requested(self, job_id: str) -> bool:
        with self._process_lock:
            return job_id in self._cancel_requests

    def finalise_if_not_cancelled(self, job_id: str, action: Any) -> None:
        """Run the irreversible save/delete phase only while the job is still wanted."""
        with self._process_lock:
            if job_id in self._cancel_requests:
                raise JobCancelledError("Cancelled by user.")
            action()

    def _clear_cancellation_request(self, job_id: str) -> None:
        with self._process_lock:
            self._cancel_requests.discard(job_id)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            job = claim_next_job()
            if not job:
                self._stop_event.wait(1.0)
                continue
            self._process_job(job)

    def _process_job(self, job: dict[str, Any]) -> None:
        job_id = str(job["id"])
        temp_path: Path | None = None
        fallback_path: Path | None = None
        try:
            source = safe_input_file(str(job["relative_path"]))
            if not source.is_file():
                raise FileNotFoundError("The source file is no longer present in the input folder.")

            source_size_bytes = source.stat().st_size
            probe = probe_media(source)
            duration = float(probe["duration"])
            cut_start = float(job["cut_start"])
            cut_end = float(job["cut_end"])
            validate_cut(cut_start, cut_end, duration)
            retained_duration = duration - (cut_end - cut_start)

            output = output_path_for_job(source, job_id)
            output.parent.mkdir(parents=True, exist_ok=True)
            temp_path = output.with_name(f".{output.stem}.working-{job_id[:8]}{output.suffix}")
            command = build_ffmpeg_command(
                source=source,
                output=temp_path,
                cut_start=cut_start,
                cut_end=cut_end,
                duration=duration,
                has_audio=bool(probe["has_audio"]),
            )

            # First pass: visual quality is controlled with H.264 CRF 23.
            run_ffmpeg_job(job_id, command, retained_duration)
            if not temp_path.exists() or temp_path.stat().st_size == 0:
                raise RuntimeError("FFmpeg exited without creating an output file.")

            size_limited = False
            size_guard_note: str | None = None
            if should_apply_size_guard(output.suffix.lower(), temp_path.stat().st_size, source_size_bytes):
                target_video_bitrate, target_audio_bitrate = target_bitrates_for_size(
                    source_size_bytes=source_size_bytes,
                    retained_duration=retained_duration,
                    has_audio=bool(probe["has_audio"]),
                    source_audio_bitrate=int(probe.get("audio_bitrate") or 0),
                )
                fallback_path = output.with_name(f".{output.stem}.size-guard-{job_id[:8]}{output.suffix}")
                passlog_prefix = TEMP_DIR / f"two-pass-{job_id[:8]}"
                try:
                    # A two-pass encode can allocate enough bits to the opening IDR frame. The
                    # previous one-pass VBV cap could make the first frames look blocky, even
                    # though the rest of the file played normally.
                    cleanup_two_pass_logs(passlog_prefix)
                    update_job(job_id, progress=0)
                    first_pass_command = build_ffmpeg_command(
                        source=source,
                        output=fallback_path,
                        cut_start=cut_start,
                        cut_end=cut_end,
                        duration=duration,
                        has_audio=bool(probe["has_audio"]),
                        target_video_bitrate=target_video_bitrate,
                        target_audio_bitrate=target_audio_bitrate,
                        pass_number=1,
                        passlog_prefix=passlog_prefix,
                    )
                    run_ffmpeg_job(
                        job_id,
                        first_pass_command,
                        retained_duration,
                        progress_start=0,
                        progress_end=47,
                    )
                    second_pass_command = build_ffmpeg_command(
                        source=source,
                        output=fallback_path,
                        cut_start=cut_start,
                        cut_end=cut_end,
                        duration=duration,
                        has_audio=bool(probe["has_audio"]),
                        target_video_bitrate=target_video_bitrate,
                        target_audio_bitrate=target_audio_bitrate,
                        pass_number=2,
                        passlog_prefix=passlog_prefix,
                    )
                    run_ffmpeg_job(
                        job_id,
                        second_pass_command,
                        retained_duration,
                        progress_start=48,
                        progress_end=99,
                    )
                    if not fallback_path.exists() or fallback_path.stat().st_size == 0:
                        raise RuntimeError("The two-pass size guard did not create an output file.")
                    if fallback_path.stat().st_size < temp_path.stat().st_size:
                        temp_path.unlink(missing_ok=True)
                        fallback_path.replace(temp_path)
                        size_limited = True
                        size_guard_note = "A two-pass H.264 size guard was used to keep the output compact without starving the opening keyframe."
                    else:
                        size_guard_note = "The two-pass size guard did not produce a smaller result, so the CRF 23 output was kept."
                except Exception as exc:
                    # Keep the successful CRF encode rather than losing a completed edit because
                    # the optional size guard failed.
                    if is_job_cancelled(job_id):
                        raise
                    size_guard_note = f"The size guard could not run: {human_error(exc)}"
                finally:
                    cleanup_two_pass_logs(passlog_prefix)
                    if fallback_path and fallback_path.exists():
                        fallback_path.unlink(missing_ok=True)

            validate_export(temp_path, retained_duration)

            source_removed_at: str | None = None
            source_cleanup_error: str | None = None

            def finalise_output() -> None:
                nonlocal source_removed_at, source_cleanup_error
                temp_path.replace(output)
                if bool(job.get("delete_source_on_success")):
                    try:
                        delete_source_after_success(source, str(job["video_id"]))
                        source_removed_at = utc_now()
                    except Exception as exc:
                        # The output is already safely stored; make a cleanup failure visible without
                        # turning an otherwise successful job into a failed one.
                        source_cleanup_error = human_error(exc)

            # A queue clear/cancel can arrive after FFmpeg finishes. Keep output publication and
            # optional source deletion together behind the worker lock, so a cancellation that wins
            # before finalisation cannot leave a new output or delete the source.
            worker.finalise_if_not_cancelled(job_id, finalise_output)
            output_size_bytes = output.stat().st_size

            update_job(
                job_id,
                status="complete",
                progress=100,
                output_relative=str(output.relative_to(OUTPUT_ROOT)),
                source_size_bytes=source_size_bytes,
                output_size_bytes=output_size_bytes,
                size_limited=int(size_limited),
                size_guard_note=size_guard_note,
                source_removed_at=source_removed_at,
                source_cleanup_error=source_cleanup_error,
                completed_at=utc_now(),
                error=None,
            )
        except Exception as exc:  # Make a failed row visible rather than killing the worker.
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)
            if fallback_path and fallback_path.exists():
                fallback_path.unlink(missing_ok=True)
            if not isinstance(exc, JobCancelledError) and not worker.cancellation_requested(job_id) and not is_job_cancelled(job_id):
                update_job(
                    job_id,
                    status="failed",
                    error=human_error(exc),
                    completed_at=utc_now(),
                )
        finally:
            with self._process_lock:
                self._processes.pop(job_id, None)
            self._clear_cancellation_request(job_id)

    def _register_process(self, job_id: str, process: subprocess.Popen[str]) -> None:
        with self._process_lock:
            self._processes[job_id] = process


worker = QueueWorker()
app = FastAPI(title=APP_TITLE, version=APP_VERSION, docs_url=None, redoc_url=None)
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.on_event("startup")
def on_startup() -> None:
    ensure_directories()
    initialise_database()
    recover_interrupted_jobs()
    scan_input_library()
    worker.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    worker.stop()


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "application": APP_TITLE,
        "version": APP_VERSION,
        "input_root": str(INPUT_ROOT),
        "output_root": str(OUTPUT_ROOT),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "h264_crf": H264_CRF,
        "input_writable": is_directory_writable(INPUT_ROOT),
    }


@app.get("/api/videos")
def list_videos(include_skipped: bool = Query(False)) -> dict[str, Any]:
    scan_input_library()
    with db_connection() as connection:
        query = "SELECT * FROM videos"
        params: list[Any] = []
        if not include_skipped:
            query += " WHERE skipped = 0"
        query += " ORDER BY relative_path COLLATE NOCASE"
        rows = connection.execute(query, params).fetchall()
    return {"videos": [serialise_video(row) for row in rows]}


@app.post("/api/videos/rescan")
def rescan_videos() -> dict[str, Any]:
    return scan_input_library()


@app.get("/api/videos/{video_id}/stream")
def stream_video(video_id: str) -> FileResponse:
    video = get_video_or_404(video_id)
    file_path = safe_input_file(str(video["relative_path"]))
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="The source video is no longer present.")
    media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    return FileResponse(file_path, media_type=media_type, filename=file_path.name)


@app.get("/api/videos/{video_id}/thumbnail")
def video_thumbnail(video_id: str) -> FileResponse:
    video = get_video_or_404(video_id)
    source = safe_input_file(str(video["relative_path"]))
    if not source.is_file():
        raise HTTPException(status_code=404, detail="The source video is no longer present.")

    thumbnail = THUMBNAIL_DIR / f"{video_id}.jpg"
    source_mtime = int(source.stat().st_mtime)
    if not thumbnail.exists() or thumbnail.stat().st_mtime < source_mtime:
        generate_thumbnail(source, thumbnail, float(video["duration"] or 0))

    if not thumbnail.exists():
        raise HTTPException(status_code=404, detail="A thumbnail could not be generated for this video.")
    return FileResponse(thumbnail, media_type="image/jpeg")


@app.post("/api/videos/{video_id}/skip")
def set_skip_state(video_id: str, update: SkipUpdate) -> dict[str, Any]:
    get_video_or_404(video_id)
    with db_connection() as connection:
        connection.execute("UPDATE videos SET skipped = ? WHERE id = ?", (int(update.skipped), video_id))
    return {"ok": True, "video_id": video_id, "skipped": update.skipped}


@app.delete("/api/videos/{video_id}")
def delete_video(video_id: str) -> dict[str, Any]:
    """Delete one source video from the input folder after confirming it is not active."""
    video = get_video_or_404(video_id)
    with db_connection() as connection:
        active_job = connection.execute(
            "SELECT id FROM jobs WHERE video_id = ? AND status IN ('queued', 'processing') LIMIT 1",
            (video_id,),
        ).fetchone()
    if active_job:
        raise HTTPException(
            status_code=409,
            detail="This video is queued or processing and cannot be deleted until that job is finished or cancelled.",
        )

    source = safe_input_file(str(video["relative_path"]))
    try:
        if source.exists():
            if not source.is_file():
                raise RuntimeError("The selected input path is not a regular file.")
            source.unlink()
            prune_empty_input_directories(source.parent)
    except PermissionError as exc:
        raise HTTPException(
            status_code=503,
            detail="ClipQueue cannot delete the source file because the input folder is not writable. "
            "Confirm that INPUT_DIR is writable by the configured host user.",
        ) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not delete the source file: {human_error(exc)}") from exc

    remove_video_record(video_id)
    return {"ok": True, "video_id": video_id, "filename": str(video["filename"])}


@app.post("/api/jobs", status_code=201)
def create_job(payload: QueueJobCreate) -> dict[str, Any]:
    video = get_video_or_404(payload.video_id)
    duration = float(video["duration"] or 0)
    validate_cut(payload.cut_start, payload.cut_end, duration)

    job_id = str(uuid.uuid4())
    now = utc_now()
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO jobs (
                id, video_id, relative_path, source_filename, cut_start, cut_end,
                delete_source_on_success, status, progress, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?)
            """,
            (
                job_id,
                payload.video_id,
                video["relative_path"],
                video["filename"],
                payload.cut_start,
                payload.cut_end,
                int(payload.delete_source_on_success),
                now,
            ),
        )
        # Hide the source from the default review flow while its requested edit is queued.
        connection.execute("UPDATE videos SET skipped = 1 WHERE id = ?", (payload.video_id,))
    return {"job": get_job(job_id)}


@app.get("/api/jobs")
def list_jobs(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    with db_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM jobs ORDER BY CASE status WHEN 'processing' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {"jobs": [serialise_job(row) for row in rows]}


@app.post("/api/jobs/clear")
def clear_queue() -> dict[str, Any]:
    """Cancel active work and remove all queue records without deleting media files."""
    with db_connection() as connection:
        # This serialises with claim_next_job(), so a queued row cannot be claimed halfway
        # through a clear operation. Jobs already claimed as processing receive an in-memory
        # cancellation request before their database rows are removed.
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute("SELECT id, video_id, status FROM jobs").fetchall()
        if not rows:
            return {"ok": True, "cleared": 0, "cancelled_processing": 0, "restored_videos": 0}

        processing_ids = [str(row["id"]) for row in rows if row["status"] == "processing"]
        video_ids = sorted({str(row["video_id"]) for row in rows if row["video_id"]})
        for job_id in processing_ids:
            worker.cancel_active_job(job_id)

        connection.execute("DELETE FROM jobs")
        restored_videos = 0
        if video_ids:
            placeholders = ", ".join("?" for _ in video_ids)
            cursor = connection.execute(
                f"UPDATE videos SET skipped = 0 WHERE id IN ({placeholders})",
                video_ids,
            )
            restored_videos = max(0, cursor.rowcount)

    return {
        "ok": True,
        "cleared": len(rows),
        "cancelled_processing": len(processing_ids),
        "restored_videos": restored_videos,
    }


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    job = get_job(job_id)
    if job["status"] == "complete":
        raise HTTPException(status_code=409, detail="Completed jobs cannot be cancelled.")

    if job["status"] == "processing":
        worker.cancel_active_job(job_id)
    with db_connection() as connection:
        connection.execute(
            "UPDATE jobs SET status = 'cancelled', completed_at = ?, error = NULL WHERE id = ? AND status IN ('queued', 'processing')",
            (utc_now(), job_id),
        )
    return {"ok": True, "job": get_job(job_id)}


@app.exception_handler(HTTPException)
def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@contextmanager
def db_connection() -> Any:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def ensure_writable_directory(directory: Path, label: str) -> None:
    """Create a working directory and fail with an actionable mount-permission error."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".clipqueue-write-test-{os.getpid()}"
        probe.touch(exist_ok=False)
        probe.unlink(missing_ok=True)
    except PermissionError as exc:
        raise RuntimeError(
            f"ClipQueue cannot write to the {label}: {directory}. "
            "Ensure the host folder is writable and that PUID/PGID in .env match `id -u` and `id -g`."
        ) from exc


def ensure_directories() -> None:
    ensure_writable_directory(DATA_DIR, "application data folder")
    ensure_writable_directory(THUMBNAIL_DIR, "thumbnail cache")
    ensure_writable_directory(TEMP_DIR, "temporary work folder")
    if not INPUT_ROOT.exists() or not INPUT_ROOT.is_dir():
        raise RuntimeError(f"The mounted input folder does not exist: {INPUT_ROOT}")
    ensure_writable_directory(OUTPUT_ROOT, "output folder")


def initialise_database() -> None:
    with db_connection() as connection:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY,
                relative_path TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                extension TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                modified_at REAL NOT NULL,
                duration REAL NOT NULL DEFAULT 0,
                width INTEGER,
                height INTEGER,
                video_codec TEXT,
                audio_codec TEXT,
                skipped INTEGER NOT NULL DEFAULT 0,
                indexed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                source_filename TEXT NOT NULL,
                cut_start REAL NOT NULL,
                cut_end REAL NOT NULL,
                delete_source_on_success INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL CHECK(status IN ('queued', 'processing', 'complete', 'failed', 'cancelled')),
                progress REAL NOT NULL DEFAULT 0,
                output_relative TEXT,
                source_size_bytes INTEGER,
                output_size_bytes INTEGER,
                size_limited INTEGER NOT NULL DEFAULT 0,
                size_guard_note TEXT,
                source_removed_at TEXT,
                source_cleanup_error TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_videos_relative_path ON videos(relative_path);
            """
        )
        ensure_jobs_columns(connection)


def ensure_jobs_columns(connection: sqlite3.Connection) -> None:
    """Upgrade older v1.0.x databases without dropping their job history."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
    required_columns = {
        "delete_source_on_success": "INTEGER NOT NULL DEFAULT 1",
        "source_size_bytes": "INTEGER",
        "output_size_bytes": "INTEGER",
        "size_limited": "INTEGER NOT NULL DEFAULT 0",
        "size_guard_note": "TEXT",
        "source_removed_at": "TEXT",
        "source_cleanup_error": "TEXT",
    }
    for name, definition in required_columns.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")


def recover_interrupted_jobs() -> None:
    """Return in-flight work to the queue after a container restart."""
    with db_connection() as connection:
        connection.execute(
            "UPDATE jobs SET status = 'queued', progress = 0, started_at = NULL, error = 'Recovered after application restart.' WHERE status = 'processing'"
        )


def scan_input_library() -> dict[str, Any]:
    ensure_directories()
    discovered = 0
    updated = 0
    seen_paths: set[str] = set()
    ignored: list[dict[str, str]] = []

    for candidate in INPUT_ROOT.rglob("*"):
        if not candidate.is_file() or candidate.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue

        relative_path = candidate.relative_to(INPUT_ROOT).as_posix()
        seen_paths.add(relative_path)
        try:
            stat = candidate.stat()
            video_id = stable_id(relative_path)
            with db_connection() as connection:
                existing = connection.execute(
                    "SELECT size_bytes, modified_at FROM videos WHERE id = ?", (video_id,)
                ).fetchone()

            needs_probe = (
                existing is None
                or int(existing["size_bytes"]) != stat.st_size
                or float(existing["modified_at"]) != stat.st_mtime
            )
            if needs_probe:
                metadata = probe_media(candidate)
                with db_connection() as connection:
                    connection.execute(
                        """
                        INSERT INTO videos (
                            id, relative_path, filename, extension, size_bytes, modified_at,
                            duration, width, height, video_codec, audio_codec, indexed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            relative_path=excluded.relative_path,
                            filename=excluded.filename,
                            extension=excluded.extension,
                            size_bytes=excluded.size_bytes,
                            modified_at=excluded.modified_at,
                            duration=excluded.duration,
                            width=excluded.width,
                            height=excluded.height,
                            video_codec=excluded.video_codec,
                            audio_codec=excluded.audio_codec,
                            indexed_at=excluded.indexed_at
                        """,
                        (
                            video_id,
                            relative_path,
                            candidate.name,
                            candidate.suffix.lower(),
                            stat.st_size,
                            stat.st_mtime,
                            metadata["duration"],
                            metadata["width"],
                            metadata["height"],
                            metadata["video_codec"],
                            metadata["audio_codec"],
                            utc_now(),
                        ),
                    )
                if existing is None:
                    discovered += 1
                else:
                    updated += 1
        except Exception as exc:
            # Keep scanning other media but show the exact reason in the UI and container logs.
            message = human_error(exc)
            issue = {"path": relative_path, "error": message}
            ignored.append(issue)
            print(f"[ClipQueue] Could not index {relative_path!r}: {message}", flush=True)

    with db_connection() as connection:
        known = connection.execute("SELECT id, relative_path FROM videos").fetchall()
        for row in known:
            if row["relative_path"] not in seen_paths:
                connection.execute("DELETE FROM videos WHERE id = ?", (row["id"],))
                (THUMBNAIL_DIR / f"{row['id']}.jpg").unlink(missing_ok=True)
        indexed = connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0]

    return {
        "ok": True,
        "discovered": discovered,
        "updated": updated,
        "total": len(seen_paths),
        "indexed": int(indexed),
        "ignored": ignored,
    }


def probe_media(source: Path) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,bit_rate:stream=codec_type,codec_name,profile,pix_fmt,width,height,duration,duration_ts,time_base,bit_rate",
        "-of",
        "json",
        str(source),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=90)
    info = json.loads(result.stdout)
    streams = info.get("streams", [])
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not video_stream:
        raise ValueError("No video stream was found.")

    duration = number_or_zero(info.get("format", {}).get("duration"))
    if duration <= 0:
        duration = stream_duration(video_stream)
    if duration <= 0 and audio_stream:
        duration = stream_duration(audio_stream)
    if duration <= 0:
        raise ValueError(
            "The video duration could not be determined. The file may be incomplete, "
            "damaged, or a raw H.264 stream renamed with an .mp4 extension."
        )
    return {
        "duration": round(duration, 3),
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "video_codec": video_stream.get("codec_name") or "unknown",
        "video_profile": video_stream.get("profile") or None,
        "pixel_format": video_stream.get("pix_fmt") or None,
        "audio_codec": audio_stream.get("codec_name") if audio_stream else None,
        "has_audio": audio_stream is not None,
        "format_bitrate": int(number_or_zero(info.get("format", {}).get("bit_rate"))),
        "video_bitrate": int(number_or_zero(video_stream.get("bit_rate"))),
        "audio_bitrate": int(number_or_zero(audio_stream.get("bit_rate"))) if audio_stream else 0,
    }


def number_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def stream_duration(stream: dict[str, Any]) -> float:
    duration = number_or_zero(stream.get("duration"))
    if duration > 0:
        return duration

    duration_ts = number_or_zero(stream.get("duration_ts"))
    time_base = str(stream.get("time_base") or "")
    if duration_ts <= 0 or "/" not in time_base:
        return 0.0
    try:
        numerator, denominator = time_base.split("/", 1)
        return duration_ts * (float(numerator) / float(denominator))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def generate_thumbnail(source: Path, output: Path, duration: float) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    seconds = max(0.1, min(duration * 0.12, 12.0))
    temporary = output.with_suffix(".tmp.jpg")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{seconds:.3f}",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-vf",
        "scale='min(720,iw)':-2",
        "-q:v",
        "4",
        str(temporary),
    ]
    try:
        subprocess.run(command, check=True, timeout=90)
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)


def claim_next_job() -> dict[str, Any] | None:
    with db_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        connection.execute(
            "UPDATE jobs SET status = 'processing', started_at = ?, progress = 0 WHERE id = ?",
            (utc_now(), row["id"]),
        )
        claimed = connection.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
    return dict(claimed) if claimed else None


def run_ffmpeg_job(
    job_id: str,
    command: list[str],
    retained_duration: float,
    progress_start: float = 0,
    progress_end: float = 99,
) -> None:
    """Run FFmpeg while surfacing concise encoder errors in the queue."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    worker._register_process(job_id, process)
    last_update = 0.0
    diagnostic_lines: list[str] = []

    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("out_time="):
            seconds = parse_ffmpeg_time(line.partition("=")[2])
            now = time.monotonic()
            if now - last_update >= 0.75:
                fraction = min(1.0, max(0.0, seconds / max(retained_duration, 0.001)))
                progress = progress_start + ((progress_end - progress_start) * fraction)
                update_job(job_id, progress=round(progress, 1))
                last_update = now
        elif not line.startswith(("progress=", "frame=", "fps=", "bitrate=", "speed=")):
            diagnostic_lines.append(line)
            if len(diagnostic_lines) > 12:
                diagnostic_lines.pop(0)

    return_code = process.wait()
    if worker.cancellation_requested(job_id) or is_job_cancelled(job_id):
        raise JobCancelledError("Cancelled by user.")
    if return_code != 0:
        details = " ".join(diagnostic_lines[-4:])
        message = "FFmpeg could not process this video. Check that its codecs and container are supported."
        raise RuntimeError(f"{message} {details}".strip())


def build_ffmpeg_command(
    source: Path,
    output: Path,
    cut_start: float,
    cut_end: float,
    duration: float,
    has_audio: bool,
    target_video_bitrate: int | None = None,
    target_audio_bitrate: int | None = None,
    pass_number: int | None = None,
    passlog_prefix: Path | None = None,
) -> list[str]:
    """Build an accurate trim command, optionally for H.264 two-pass output."""
    start = format_seconds(cut_start)
    end = format_seconds(cut_end)
    at_end = cut_end >= duration - 0.02
    at_start = cut_start <= 0.02

    # The first H.264 pass maps video only. Do not leave an unconnected audio
    # filter output in that pass, otherwise FFmpeg rejects the filter graph.
    include_audio = has_audio and pass_number != 1
    filters: list[str] = []
    if at_start:
        filters.append(f"[0:v]trim=start={end},setpts=PTS-STARTPTS[vout]")
        if include_audio:
            filters.append(f"[0:a]atrim=start={end},asetpts=PTS-STARTPTS[aout]")
    elif at_end:
        filters.append(f"[0:v]trim=end={start},setpts=PTS-STARTPTS[vout]")
        if include_audio:
            filters.append(f"[0:a]atrim=end={start},asetpts=PTS-STARTPTS[aout]")
    else:
        filters.extend(
            [
                f"[0:v]trim=end={start},setpts=PTS-STARTPTS[vpre]",
                f"[0:v]trim=start={end},setpts=PTS-STARTPTS[vpost]",
                "[vpre][vpost]concat=n=2:v=1:a=0[vout]",
            ]
        )
        if include_audio:
            filters.extend(
                [
                    f"[0:a]atrim=end={start},asetpts=PTS-STARTPTS[apre]",
                    f"[0:a]atrim=start={end},asetpts=PTS-STARTPTS[apost]",
                    "[apre][apost]concat=n=2:v=0:a=1[aout]",
                ]
            )

    if pass_number is not None and (not target_video_bitrate or pass_number not in {1, 2}):
        raise ValueError("Two-pass encoding requires a target video bitrate and pass number 1 or 2.")
    if pass_number is not None and passlog_prefix is None:
        raise ValueError("Two-pass encoding requires a pass-log file location.")

    video_args, audio_args, format_args = encoder_args_for_extension(
        output.suffix.lower(),
        has_audio,
        target_video_bitrate=target_video_bitrate,
        target_audio_bitrate=target_audio_bitrate,
    )
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[vout]",
        "-map_metadata",
        "0",
        "-map_chapters",
        "-1",
        *video_args,
    ]

    if pass_number is not None:
        command.extend(["-pass", str(pass_number), "-passlogfile", str(passlog_prefix)])
        if pass_number == 1:
            # Pass 1 deliberately writes no media file and no audio; its only job is to
            # measure complexity so pass 2 can protect the opening keyframe.
            command.extend(["-an", "-f", "null", "-progress", "pipe:1", "-nostats", os.devnull])
            return command

    if has_audio:
        command.extend(["-map", "[aout]", *audio_args])
    command.extend(
        [
            *format_args,
            "-progress",
            "pipe:1",
            "-nostats",
            str(output),
        ]
    )
    return command


def encoder_args_for_extension(
    extension: str,
    has_audio: bool,
    target_video_bitrate: int | None = None,
    target_audio_bitrate: int | None = None,
) -> tuple[list[str], list[str], list[str]]:
    if extension == ".webm":
        return (
            ["-c:v", "libvpx-vp9", "-crf", "31", "-b:v", "0"],
            ["-c:a", "libopus", "-b:a", "128k"] if has_audio else [],
            [],
        )
    if extension == ".avi":
        return (
            ["-c:v", "mpeg4", "-q:v", "3"],
            ["-c:a", "libmp3lame", "-b:a", "128k"] if has_audio else [],
            [],
        )
    if extension in {".mpg", ".mpeg"}:
        return (
            ["-c:v", "mpeg2video", "-q:v", "3"],
            ["-c:a", "mp2", "-b:a", "128k"] if has_audio else [],
            [],
        )

    # Closed GOP plus a normal MP4 layout gives video services a clean, independently
    # decodable opening frame. Do not use maxrate/bufsize here: their tight one-pass VBV
    # restriction was the source of the opening-frame pixelation in size-guard outputs.
    h264_compatibility_args = [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-x264-params",
        "open-gop=0:scenecut=40",
    ]
    format_args = (
        ["-movflags", "+faststart", "-avoid_negative_ts", "make_zero"]
        if extension in {".mp4", ".m4v", ".mov", ".3gp"}
        else []
    )
    if target_video_bitrate:
        video_kbps = max(100, int(target_video_bitrate / 1000))
        video_args = [*h264_compatibility_args, "-b:v", f"{video_kbps}k"]
    else:
        video_args = [*h264_compatibility_args, "-crf", H264_CRF]

    audio_bitrate = max(48_000, int(target_audio_bitrate or H264_AUDIO_BITRATE))
    audio_args = ["-c:a", "aac", "-b:a", f"{int(audio_bitrate / 1000)}k"] if has_audio else []
    return video_args, audio_args, format_args


def cleanup_two_pass_logs(passlog_prefix: Path) -> None:
    """Remove temporary FFmpeg pass logs without touching unrelated app data."""
    for candidate in passlog_prefix.parent.glob(f"{passlog_prefix.name}*"):
        if candidate.is_file():
            candidate.unlink(missing_ok=True)


def validate_export(output: Path, expected_duration: float) -> None:
    """Reject an export that cannot be cleanly decoded from its opening frames."""
    inspected = probe_media(output)
    actual_duration = float(inspected["duration"])
    if actual_duration < max(0.1, expected_duration - 0.75):
        raise RuntimeError(
            f"The exported file is unexpectedly short ({actual_duration:.2f}s; expected about {expected_duration:.2f}s)."
        )

    if output.suffix.lower() in {".mp4", ".m4v", ".mov", ".3gp"}:
        if inspected.get("video_codec") != "h264" or inspected.get("pixel_format") != "yuv420p":
            raise RuntimeError("The MP4 compatibility check failed; the export is not H.264 yuv420p.")

    decode_seconds = min(max(0.1, expected_duration), COMPATIBILITY_DECODE_SECONDS)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-v",
        "error",
        "-xerror",
        "-t",
        format_seconds(decode_seconds),
        "-i",
        str(output),
        "-map",
        "0:v:0",
        "-f",
        "null",
        os.devnull,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"The opening-frame decode check failed. {details}".strip())


def should_apply_size_guard(extension: str, output_size_bytes: int, source_size_bytes: int) -> bool:
    """Only use the bitrate fallback for H.264 container outputs that grew too large."""
    if extension not in {".3gp", ".m4v", ".mkv", ".mov", ".mp4", ".m2ts", ".mts", ".ts"}:
        return False
    ratio = output_size_limit_ratio()
    return output_size_bytes > int(source_size_bytes * ratio)


def output_size_limit_ratio() -> float:
    raw_value = number_or_zero(os.getenv("MAX_OUTPUT_SIZE_RATIO", str(DEFAULT_OUTPUT_SIZE_RATIO)))
    if 0.50 <= raw_value <= 1.00:
        return raw_value
    return DEFAULT_OUTPUT_SIZE_RATIO


def target_bitrates_for_size(
    source_size_bytes: int,
    retained_duration: float,
    has_audio: bool,
    source_audio_bitrate: int,
) -> tuple[int, int | None]:
    """Derive a conservative bitrate target that keeps a shorter output at or below source size."""
    target_total_bitrate = int((source_size_bytes * 8 * output_size_limit_ratio()) / max(retained_duration, 0.1))
    # Leave headroom for audio/container metadata so the completed file remains below
    # the requested source-size ratio without imposing a harmful per-frame VBV cap.
    container_margin = max(64_000, int(target_total_bitrate * 0.06))

    if has_audio:
        preferred_audio = source_audio_bitrate if source_audio_bitrate > 0 else H264_AUDIO_BITRATE
        audio_bitrate = min(H264_AUDIO_BITRATE, preferred_audio)
        audio_bitrate = min(audio_bitrate, max(48_000, int(target_total_bitrate * 0.18)))
    else:
        audio_bitrate = 0

    video_bitrate = max(120_000, target_total_bitrate - audio_bitrate - container_margin)
    return video_bitrate, audio_bitrate if has_audio else None


def output_path_for_job(source: Path, job_id: str) -> Path:
    relative = source.relative_to(INPUT_ROOT)
    output_name = f"{source.stem}_cut_{job_id[:8]}{source.suffix.lower()}"
    return (OUTPUT_ROOT / relative.parent / output_name).resolve()


def validate_cut(cut_start: float, cut_end: float, duration: float) -> None:
    if duration <= 0:
        raise HTTPException(status_code=422, detail="This video has no usable duration.")
    if cut_start < 0 or cut_end <= cut_start:
        raise HTTPException(status_code=422, detail="Choose a cut end that is later than the cut start.")
    if cut_end > duration + 0.05:
        raise HTTPException(status_code=422, detail="The cut end cannot be beyond the end of the video.")
    if cut_start <= 0.02 and cut_end >= duration - 0.02:
        raise HTTPException(status_code=422, detail="The selected cut would remove the entire video.")


def get_video_or_404(video_id: str) -> sqlite3.Row:
    with db_connection() as connection:
        row = connection.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Video not found. Rescan the input folder and try again.")
    return row


def get_job(job_id: str) -> dict[str, Any]:
    with db_connection() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Queue job not found.")
    return serialise_job(row)


def update_job(job_id: str, **updates: Any) -> None:
    if not updates:
        return
    columns = ", ".join(f"{column} = ?" for column in updates)
    values = [updates[column] for column in updates]
    values.append(job_id)
    with db_connection() as connection:
        connection.execute(f"UPDATE jobs SET {columns} WHERE id = ?", values)


def is_job_cancelled(job_id: str) -> bool:
    with db_connection() as connection:
        row = connection.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return bool(row and row["status"] == "cancelled")


def safe_input_file(relative_path: str) -> Path:
    candidate = (INPUT_ROOT / relative_path).resolve()
    try:
        candidate.relative_to(INPUT_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Unsafe source path.") from exc
    return candidate


def delete_source_after_success(source: Path, video_id: str) -> None:
    """Remove a source only after output validation has completed successfully."""
    if not source.is_file():
        raise FileNotFoundError("The source video is no longer present in the input folder.")
    try:
        source.unlink()
    except PermissionError as exc:
        raise RuntimeError(
            "Output was saved, but ClipQueue cannot delete the source because INPUT_DIR is not writable."
        ) from exc
    prune_empty_input_directories(source.parent)
    remove_video_record(video_id)


def remove_video_record(video_id: str) -> None:
    with db_connection() as connection:
        connection.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    (THUMBNAIL_DIR / f"{video_id}.jpg").unlink(missing_ok=True)


def prune_empty_input_directories(directory: Path) -> None:
    """Remove only empty folders beneath INPUT_ROOT; never remove the input root itself."""
    current = directory
    while current != INPUT_ROOT:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def is_directory_writable(directory: Path) -> bool:
    return os.access(directory, os.W_OK | os.X_OK)


def serialise_video(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["skipped"] = bool(data["skipped"])
    return data


def serialise_job(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["cut_duration"] = round(float(data["cut_end"]) - float(data["cut_start"]), 3)
    data["delete_source_on_success"] = bool(data.get("delete_source_on_success"))
    data["size_limited"] = bool(data.get("size_limited"))
    return data


def stable_id(relative_path: str) -> str:
    return hashlib.sha1(relative_path.encode("utf-8"), usedforsecurity=False).hexdigest()[:20]


def format_seconds(value: float) -> str:
    return f"{max(0.0, value):.3f}"


def parse_ffmpeg_time(value: str) -> float:
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, TypeError):
        return 0.0


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def human_error(error: Exception) -> str:
    message = str(error).strip() or error.__class__.__name__
    if len(message) > 500:
        message = message[:497] + "..."
    return message
