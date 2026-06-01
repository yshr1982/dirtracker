import io
import os
import zipfile
import difflib
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

DB_PATH = os.getenv("DB_PATH", "/data/dirtracker.db")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

LOG_LEVEL = os.getenv("DIRTRACKER_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [backend] %(message)s",
)
logger = logging.getLogger(__name__)


async def init_db() -> None:
    logger.info("Initializing database: %s", DB_PATH)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                path       TEXT UNIQUE NOT NULL,
                first_seen TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id    INTEGER NOT NULL,
                timestamp  TEXT NOT NULL,
                event_type TEXT NOT NULL,
                content    TEXT,
                diff       TEXT,
                FOREIGN KEY (file_id) REFERENCES files(id)
            )
            """
        )
        await db.commit()
    logger.info("Database initialization completed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Backend startup sequence started")
    await init_db()
    logger.info("Backend startup sequence completed")
    yield
    logger.info("Backend shutdown sequence started")


app = FastAPI(title="dirtracker", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class IngestRequest(BaseModel):
    path: str
    event_type: str  # "modified" | "created" | "deleted"
    content: Optional[str] = None


class IngestBatchRequest(BaseModel):
    events: list[IngestRequest]


# ---------------------------------------------------------------------------
# Ingest endpoint (called by watcher)
# ---------------------------------------------------------------------------

async def store_ingest_event(db: aiosqlite.Connection, req: IngestRequest):
    now = datetime.now(timezone.utc).isoformat()

    # Get or create file record
    async with db.execute("SELECT id FROM files WHERE path = ?", (req.path,)) as cur:
        row = await cur.fetchone()

    if row is None:
        cur = await db.execute(
            "INSERT INTO files (path, first_seen) VALUES (?, ?)", (req.path, now)
        )
        file_id = cur.lastrowid
    else:
        file_id = row["id"]

    # Compute unified diff against previous snapshot
    diff = None
    if req.event_type != "deleted" and req.content is not None:
        async with db.execute(
            """SELECT content FROM snapshots
               WHERE file_id = ? AND event_type != 'deleted'
               ORDER BY id DESC LIMIT 1""",
            (file_id,),
        ) as cur:
            prev = await cur.fetchone()

        if prev and prev["content"] is not None:
            prev_lines = prev["content"].splitlines(keepends=True)
            curr_lines = req.content.splitlines(keepends=True)
            diff_lines = list(
                difflib.unified_diff(
                    prev_lines,
                    curr_lines,
                    fromfile=f"{req.path} (prev)",
                    tofile=f"{req.path} (curr)",
                )
            )
            diff = "".join(diff_lines)

    await db.execute(
        "INSERT INTO snapshots (file_id, timestamp, event_type, content, diff) VALUES (?, ?, ?, ?, ?)",
        (file_id, now, req.event_type, req.content, diff),
    )


@app.post("/api/ingest")
async def ingest(req: IngestRequest):
    logger.info("/api/ingest called: path=%s event_type=%s", req.path, req.event_type)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await store_ingest_event(db, req)
        await db.commit()

    return {"status": "ok"}


@app.post("/api/ingest/batch")
async def ingest_batch(req: IngestBatchRequest):
    logger.info("/api/ingest/batch called: events=%d", len(req.events))
    if not req.events:
        return {"status": "ok", "ingested": 0}

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        for event in req.events:
            await store_ingest_event(db, event)
        await db.commit()

    return {"status": "ok", "ingested": len(req.events)}


# ---------------------------------------------------------------------------
# File listing
# ---------------------------------------------------------------------------

@app.get("/api/files")
async def list_files():
    logger.info("/api/files called")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, path, first_seen FROM files ORDER BY path"
        ) as cur:
            rows = await cur.fetchall()
    logger.debug("/api/files returned rows=%d", len(rows))
    return [dict(r) for r in rows]


@app.get("/api/files/{file_id}/snapshots")
async def list_snapshots(file_id: int):
    logger.info("/api/files/%d/snapshots called", file_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, file_id, timestamp, event_type
               FROM snapshots WHERE file_id = ? ORDER BY id DESC""",
            (file_id,),
        ) as cur:
            rows = await cur.fetchall()
    logger.debug("/api/files/%d/snapshots returned rows=%d", file_id, len(rows))
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Snapshot detail
# ---------------------------------------------------------------------------

@app.get("/api/snapshots/{snapshot_id}")
async def get_snapshot(snapshot_id: int):
    logger.info("/api/snapshots/%d called", snapshot_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, file_id, timestamp, event_type, content FROM snapshots WHERE id = ?",
            (snapshot_id,),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return dict(row)


@app.get("/api/snapshots/{snapshot_id}/diff")
async def get_diff(snapshot_id: int):
    logger.info("/api/snapshots/%d/diff called", snapshot_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, file_id, timestamp, event_type, diff FROM snapshots WHERE id = ?",
            (snapshot_id,),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return dict(row)


# ---------------------------------------------------------------------------
# Export  (フォルダ構造保持 ZIP)
# ---------------------------------------------------------------------------

@app.get("/api/export")
async def export_files(
    file_ids: Optional[str] = None,
    snapshot_ids: Optional[str] = None,
):
    """
    クエリパラメータ:
      snapshot_ids=1,2,3  — 指定スナップショット時点の内容を ZIP 化
      file_ids=1,2,3      — 指定ファイルの最新スナップショットを ZIP 化
      (なし)              — 全ファイルの最新スナップショットを ZIP 化

    ZIP 内パスはファイルの絶対パスの先頭 '/' を除去してフォルダ構造を保持。
    """
    logger.info(
        "/api/export called: file_ids=%s snapshot_ids=%s",
        file_ids,
        snapshot_ids,
    )
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        if snapshot_ids:
            ids = [int(x.strip()) for x in snapshot_ids.split(",") if x.strip()]
            placeholders = ",".join("?" * len(ids))
            async with db.execute(
                f"""SELECT s.id, s.content, s.event_type, f.path
                    FROM snapshots s JOIN files f ON s.file_id = f.id
                    WHERE s.id IN ({placeholders})""",
                ids,
            ) as cur:
                rows = await cur.fetchall()

        elif file_ids:
            ids = [int(x.strip()) for x in file_ids.split(",") if x.strip()]
            placeholders = ",".join("?" * len(ids))
            async with db.execute(
                f"""SELECT s.id, s.content, s.event_type, f.path
                    FROM snapshots s JOIN files f ON s.file_id = f.id
                    WHERE f.id IN ({placeholders})
                      AND s.id = (
                        SELECT MAX(s2.id) FROM snapshots s2
                        WHERE s2.file_id = f.id AND s2.event_type != 'deleted'
                      )""",
                ids,
            ) as cur:
                rows = await cur.fetchall()

        else:
            async with db.execute(
                """SELECT s.id, s.content, s.event_type, f.path
                   FROM snapshots s JOIN files f ON s.file_id = f.id
                   WHERE s.id = (
                     SELECT MAX(s2.id) FROM snapshots s2
                     WHERE s2.file_id = f.id AND s2.event_type != 'deleted'
                   )"""
            ) as cur:
                rows = await cur.fetchall()

    logger.debug("/api/export selected rows=%d", len(rows))

    # Build ZIP in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for row in rows:
            if row["content"] is None:
                continue
            # Strip leading '/' to create a relative path inside the ZIP
            archive_path = row["path"].lstrip("/")
            zf.writestr(archive_path, row["content"])

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=dirtracker_export.zip"},
    )


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    logger.info("/ called; serving static index")
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
