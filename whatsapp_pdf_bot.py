#!/usr/bin/env python3
"""
WhatsApp Image-to-PDF Bot (Single-file)

Features
- Automates WhatsApp Web (Playwright) with persistent session
- Monitors new incoming image messages, downloads images with metadata
- Groups images into sets per sender within a configurable time window
- Composes a deterministic A4-layout PDF per set (300 DPI default) with margins, outlines, aspect-ratio preserving placement
- Sends the generated PDF to a configured admin WhatsApp private chat
- WebUI (FastAPI) for dashboard: QR status, live logs, files browser, job controls, settings
- Robustness: structured JSON logging, retries with backoff, persistence via SQLite, atomic file moves, quarantine on failures
- Single Python file for easier deployment

Quickstart
1) Install dependencies:
   pip install fastapi uvicorn[standard] playwright pillow pydantic[dotenv]
   playwright install chromium

2) Run:
   python whatsapp_pdf_bot.py

3) Open Web UI:
   http://localhost:8080
   Configure your admin contact in Settings. Scan QR if required.

Notes
- Use a dedicated WhatsApp account for automation. This is against WhatsApp ToS, may lead to account bans.
- Selectors may change over time; minor adjustments might be needed after WhatsApp Web updates.

Author: Genie (Cosine)
"""

import asyncio
import base64
import contextlib
import dataclasses
import datetime as dt
import io
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import sys
import tempfile
import time
import traceback
import uuid
from collections import deque, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Optional HTTP for Gemini API
try:
    import requests  # lightweight, for Gemini REST
except Exception:
    requests = None


# Third-party libraries
try:
    from fastapi import FastAPI, Request, Response, Depends, HTTPException, status
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
    from PIL import Image, ImageDraw, ImageFont
    from playwright.async_api import async_playwright, Page, BrowserContext, Download, TimeoutError as PWTimeoutError
except ImportError as e:
    print("Missing dependencies. Install with:")
    print("  pip install fastapi uvicorn[standard] playwright pillow pydantic[dotenv]")
    print("Then run: playwright install chromium")
    raise

APP_NAME = "WhatsApp Image-to-PDF Bot"
VERSION = "0.1.0"

# Storage layout
BASE_DIR = Path(os.environ.get("WPPDF_BASE_DIR", os.getcwd())).resolve()
STORAGE_DIR = BASE_DIR / "storage"
RAW_DIR = STORAGE_DIR / "raw"
PDF_DIR = STORAGE_DIR / "pdf"
PROCESSED_DIR = STORAGE_DIR / "processed"
QUAR_DIR = STORAGE_DIR / "quarantine"
TMP_DIR = STORAGE_DIR / "tmp"
BROWSER_PROFILE_DIR = STORAGE_DIR / "browser_profile"
BACKUP_DIR = STORAGE_DIR / "backups"
SESSION_BACKUP_DIR = STORAGE_DIR / "session_backups"
LOG_FILE = STORAGE_DIR / "app.log"
DB_FILE = STORAGE_DIR / "db.sqlite3"
QR_FILE = STORAGE_DIR / "qr.png"
SETTINGS_FILE = STORAGE_DIR / "settings.json"


# PDF defaults
DEFAULT_DPI = 300
A4_W_PX = 2480  # 8.2677in * 300
A4_H_PX = 3508  # 11.6929in * 300

# Classification thresholds (fraction of A4 in px)
FULL_THR = 0.95
HALF_THR = 0.45
QUARTER_THR = 0.22

# Margins
MAX_MARGIN_MM = 15
MARGIN_FRAC = 0.03  # 3% of page width

# Job windows
DEFAULT_GROUP_WINDOW_SEC = 45  # group images within 45s per sender into one set
DEFAULT_RETENTION_DAYS = 30

# Sending limits
SEND_RATE_LIMIT_SEC = 6
MAX_SEND_FAILS_FOR_BREAKER = 3
BREAKER_COOLDOWN_SEC = 60

# Web UI
HOST = os.environ.get("WPPDF_HOST", "0.0.0.0")
PORT = int(os.environ.get("WPPDF_PORT", "8080"))


def now_ts() -> int:
    return int(time.time())


def ts_to_dt(ts: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts)


def dt_fmt(ts: Optional[int] = None) -> str:
    if ts is None:
        ts = now_ts()
    return ts_to_dt(ts).strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs():
    for d in [STORAGE_DIR, RAW_DIR, PDF_DIR, PROCESSED_DIR, QUAR_DIR, TMP_DIR, BROWSER_PROFILE_DIR, BACKUP_DIR, SESSION_BACKUP_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def dir_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except Exception:
                continue
    return total


def list_session_backups() -> List[Path]:
    if not SESSION_BACKUP_DIR.exists():
        return []
    items = [p for p in SESSION_BACKUP_DIR.glob("session_*") if p.is_dir()]
    items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return items


def prune_session_backups(max_keep: int = 5) -> int:
    """
    Keep only newest max_keep session backups.
    """
    items = list_session_backups()
    removed = 0
    for p in items[max_keep:]:
        try:
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
        except Exception:
            continue
    return removed


def create_session_backup(label: Optional[str] = None) -> Optional[Path]:
    """
    Copy the persistent browser profile to a timestamped backup folder.
    """
    try:
        if not BROWSER_PROFILE_DIR.exists():
            return None
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"session_{ts}" + (f"_{label}" if label else "")
        dest = SESSION_BACKUP_DIR / name
        SESSION_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(BROWSER_PROFILE_DIR, dest)
        return dest
    except Exception:
        return None


def restore_latest_session_backup() -> bool:
    """
    Restore the most recent session backup into the persistent browser profile dir.
    """
    try:
        items = list_session_backups()
        if not items:
            return False
        latest = items[0]
        if BROWSER_PROFILE_DIR.exists():
            shutil.rmtree(BROWSER_PROFILE_DIR, ignore_errors=True)
        shutil.copytree(latest, BROWSER_PROFILE_DIR)
        return True
    except Exception:
        return False




class JsonLogger:
    def __init__(self, file_path: Path, maxlen: int = 2000):
        self.file_path = Path(file_path)
        self._deque = deque(maxlen=maxlen)
        self._lock = asyncio.Lock()
        # Load existing logs if file exists
        if self.file_path.exists():
            try:
                with self.file_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            self._deque.append(json.loads(line))
                        except Exception:
                            continue
            except Exception:
                pass

    async def log(self, level: str, module: str, msg: str, **fields):
        record = {
            "ts": dt_fmt(),
            "level": level.upper(),
            "module": module,
            "msg": msg,
            **fields,
        }
        text = json.dumps(record, ensure_ascii=False)
        async with self._lock:
            self._deque.append(record)
            with self.file_path.open("a", encoding="utf-8") as f:
                f.write(text + "\n")

    async def info(self, module: str, msg: str, **fields):
        await self.log("INFO", module, msg, **fields)

    async def error(self, module: str, msg: str, **fields):
        await self.log("ERROR", module, msg, **fields)

    async def warn(self, module: str, msg: str, **fields):
        await self.log("WARN", module, msg, **fields)

    def recent(self, limit: int = 200) -> List[Dict[str, Any]]:
        items = list(self._deque)[-limit:]
        return items


LOGGER = JsonLogger(LOG_FILE)


@dataclass
class Settings:
    admin_contact: str = ""  # name or phone to search for in WhatsApp to send PDFs
    group_window_sec: int = DEFAULT_GROUP_WINDOW_SEC
    dpi: int = DEFAULT_DPI
    ui_token: str = ""  # if non-empty, required in X-Token header for protected endpoints
    allow_upscale: bool = False
    retention_days: int = DEFAULT_RETENTION_DAYS
    landscape_for_landscape_images: bool = True
    max_concurrency: int = 2
    # Auto-reply via Gemini
    enable_gemini_reply: bool = False
    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-flash"
    # Auto-scan for unread chats
    auto_scan_unread_secs: int = 10
    # Browser behavior
    run_headless: bool = False
    auto_adjust_viewport: bool = True

    @staticmethod
    def from_json(d: Dict[str, Any]) -> "Settings":
        s = Settings()
        for k, v in d.items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s

    def to_json(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


class DB:
    def __init__(self, path: Path):
        self.path = path
        self._conn = None
        self._lock = asyncio.Lock()

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    async def init(self):
        if not self.path.exists():
            self.path.touch()
        if self._conn is None:
            self._conn = self._connect()
        await self._init_schema()

    async def _init_schema(self):
        async with self._lock:
            c = self._conn.cursor()
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    sender TEXT,
                    chat_title TEXT,
                    ts INTEGER,
                    has_media INTEGER DEFAULT 0,
                    processed INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS media (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    msg_id TEXT,
                    idx INTEGER,
                    path TEXT,
                    width INTEGER,
                    height INTEGER,
                    mime TEXT,
                    original_filename TEXT
                );

                CREATE TABLE IF NOT EXISTS sets (
                    set_id TEXT PRIMARY KEY,
                    sender TEXT,
                    start_ts INTEGER,
                    end_ts INTEGER,
                    msg_ids TEXT,
                    pdf_path TEXT,
                    status TEXT,
                    version INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    set_id TEXT,
                    state TEXT,
                    attempts INTEGER DEFAULT 0,
                    created_at INTEGER,
                    updated_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    set_id TEXT,
                    status TEXT,
                    attempts INTEGER DEFAULT 0,
                    last_error TEXT,
                    sent_at INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_messages_sender_ts ON messages(sender, ts);
                CREATE INDEX IF NOT EXISTS idx_media_msg ON media(msg_id);
                CREATE INDEX IF NOT EXISTS idx_sets_status ON sets(status);
                CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
                """
            )
            self._conn.commit()

    async def get_settings(self) -> Settings:
        async with self._lock:
            cur = self._conn.execute("SELECT key, value FROM settings")
            rows = cur.fetchall()
        s = {}
        for r in rows:
            try:
                s[r["key"]] = json.loads(r["value"])
            except Exception:
                s[r["key"]] = r["value"]
        # Fallback to JSON file if DB empty
        if not s and SETTINGS_FILE.exists():
            try:
                s = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except Exception:
                s = {}
        settings = Settings.from_json(s)
        return settings

    async def set_settings(self, settings: Settings):
        data = settings.to_json()
        async with self._lock:
            for k, v in data.items():
                self._conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, json.dumps(v)),
                )
            self._conn.commit()
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    async def upsert_message(self, msg_id: str, sender: str, chat_title: str, ts: int, has_media: bool):
        async with self._lock:
            self._conn.execute(
                "INSERT INTO messages(id, sender, chat_title, ts, has_media, processed) VALUES (?, ?, ?, ?, ?, 0) "
                "ON CONFLICT(id) DO UPDATE SET sender=excluded.sender, chat_title=excluded.chat_title, ts=excluded.ts, has_media=excluded.has_media",
                (msg_id, sender, chat_title, ts, 1 if has_media else 0),
            )
            self._conn.commit()

    async def mark_message_processed(self, msg_id: str):
        async with self._lock:
            self._conn.execute("UPDATE messages SET processed=1 WHERE id=?", (msg_id,))
            self._conn.commit()

    async def add_media(self, msg_id: str, idx: int, path: str, w: int, h: int, mime: str, original_filename: str):
        async with self._lock:
            self._conn.execute(
                "INSERT INTO media(msg_id, idx, path, width, height, mime, original_filename) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (msg_id, idx, path, w, h, mime, original_filename),
            )
            self._conn.commit()

    async def create_or_update_set(
        self, set_id: str, sender: str, start_ts: int, end_ts: int, msg_ids: List[str], status: str = "PENDING"
    ):
        async with self._lock:
            self._conn.execute(
                "INSERT INTO sets(set_id, sender, start_ts, end_ts, msg_ids, status) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(set_id) DO UPDATE SET sender=excluded.sender, start_ts=excluded.start_ts, end_ts=excluded.end_ts, msg_ids=excluded.msg_ids, status=excluded.status",
                (set_id, sender, start_ts, end_ts, json.dumps(msg_ids), status),
            )
            self._conn.commit()

    async def set_pdf_path(self, set_id: str, pdf_path: str):
        async with self._lock:
            self._conn.execute("UPDATE sets SET pdf_path=? WHERE set_id=?", (pdf_path, set_id))
            self._conn.commit()

    async def set_set_status(self, set_id: str, status: str):
        async with self._lock:
            self._conn.execute("UPDATE sets SET status=? WHERE set_id=?", (status, set_id))
            self._conn.commit()

    async def enqueue_job(self, set_id: str, state: str = "PENDING"):
        ts = now_ts()
        async with self._lock:
            self._conn.execute(
                "INSERT INTO jobs(set_id, state, attempts, created_at, updated_at) VALUES (?, ?, 0, ?, ?)",
                (set_id, state, ts, ts),
            )
            self._conn.commit()

    async def get_next_job(self) -> Optional[sqlite3.Row]:
        async with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM jobs WHERE state IN ('PENDING','RETRY') ORDER BY created_at ASC LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                self._conn.execute(
                    "UPDATE jobs SET state='PROCESSING', updated_at=? WHERE id=?",
                    (now_ts(), row["id"]),
                )
                self._conn.commit()
            return row

    async def update_job_state(self, job_id: int, state: str, inc_attempt: bool = False):
        async with self._lock:
            if inc_attempt:
                self._conn.execute(
                    "UPDATE jobs SET state=?, attempts=attempts+1, updated_at=? WHERE id=?",
                    (state, now_ts(), job_id),
                )
            else:
                self._conn.execute(
                    "UPDATE jobs SET state=?, updated_at=? WHERE id=?", (state, now_ts(), job_id)
                )
            self._conn.commit()

    async def add_delivery(self, set_id: str, status: str, last_error: str = ""):
        async with self._lock:
            self._conn.execute(
                "INSERT INTO deliveries(set_id, status, attempts, last_error, sent_at) VALUES (?, ?, 1, ?, ?)",
                (set_id, status, last_error, now_ts() if status == "SENT" else None),
            )
            self._conn.commit()

    async def list_jobs(self) -> List[Dict[str, Any]]:
        async with self._lock:
            cur = self._conn.execute(
                "SELECT j.*, s.sender, s.pdf_path, s.status as set_status FROM jobs j LEFT JOIN sets s ON j.set_id=s.set_id ORDER BY j.id DESC LIMIT 200"
            )
            rows = [dict(r) for r in cur.fetchall()]
            return rows

    async def list_sets(self, limit: int = 200) -> List[Dict[str, Any]]:
        async with self._lock:
            cur = self._conn.execute("SELECT * FROM sets ORDER BY start_ts DESC LIMIT ?", (limit,))
            return [dict(r) for r in cur.fetchall()]

    async def get_set(self, set_id: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            cur = self._conn.execute("SELECT * FROM sets WHERE set_id=?", (set_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    async def get_media_for_set(self, set_id: str) -> List[Dict[str, Any]]:
        async with self._lock:
            cur = self._conn.execute(
                """
                SELECT m.* FROM media m
                WHERE m.msg_id IN (
                    SELECT json_extract(value,'$') FROM json_each((SELECT msg_ids FROM sets WHERE set_id=?))
                )
                ORDER BY m.msg_id, m.idx ASC
                """,
                (set_id,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            return rows

    async def cleanup_old_raw(self, before_ts: int) -> int:
        deleted = 0
        for sender_dir in RAW_DIR.glob("*"):
            if not sender_dir.is_dir():
                continue
            for set_dir in sender_dir.glob("*"):
                try:
                    ts_str = set_dir.name.split("_")[0]
                    dt_obj = dt.datetime.strptime(ts_str, "%Y%m%d")
                    if int(dt_obj.timestamp()) < before_ts:
                        shutil.rmtree(set_dir, ignore_errors=True)
                        deleted += 1
                except Exception:
                    continue
        return deleted


class Auth:
    def __init__(self, settings: Settings):
        self.settings = settings

    def __call__(self, request: Request):
        token = self.settings.ui_token
        if not token:
            return
        provided = request.headers.get("X-Token", "")
        if provided != token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


@dataclass
class IncomingMessage:
    msg_id: str
    sender: str
    chat_title: str
    ts: int
    media_thumbs: List[str] = field(default_factory=list)  # for debug
    # internal fields for processing


class IngestionManager:
    """
    Groups incoming messages into sets per sender within a time window.
    """

    def __init__(self, db: DB, settings: Settings):
        self.db = db
        self.settings = settings
        self.pending_sets: Dict[str, Dict[str, Any]] = {}  # key=sender, value=dict(set_id, start_ts, end_ts, msg_ids)

    async def ingest_message(self, msg: IncomingMessage):
        await self.db.upsert_message(msg.msg_id, msg.sender, msg.chat_title, msg.ts, True)

        window = self.settings.group_window_sec
        bucket = self.pending_sets.get(msg.sender)
        if bucket is None:
            set_id = f"{msg.sender}_{msg.ts}"
            self.pending_sets[msg.sender] = {
                "set_id": set_id,
                "start_ts": msg.ts,
                "end_ts": msg.ts,
                "msg_ids": [msg.msg_id],
            }
            await self.db.create_or_update_set(set_id, msg.sender, msg.ts, msg.ts, [msg.msg_id], status="PENDING")
            await LOGGER.info("ingest", "created new set bucket", sender=msg.sender, set_id=set_id)
        else:
            # Extend if within window; else finalize old and start new
            if msg.ts - bucket["end_ts"] <= window:
                bucket["end_ts"] = msg.ts
                bucket["msg_ids"].append(msg.msg_id)
                await self.db.create_or_update_set(
                    bucket["set_id"], msg.sender, bucket["start_ts"], bucket["end_ts"], bucket["msg_ids"], status="PENDING"
                )
                await LOGGER.info("ingest", "appended to existing set", sender=msg.sender, set_id=bucket["set_id"], msg_id=msg.msg_id)
            else:
                # finalize old
                await LOGGER.info("ingest", "finalizing expired set", sender=msg.sender, set_id=bucket["set_id"])
                await self.db.enqueue_job(bucket["set_id"], state="PENDING")
                # create new
                set_id = f"{msg.sender}_{msg.ts}"
                self.pending_sets[msg.sender] = {
                    "set_id": set_id,
                    "start_ts": msg.ts,
                    "end_ts": msg.ts,
                    "msg_ids": [msg.msg_id],
                }
                await self.db.create_or_update_set(set_id, msg.sender, msg.ts, msg.ts, [msg.msg_id], status="PENDING")
                await LOGGER.info("ingest", "created new set bucket", sender=msg.sender, set_id=set_id)

    async def finalize_expired_sets(self):
        """
        Check pending sets and enqueue jobs for sets whose end_ts exceeded window.
        """
        if not self.pending_sets:
            return
        now = now_ts()
        to_finalize = []
        for sender, bucket in list(self.pending_sets.items()):
            if now - bucket["end_ts"] > self.settings.group_window_sec:
                to_finalize.append(sender)
        for sender in to_finalize:
            bucket = self.pending_sets.pop(sender, None)
            if not bucket:
                continue
            await LOGGER.info("ingest", "auto-finalize set", sender=sender, set_id=bucket["set_id"])
            await self.db.enqueue_job(bucket["set_id"], state="PENDING")


class PDFComposer:
    """
    Deterministic A4 layout composer using PIL.
    Implements classification and packing heuristics as specified.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def mm_to_px(mm: float, dpi: int) -> int:
        return int(round(mm / 25.4 * dpi))

    def page_size(self, landscape: bool = False) -> Tuple[int, int]:
        w, h = A4_W_PX, A4_H_PX
        if landscape:
            return h, w
        return w, h

    def classify(self, w: int, h: int, landscape: bool = False) -> str:
        A4w, A4h = self.page_size(landscape)
        if w >= FULL_THR * A4w and h >= FULL_THR * A4h:
            return "FULL"
        if w >= HALF_THR * A4w and h >= HALF_THR * A4h:
            return "HALF"
        if w >= QUARTER_THR * A4w and h >= QUARTER_THR * A4h:
            return "QUARTER"
        return "SMALL"

    def compose(self, images: List[Dict[str, Any]], out_pdf: Path, sidecar: Path) -> None:
        """
        images: list of dicts:
          - path: str
          - width: int
          - height: int
          - mime: str

        Writes a multi-page PDF to out_pdf and JSON sidecar with metadata.
        """
        dpi = self.settings.dpi or DEFAULT_DPI
        allow_up = self.settings.allow_upscale

        # Sort by descending area for greedy packing
        imgs = sorted(images, key=lambda x: x["width"] * x["height"], reverse=True)

        pages: List[Image.Image] = []
        placements: List[Dict[str, Any]] = []

        def margin_px(page_w):
            return min(self.mm_to_px(MAX_MARGIN_MM, dpi), int(round(MARGIN_FRAC * page_w)))

        # Determine if any landscape page is helpful based on majority orientation
        prefer_landscape = False
        if self.settings.landscape_for_landscape_images:
            landscape_count = sum(1 for i in imgs if i["width"] > i["height"])
            prefer_landscape = landscape_count > len(imgs) / 2

        page_w, page_h = self.page_size(landscape=prefer_landscape)
        m = margin_px(page_w)

        # Packing state
        current_page = Image.new("RGB", (page_w, page_h), color=(255, 255, 255))
        current_draw = ImageDraw.Draw(current_page)
        current_mode = None  # None | "FULL" | "HALF" | "QUARTER" | "SMALL"
        slots: List[Tuple[int, int, int, int]] = []  # list of (x, y, w, h)
        slot_used: List[bool] = []
        pending_slots_for_page: List[Dict[str, Any]] = []

        def new_page(mode: Optional[str] = None):
            nonlocal current_page, current_draw, current_mode, slots, slot_used, pending_slots_for_page
            if any(slot_used):
                pages.append(current_page)
            current_page = Image.new("RGB", (page_w, page_h), color=(255, 255, 255))
            current_draw = ImageDraw.Draw(current_page)
            current_mode = mode
            slots = []
            slot_used = []
            pending_slots_for_page = []

        def finalize_page_if_any():
            nonlocal current_page, slot_used
            if any(slot_used):
                pages.append(current_page)

        def make_slots(mode: str, grid: Optional[Tuple[int, int]] = None):
            nonlocal slots, slot_used
            inner_w = page_w - 2 * m
            inner_h = page_h - 2 * m
            if mode == "FULL":
                slots = [(m, m, inner_w, inner_h)]
            elif mode == "HALF":
                # Two vertical slots (stacked) for portrait by default
                # If prefer_landscape, use horizontal slots
                if page_h >= page_w:
                    # stacked vertically
                    slot_h = (inner_h - m) // 2
                    slots = [(m, m, inner_w, slot_h), (m, m + slot_h + m, inner_w, slot_h)]
                else:
                    # side-by-side
                    slot_w = (inner_w - m) // 2
                    slots = [(m, m, slot_w, inner_h), (m + slot_w + m, m, slot_w, inner_h)]
            elif mode == "QUARTER":
                # 2x2 grid
                rows, cols = 2, 2
                cell_w = (inner_w - m) // cols
                cell_h = (inner_h - m) // rows
                slots = []
                for r in range(rows):
                    for c in range(cols):
                        x = m + c * (cell_w + (m if c > 0 else 0))
                        y = m + r * (cell_h + (m if r > 0 else 0))
                        slots.append((x, y, cell_w, cell_h))
            elif mode == "SMALL":
                # Choose grid based on count; default 3x2
                if grid is None:
                    rows, cols = 2, 3
                else:
                    rows, cols = grid
                cell_w = (inner_w - (cols - 1) * m) // cols
                cell_h = (inner_h - (rows - 1) * m) // rows
                slots = []
                for r in range(rows):
                    for c in range(cols):
                        x = m + c * (cell_w + m)
                        y = m + r * (cell_h + m)
                        slots.append((x, y, cell_w, cell_h))
            else:
                slots = []
            slot_used = [False] * len(slots)

        def place_into_slot(img: Image.Image, slot: Tuple[int, int, int, int]):
            x, y, w, h = slot
            # Scale preserving aspect ratio to fit within slot
            iw, ih = img.size
            scale = min(w / iw, h / ih, 1.0 if not allow_up else 10.0)
            tw, th = int(iw * scale), int(ih * scale)
            ox = x + (w - tw) // 2
            oy = y + (h - th) // 2
            img_resized = img.resize((tw, th), Image.LANCZOS)
            current_page.paste(img_resized, (ox, oy))
            # Outline
            current_draw.rectangle([ox - 1, oy - 1, ox + tw + 1, oy + th + 1], outline=(180, 180, 180), width=2)
            return {"slot": (x, y, w, h), "placed": (ox, oy, tw, th)}

        # Process images greedily
        for i, meta in enumerate(imgs):
            try:
                with Image.open(meta["path"]) as im:
                    im = im.convert("RGB")
                    iw, ih = im.size
                    landscape_img = iw > ih
                    # choose classification based on current page orientation
                    klass = self.classify(iw, ih, landscape=prefer_landscape)
                    if klass == "FULL":
                        # Start a new page and place
                        new_page("FULL")
                        make_slots("FULL")
                        placements.append(place_into_slot(im, slots[0]))
                        slot_used[0] = True
                        finalize_page_if_any()
                        new_page(None)  # reset
                        continue
                    if current_mode is None:
                        # pick mode based on class
                        current_mode = klass if klass in ("HALF", "QUARTER") else "SMALL"
                        new_page(current_mode)
                        make_slots(current_mode)
                    # attempt to place into first free slot
                    free_idx = None
                    for idx, used in enumerate(slot_used):
                        if not used:
                            free_idx = idx
                            break
                    if free_idx is None:
                        # page full, start a new page of same mode
                        new_page(current_mode)
                        make_slots(current_mode)
                        free_idx = 0
                    placements.append(place_into_slot(im, slots[free_idx]))
                    slot_used[free_idx] = True
            except Exception as e:
                # Skip images that fail to open
                continue

        # finalize last page if any
        if any(slot_used):
            pages.append(current_page)

        if not pages:
            raise RuntimeError("No pages generated from images")

        # Save PDF
        first, *rest = pages
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        first.save(out_pdf, "PDF", resolution=dpi, save_all=True, append_images=rest)
        # Sidecar
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps({"images": images, "created_at": dt_fmt()}, indent=2), encoding="utf-8")


class DeliveryManager:
    def __init__(self, db: DB, settings: Settings):
        self.db = db
        self.settings = settings
        self.last_send_ts = 0
        self.consecutive_failures = 0
        self.breaker_open_until = 0

    async def can_send(self) -> bool:
        if self.settings.admin_contact.strip() == "":
            return False
        now = now_ts()
        if self.breaker_open_until and now < self.breaker_open_until:
            return False
        if now - self.last_send_ts < SEND_RATE_LIMIT_SEC:
            return False
        return True

    async def mark_success(self):
        self.last_send_ts = now_ts()
        self.consecutive_failures = 0

    async def mark_failure(self, error: str):
        self.consecutive_failures += 1
        if self.consecutive_failures >= MAX_SEND_FAILS_FOR_BREAKER:
            self.breaker_open_until = now_ts() + BREAKER_COOLDOWN_SEC
        await LOGGER.warn("delivery", "send failure", consecutive_failures=self.consecutive_failures, error=error)


class WhatsAppBot:
    """
    Playwright automation for WhatsApp Web
    - Persistent context
    - QR capture
    - Incoming image messages detection via MutationObserver
    - Download images by interacting with UI
    - Send PDF files to admin chat
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.playwright = None
        self.ctx: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()

        # incoming queue from MutationObserver
        self.incoming_queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
        # seen message ids to de-duplicate processing
        self._seen_msg_ids = set()

    async def start(self):
        await LOGGER.info("wa", "starting playwright")

        # Attempt session restore if profile dir looks empty/small and backups exist
        try:
            size = dir_size_bytes(BROWSER_PROFILE_DIR)
            if size < 1024 * 50:  # less than ~50KB likely empty
                restored = restore_latest_session_backup()
                if restored:
                    await LOGGER.info("wa", "restored latest session backup")
        except Exception as e:
            await LOGGER.warn("wa", "session restore check failed", error=str(e))

        self.playwright = await async_playwright().start()
        chromium = self.playwright.chromium
        self.ctx = await chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_DIR),
            headless=bool(self.settings.run_headless),
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--disable-features=CalculateNativeWinOcclusion",
            ],
        )

        pages = self.ctx.pages
        if pages:
            self.page = pages[0]
        else:
            self.page = await self.ctx.new_page()

        self.page.on("response", self._on_response)
        self.page.on("download", self._on_download)

        # Make navigation timeouts generous for slow connections
        try:
            self.page.set_default_navigation_timeout(120000)  # 120s
            self.page.set_default_timeout(120000)  # 120s for operations
        except Exception:
            pass

        # Try to navigate but don't fail startup on timeout
        try:
            await self.page.goto("https://web.whatsapp.com/", timeout=120000, wait_until="domcontentloaded")
        except Exception as e:
            # Continue startup even if initial navigation times out; page may still load eventually
            await LOGGER.warn("wa", "initial goto timed out/failed; continuing", error=str(e))

        await asyncio.sleep(3)
        try:
            await self._install_observer_scripts()
        except Exception as e:
            # Observer script also gets re-attempted via setInterval inside the page
            await LOGGER.warn("wa", "install observer failed; will retry via page timer", error=str(e))

        self._ready.set()
        await LOGGER.info("wa", "playwright ready")

        # Periodic QR capture
        asyncio.create_task(self._qr_updater())

        # Start unread/observer fallback scanner
        asyncio.create_task(self._auto_scan_loop())
        # Start viewport adjust loop
        asyncio.create_task(self._viewport_adjust_loop())

    async def _auto_scan_loop(self):
        """
        Periodically scan chats to surface new messages to the DOM and trigger the page-side scanner.
        This mitigates cases where the observer misses events.
        Designed to keep working even when the window is minimized or backgrounded.
        """
        while not self._stop.is_set():
            try:
                if not self.page:
                    await asyncio.sleep(1.0)
                    continue
                # Trigger in-chat rescan
                try:
                    await self.page.evaluate("window.__wabot_scanAll && window.__wabot_scanAll()")
                except Exception:
                    pass

                # Click a few chats in the list to load their recent messages
                chat_items = self.page.locator('[data-testid="cell-frame-container"]')
                count = await chat_items.count()
                max_to_check = min(count, 5)
                for i in range(max_to_check):
                    try:
                        await chat_items.nth(i).click()
                        await asyncio.sleep(0.5)
                        try:
                            await self.page.evaluate("window.__wabot_scanAll && window.__wabot_scanAll()")
                        except Exception:
                            pass
                    except Exception:
                        continue
                # sleep per settings (shorter in background)
                await asyncio.sleep(max(2, int(self.settings.auto_scan_unread_secs or 10)))
            except Exception:
                await asyncio.sleep(5)

    async def _viewport_adjust_loop(self):
        """
        Periodically match the Playwright viewport to the page's inner window size.
        Helps when the browser is resized or on different displays.
        """
        if not self.settings.auto_adjust_viewport:
            return
        while not self._stop.is_set():
            try:
                if not self.page:
                    await asyncio.sleep(2)
                    continue
                size = await self.page.evaluate("({w: window.innerWidth, h: window.innerHeight})")
                if isinstance(size, dict) and size.get("w") and size.get("h"):
                    try:
                        await self.page.set_viewport_size({"width": int(size["w"]), "height": int(size["h"])})
                    except Exception:
                        pass
                await asyncio.sleep(10)
            except Exception:
                await asyncio.sleep(10)

    async def _send_text_reply(self, text: str) -> bool:
        """
        Type a reply in the current chat composer and send.
        """
        if not self.page:
            return False
        try:
            inputs = self.page.locator('div[contenteditable="true"][role="textbox"]')
            n = await inputs.count()
            if n == 0:
                return False
            # Prefer the last input (composer) rather than search
            composer = inputs.nth(n - 1)
            await composer.click()
            await composer.fill(text)
            await asyncio.sleep(0.2)
            # Press Enter to send
            await self.page.keyboard.press("Enter")
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            await LOGGER.warn("wa", "send_text_reply failed", error=str(e))
            return False

    async def _maybe_auto_reply(self, text_body: str, chat_title: str):
        """
        If enabled, use Gemini to generate a reply and send it.
        """
        if not self.settings.enable_gemini_reply:
            return
        api_key = (self.settings.gemini_api_key or "").strip()
        if not api_key or requests is None:
            await LOGGER.warn("wa", "gemini not configured or requests missing")
            return
        model = (self.settings.gemini_model or "gemini-1.5-flash").strip()
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": f"You are a helpful WhatsApp assistant. Reply concisely to the following message:\n\n{chat_title}: {text_body}"}
                        ]
                    }
                ]
            }
            resp = requests.post(url, json=payload, timeout=20)
            if resp.status_code != 200:
                await LOGGER.warn("wa", "gemini api non-200", status=resp.status_code, body=resp.text[:200])
                return
            data = resp.json()
            # Extract first text part
            reply = ""
            try:
                candidates = data.get("candidates") or []
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        reply = parts[0].get("text", "").strip()
            except Exception:
                reply = ""
            if not reply:
                return
            ok = await self._send_text_reply(reply)
            await LOGGER.info("wa", "auto-replied", ok=ok)
        except Exception as e:
            await LOGGER.warn("wa", "gemini reply failed", error=str(e))

    async def stop(self):
        self._stop.set()
        if self.ctx:
            await self.ctx.close()
        if self.playwright:
            await self.playwright.stop()
        await LOGGER.info("wa", "stopped")

    async def _qr_updater(self):
        while not self._stop.is_set():
            try:
                if self.page:
                    # Try to locate the QR canvas and save as PNG periodically
                    # WhatsApp QR canvas selector may vary; attempt multiple
                    qr_canvas = self.page.locator("canvas[aria-label*='Scan me'], canvas")
                    if await qr_canvas.count() > 0:
                        try:
                            # Try to get data URL
                            data_url = await qr_canvas.evaluate("(c) => c.toDataURL('image/png')")
                            if data_url and data_url.startswith("data:image/png;base64,"):
                                b64 = data_url.split(",", 1)[1]
                                QR_FILE.write_bytes(base64.b64decode(b64))
                                await LOGGER.info("wa", "QR updated")
                        except Exception:
                            # fallback: screenshot element
                            try:
                                png = await qr_canvas.first.screenshot()
                                QR_FILE.write_bytes(png)
                                await LOGGER.info("wa", "QR screenshot updated")
                            except Exception:
                                pass
                await asyncio.sleep(5)
            except Exception:
                await asyncio.sleep(5)

    async def _install_observer_scripts(self):
        """
        Inject a MutationObserver and periodic scanner to detect new messages (media and text),
        then send to Python via exposed binding. Deduplicates by message-id on the page side.
        """
        if not self.page:
            return

        async def on_new_media(source, payload):
            await LOGGER.info("wa", "observer event", payload=payload)
            await self.incoming_queue.put(payload)

        await self.page.expose_binding("pyOnNewMedia", on_new_media)

        js = """
(() => {
  if (window.__wabot_observer_installed) return;
  window.__wabot_observer_installed = true;
  window.__wabot_seen = window.__wabot_seen || new Set();

  function getChatTitle() {
    const el = document.querySelector('[data-testid="conversation-info-header"]') || document.querySelector('header[role="banner"]');
    if (!el) return "";
    const title = el.textContent || "";
    return title.trim();
  }

  function parseMessage(el) {
    try {
      const msgId = el.getAttribute('data-id') || '';
      const pre = el.getAttribute('data-pre-plain-text') || '';
      const textEl = el.querySelector('[data-testid="msg-text"]') || el.querySelector('.selectable-text.copyable-text');
      const text = textEl ? (textEl.innerText || textEl.textContent || "").trim() : "";
      const mediaThumbs = Array.from(el.querySelectorAll('img, video')).map(x => x.getAttribute('src') || x.getAttribute('poster') || '');
      const cls = (el.className || "");
      const isIncoming = /message-in/.test(cls);
      const isOutgoing = /message-out/.test(cls);
      return { id: msgId, preplain: pre, mediaThumbs, text, isIncoming, isOutgoing };
    } catch (e) {
      return null;
    }
  }

  function emitIfNew(msg) {
    if (!msg || !msg.id) return;
    if (window.__wabot_seen.has(msg.id)) return;
    window.__wabot_seen.add(msg.id);
    const payload = {
      msgId: msg.id,
      preplain: msg.preplain,
      mediaThumbs: msg.mediaThumbs || [],
      text: msg.text || "",
      isIncoming: !!msg.isIncoming,
      isOutgoing: !!msg.isOutgoing,
      chatTitle: getChatTitle()
    };
    try {
      window.pyOnNewMedia(payload);
    } catch (e) {
      console.log("pyOnNewMedia error", e);
    }
  }

  function scanAll() {
    const nodes = document.querySelectorAll('[data-id]');
    nodes.forEach((el) => {
      const msg = parseMessage(el);
      if (!msg) return;
      // Only emit for incoming messages
      if (msg.isIncoming) {
        emitIfNew(msg);
      }
    });
  }

  function installObserver() {
    const container =
      document.querySelector('[data-testid="conversation-panel-messages"]') ||
      document.querySelector('[role="application"]') ||
      document.body;
    const obs = new MutationObserver((mutations) => {
      for (const m of mutations) {
        for (const n of m.addedNodes) {
          if (!(n instanceof HTMLElement)) continue;
          const items = n.querySelectorAll ? n.querySelectorAll('[data-id]') : [];
          items.forEach((it) => emitIfNew(parseMessage(it)));
        }
      }
    });
    obs.observe(container, { childList: true, subtree: true });
  }

  // Periodic rescans as fallback (in case MutationObserver misses)
  setInterval(() => {
    try { scanAll(); } catch (e) {}
  }, 4000);

  // Kickoff
  installObserver();
  // Initial sweep
  setTimeout(scanAll, 2000);
})();
"""
        await self.page.add_init_script(js)
        # also run on current page
        await self.page.evaluate(js)

    async def _on_response(self, response):
        # Could be used to intercept media routes if needed
        return

    async def _on_download(self, download: Download):
        # Downloads are handled by explicit waits in download_images_for_message
        return

    async def wait_ready(self) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=30)
            return True
        except asyncio.TimeoutError:
            return False

    async def connection_status(self) -> Dict[str, Any]:
        if not self.page:
            return {"ready": False, "status": "No page"}
        try:
            url = self.page.url
        except Exception:
            url = ""
        status = "unknown"
        try:
            # If chat search box exists, likely logged in
            search_box = self.page.locator('div[contenteditable="true"][role="textbox"]')
            if await search_box.count() > 0:
                status = "connected"
            else:
                status = "qr"
        except Exception:
            status = "unknown"
        return {"ready": True, "status": status, "url": url}

    async def open_chat_by_query(self, query: str) -> bool:
        """
        Use WhatsApp Web search to find a chat by contact name or phone number.
        """
        if not self.page:
            return False
        try:
            # Click on search button or directly focus search box
            search_button = self.page.locator('[data-testid="chat-list-search"]')
            if await search_button.count():
                try:
                    await search_button.click()
                except Exception:
                    pass
            # Focus search input
            search_box = self.page.locator('div[contenteditable="true"][role="textbox"]')
            if await search_box.count() == 0:
                return False
            await search_box.first.click()
            await search_box.first.fill(query)
            await asyncio.sleep(1)
            # Select first result
            first_result = self.page.locator('[data-testid="cell-frame-title"]').first
            if await first_result.count() == 0:
                # Try search results alternative selector
                first_result = self.page.locator('span[dir="auto"]').first
            await first_result.click()
            await asyncio.sleep(1)
            return True
        except Exception as e:
            await LOGGER.error("wa", "open_chat_by_query error", error=str(e))
            return False

    async def download_images_for_message(self, msg_id: str, dest_dir: Path) -> List[Path]:
        """
        For a given message id, click images in that message bubble and download them.
        """
        if not self.page:
            return []
        dest_dir.mkdir(parents=True, exist_ok=True)
        paths: List[Path] = []
        try:
            bubble = self.page.locator(f'[data-id="{msg_id}"]')
            if await bubble.count() == 0:
                await LOGGER.warn("wa", "message bubble not found", msg_id=msg_id)
                return []
            # Find image/thumb elements within the bubble
            thumbs = bubble.locator('img, [data-testid="image-thumb"]')
            count = await thumbs.count()
            if count == 0:
                await LOGGER.warn("wa", "no image thumbs in message", msg_id=msg_id)
                return []

            for i in range(count):
                # Click the thumb to open viewer
                t = thumbs.nth(i)
                try:
                    await t.click()
                except Exception:
                    # try force click
                    try:
                        await t.click(force=True)
                    except Exception:
                        continue

                # In viewer: click download button and wait for download
                dled = None
                try:
                    async with self.page.expect_download(timeout=10000) as download_info:
                        # Different selectors for download
                        btn = self.page.locator('[data-testid="media-viewer-download-button"], [aria-label="Download"]')
                        await btn.first.click()
                    dled = await download_info.value
                except PWTimeoutError:
                    # Maybe viewer didn't open correctly; skip
                    pass
                except Exception:
                    pass

                # Save download
                if dled:
                    # Use our own filename format 001.jpg, 002.jpg...
                    idx = len(paths) + 1
                    filename = f"{idx:03d}"
                    suggested = dled.suggested_filename or ""
                    ext = (Path(suggested).suffix or ".jpg")
                    out_path = dest_dir / f"{filename}{ext}"
                    try:
                        await dled.save_as(str(out_path))
                        paths.append(out_path)
                    except Exception as e:
                        await LOGGER.warn("wa", "download save failed", error=str(e))

                # Close viewer to return to chat
                try:
                    close_btn = self.page.locator('[data-testid="x-viewer"], [aria-label="Close"]')
                    if await close_btn.count() > 0:
                        await close_btn.first.click()
                except Exception:
                    # ESC fallback
                    try:
                        await self.page.keyboard.press("Escape")
                    except Exception:
                        pass

                await asyncio.sleep(0.5)

            return paths
        except Exception as e:
            await LOGGER.error("wa", "download_images_for_message error", error=str(e))
            return paths

    @staticmethod
    def parse_preplain(pre: str) -> Tuple[Optional[int], Optional[str]]:
        """
        Parse 'data-pre-plain-text' like "[12:34, 1/1/24] Alice: " into (timestamp, username).
        """
        try:
            m = re.match(r"\[(?P<time>[^,]+),\s*(?P<date>[^\]]+)\]\s*(?P<name>.*?):", pre)
            if not m:
                # Some locales might be "[date, time]" order, try reverse
                m = re.match(r"\[(?P<date>[^,]+),\s*(?P<time>[^\]]+)\]\s*(?P<name>.*?):", pre)
            if not m:
                return None, None
            time_s = m.group("time").strip()
            date_s = m.group("date").strip()
            name = m.group("name").strip()

            # Attempt to parse various formats
            # Common: time "12:34", date "1/1/24"
            for fmt in ["%H:%M", "%I:%M %p"]:
                for dfmt in ["%d/%m/%y", "%m/%d/%y", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%Y-%m-%d"]:
                    try:
                        d = dt.datetime.strptime(f"{date_s} {time_s}", f"{dfmt} {fmt}")
                        return int(d.timestamp()), name
                    except Exception:
                        continue
            # Fallback: today with given time
            try:
                for fmt in ["%H:%M", "%I:%M %p"]:
                    try:
                        t = dt.datetime.strptime(time_s, fmt).time()
                        today = dt.date.today()
                        d = dt.datetime.combine(today, t)
                        return int(d.timestamp()), name
                    except Exception:
                        continue
            except Exception:
                pass
            return None, name
        except Exception:
            return None, None

    @staticmethod
    def parse_sender_from_msg_id(msg_id: str) -> str:
        # msg_id may look like "true_123456789@c.us_3A..."; extract 123456789
        try:
            parts = msg_id.split("_")
            for p in parts:
                if p.endswith("@c.us") or p.endswith("@s.whatsapp.net"):
                    return p.split("@")[0]
        except Exception:
            pass
        return "unknown"

    async def process_incoming_loop(self, db: DB, ingest: IngestionManager):
        """
        Main loop to process incoming messages notified by the MutationObserver.
        Downloads media and records metadata. Also handles text auto-replies via Gemini.
        """
        await self.wait_ready()
        await LOGGER.info("wa", "incoming loop started")
        while not self._stop.is_set():
            try:
                payload = await asyncio.wait_for(self.incoming_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if not payload:
                continue
            msg_id = payload.get("msgId") or ""
            if not msg_id or msg_id in self._seen_msg_ids:
                continue
            self._seen_msg_ids.add(msg_id)

            preplain = payload.get("preplain") or ""
            chat_title = payload.get("chatTitle") or ""
            text_body = (payload.get("text") or "").strip()
            is_incoming = bool(payload.get("isIncoming"))

            ts, display_name = self.parse_preplain(preplain)
            if ts is None:
                ts = now_ts()
            sender = self.parse_sender_from_msg_id(msg_id)
            if sender == "unknown":
                sender = display_name or "unknown"

            # If there's media, attempt download and ingestion
            media_thumbs = payload.get("mediaThumbs") or []
            if media_thumbs:
                set_dir_name = f"{dt.datetime.fromtimestamp(ts).strftime('%Y%m%d')}_{msg_id}"
                dest_dir = RAW_DIR / str(sender) / set_dir_name
                tmp_dir = TMP_DIR / f"dl_{uuid.uuid4().hex}"
                tmp_dir.mkdir(parents=True, exist_ok=True)

                paths = await self.download_images_for_message(msg_id, tmp_dir)
                if paths:
                    # Atomically move tmp dir to dest
                    dest_dir.parent.mkdir(parents=True, exist_ok=True)
                    if not dest_dir.exists():
                        tmp_dir.rename(dest_dir)
                    else:
                        for p in sorted(tmp_dir.glob("*")):
                            shutil.move(str(p), dest_dir / p.name)
                        shutil.rmtree(tmp_dir, ignore_errors=True)

                    # Collect metadata and store
                    meta = {"sender": sender, "chat_title": chat_title, "msg_id": msg_id, "timestamp": ts, "images": []}
                    for idx, p in enumerate(sorted(dest_dir.glob("*"))):
                        try:
                            with Image.open(p) as im:
                                w, h = im.size
                            mime = "image/jpeg" if p.suffix.lower() in [".jpg", ".jpeg"] else "image/png"
                            await db.add_media(msg_id, idx + 1, str(p), w, h, mime, p.name)
                            meta["images"].append(
                                {"idx": idx + 1, "path": str(p), "width": w, "height": h, "mime": mime, "original_filename": p.name}
                            )
                        except Exception:
                            continue
                    (dest_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
                    await db.upsert_message(msg_id, sender, chat_title, ts, has_media=True)
                    await db.mark_message_processed(msg_id)

                    # Ingest and possibly finalize sets
                    msg = IncomingMessage(msg_id=msg_id, sender=sender, chat_title=chat_title, ts=ts)
                    await ingest.ingest_message(msg)
                    await ingest.finalize_expired_sets()
                else:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    await LOGGER.warn("wa", "no images downloaded", msg_id=msg_id)

            # Auto-reply for text messages (only incoming)
            if is_incoming and text_body:
                # Ensure we are in the correct chat before replying
                opened = False
                try:
                    if chat_title:
                        opened = await self.open_chat_by_query(chat_title)
                    if not opened and sender and sender != "unknown":
                        opened = await self.open_chat_by_query(sender)
                except Exception:
                    opened = False
                await self._maybe_auto_reply(text_body, chat_title or sender or "")

    async def send_pdf_to_admin(self, pdf_path: Path) -> bool:
        """
        Attach and send the given PDF to the configured admin chat.
        """
        admin = self.settings.admin_contact.strip()
        if not admin:
            await LOGGER.warn("wa", "admin_contact not configured; cannot send")
            return False
        if not self.page:
            return False
        try:
            ok = await self.open_chat_by_query(admin)
            if not ok:
                await LOGGER.warn("wa", "admin chat not found", admin=admin)
                return False

            # Attach file: click clip, set file, click send
            # There is usually an input[type=file] associated with 'Attach' flow; try to locate directly
            file_inputs = self.page.locator('input[type="file"]')
            # Fallback: click clip button to reveal file input
            clip_btn = self.page.locator('[data-testid="attachment"], [data-icon="clip"]')
            if await clip_btn.count() > 0:
                try:
                    await clip_btn.first.click()
                    await asyncio.sleep(0.3)
                except Exception:
                    pass

            file_inputs = self.page.locator('input[type="file"]')
            if await file_inputs.count() == 0:
                await LOGGER.warn("wa", "file input not found for attachment")
                return False

            await file_inputs.first.set_input_files(str(pdf_path))
            await asyncio.sleep(1.0)

            # Click send
            send_btn = self.page.locator('[data-testid="send"], [aria-label="Send"]')
            if await send_btn.count() > 0:
                await send_btn.first.click()
                await asyncio.sleep(2.0)
                await LOGGER.info("wa", "pdf sent to admin", pdf=str(pdf_path))
                return True
            else:
                await LOGGER.warn("wa", "send button not found")
                return False
        except Exception as e:
            await LOGGER.error("wa", "send_pdf_to_admin error", error=str(e))
            return False


class Worker:
    """
    Processes sets into PDFs and schedules delivery.
    """

    def __init__(self, db: DB, composer: PDFComposer, delivery: DeliveryManager, wa: WhatsAppBot):
        self.db = db
        self.composer = composer
        self.delivery = delivery
        self.wa = wa
        self._stop = asyncio.Event()

    async def run(self):
        await LOGGER.info("worker", "started")
        while not self._stop.is_set():
            row = await self.db.get_next_job()
            if not row:
                await asyncio.sleep(1.0)
                continue
            job_id = row["id"]
            set_id = row["set_id"]
            await LOGGER.info("worker", "processing job", job_id=job_id, set_id=set_id)
            try:
                # Gather media for the set
                media = await self.db.get_media_for_set(set_id)
                if not media:
                    await LOGGER.warn("worker", "no media for set", set_id=set_id)
                    await self.db.update_job_state(job_id, "FAILED", inc_attempt=True)
                    await self.db.set_set_status(set_id, "FAILED")
                    continue

                # Compose PDF
                try:
                    # Determine filename
                    set_info = await self.db.get_set(set_id)
                    if not set_info:
                        raise RuntimeError("set not found")
                    sender = set_info["sender"]
                    start_ts = set_info["start_ts"]
                    ymd = dt.datetime.fromtimestamp(start_ts).strftime("%Y%m%d")
                    out_pdf = PDF_DIR / f"{ymd}_{sender}_{set_id}.pdf"
                    sidecar = out_pdf.with_suffix(".json")

                    images = sorted(media, key=lambda m: (m["msg_id"], m["idx"]))
                    self.composer.compose(images, out_pdf, sidecar)
                    await self.db.set_pdf_path(set_id, str(out_pdf))
                    await LOGGER.info("worker", "pdf composed", pdf=str(out_pdf))
                except Exception as e:
                    await LOGGER.error("worker", "compose failed", error=str(e))
                    await self.db.update_job_state(job_id, "RETRY", inc_attempt=True)
                    await asyncio.sleep(2.0)
                    continue

                # Delivery
                can_send = await self.delivery.can_send()
                if not can_send:
                    await LOGGER.warn("worker", "delivery paused or rate-limited")
                else:
                    ok = await self.wa.send_pdf_to_admin(Path(out_pdf))
                    if ok:
                        await self.delivery.mark_success()
                        await self.db.add_delivery(set_id, "SENT", last_error="")
                        await self.db.set_set_status(set_id, "SENT")
                        await self.db.update_job_state(job_id, "COMPLETED")
                    else:
                        await self.delivery.mark_failure("send failed")
                        await self.db.add_delivery(set_id, "FAILED", last_error="send failed")
                        await self.db.update_job_state(job_id, "RETRY", inc_attempt=True)
                        await asyncio.sleep(3.0)
                        continue

            except Exception as e:
                await LOGGER.error("worker", "job error", error=str(e), job_id=job_id)
                await self.db.update_job_state(job_id, "RETRY", inc_attempt=True)
                await asyncio.sleep(2.0)

        await LOGGER.info("worker", "stopped")

    async def stop(self):
        self._stop.set()


# Backup task
async def periodic_maintenance(db: DB, settings: Settings, stop_event: asyncio.Event):
    await LOGGER.info("maint", "maintenance loop start")
    last_backup = 0
    last_session_backup = 0
    while not stop_event.is_set():
        try:
            now = now_ts()
            # Backups daily (DB + PDFs)
            if now - last_backup > 24 * 3600:
                ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                dest = BACKUP_DIR / f"backup_{ts}"
                dest.mkdir(parents=True, exist_ok=True)
                # Copy DB and PDFs
                try:
                    shutil.copy2(DB_FILE, dest / "db.sqlite3")
                except Exception:
                    pass
                # PDFs
                pdf_dest = dest / "pdf"
                try:
                    if PDF_DIR.exists():
                        shutil.copytree(PDF_DIR, pdf_dest, dirs_exist_ok=True)
                except Exception:
                    pass
                last_backup = now
                await LOGGER.info("maint", "backup created", path=str(dest))

            # Session backup every 12 hours if profile looks valid
            if now - last_session_backup > 12 * 3600:
                try:
                    size = dir_size_bytes(BROWSER_PROFILE_DIR)
                    if size > 1024 * 1024:  # >1MB indicates a valid session/profile
                        bdir = create_session_backup()
                        if bdir:
                            last_session_backup = now
                            pruned = prune_session_backups(max_keep=5)
                            await LOGGER.info("maint", "session backup created", path=str(bdir), pruned=pruned)
                except Exception as e:
                    await LOGGER.warn("maint", "session backup error", error=str(e))

            # Retention cleanup for raw images
            cutoff = now - settings.retention_days * 86400
            deleted = await db.cleanup_old_raw(cutoff)
            if deleted:
                await LOGGER.info("maint", "raw cleanup complete", deleted=deleted)
        except Exception as e:
            await LOGGER.warn("maint", "maintenance error", error=str(e))
        await asyncio.sleep(3600)


# Web UI models
class SettingsModel(BaseModel):
    admin_contact: Optional[str] = None
    group_window_sec: Optional[int] = None
    dpi: Optional[int] = None
    ui_token: Optional[str] = None
    allow_upscale: Optional[bool] = None
    retention_days: Optional[int] = None
    landscape_for_landscape_images: Optional[bool] = None
    max_concurrency: Optional[int] = None
    enable_gemini_reply: Optional[bool] = None
    gemini_api_key: Optional[str] = None
    gemini_model: Optional[str] = None
    run_headless: Optional[bool] = None
    auto_adjust_viewport: Optional[bool] = None

# Simple HTML dashboard template (inline)
DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<title>WhatsApp Image-to-PDF Bot</title>
<style>
body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 0; color: #222; background: #f5f7fb; }
header { background: #1f2937; color: #fff; padding: 12px 18px; display: flex; align-items: center; justify-content: space-between; }
header h1 { margin: 0; font-size: 18px; }
.container { display: grid; grid-template-columns: 280px 1fr 360px; grid-template-rows: auto 1fr auto; gap: 12px; padding: 12px; height: calc(100vh - 52px); }
.panel { background: #fff; border-radius: 8px; box-shadow: 0 1px 2px rgba(0,0,0,0.06); padding: 12px; overflow: auto; }
h2 { margin: 6px 0 10px; font-size: 15px; }
small { color: #666; }
.code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; background: #f1f3f8; padding: 2px 6px; border-radius: 4px; }
#qr { width: 100%; max-width: 240px; background: #f1f3f8; border-radius: 8px; padding: 8px; }
.status-good { color: #16a34a; }
.status-warn { color: #d97706; }
.status-bad { color: #dc2626; }
.log { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; white-space: pre-wrap; line-height: 1.2; }
.tag { display: inline-block; padding: 2px 6px; border-radius: 999px; font-size: 11px; background: #eef2ff; color: #3730a3; margin-right: 6px; }
.btn { border: 0; border-radius: 6px; padding: 6px 10px; background: #2563eb; color: #fff; cursor: pointer; }
.btn:disabled { opacity: 0.6; cursor: not-allowed; }
input, select { padding: 6px 8px; border: 1px solid #e5e7eb; border-radius: 6px; }
label { display: block; font-size: 12px; color: #374151; margin-top: 8px; }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td { padding: 6px 8px; border-bottom: 1px solid #eee; text-align: left; }
tbody tr:hover { background: #f9fafb; }
footer { color: #6b7280; font-size: 12px; }
</style>
<script>
let TOKEN = localStorage.getItem("token") || "";
function hdrs() { return TOKEN ? {"X-Token": TOKEN} : {}; }
async function refresh() {
  const st = await fetch("/status", {headers: hdrs()}).then(r => r.json()).catch(_ => ({}));
  document.getElementById("status").textContent = st.status || "unknown";
  document.getElementById("status").className = st.status === "connected" ? "status-good" : (st.status === "qr" ? "status-warn" : "status-bad");
  if (st.status === "qr") {
    document.getElementById("qrwrap").style.display = "block";
    document.getElementById("qrimg").src = "/qr?t=" + Date.now();
  } else {
    document.getElementById("qrwrap").style.display = "none";
  }

  // Load settings to populate fields
  const settings = await fetch("/settings", {headers: hdrs()}).then(r => r.json()).catch(_ => null);
  if (settings) {
    // Only set UI token input if empty to avoid overwriting entered token
    if ((document.getElementById("ui_token").value || "") === "") {
      document.getElementById("ui_token").value = settings.ui_token || "";
    }
    document.getElementById("admin_contact").value = settings.admin_contact || "";
    document.getElementById("group_window_sec").value = settings.group_window_sec ?? 45;
    document.getElementById("dpi").value = settings.dpi ?? 300;
    document.getElementById("allow_upscale").checked = !!settings.allow_upscale;
    document.getElementById("retention_days").value = settings.retention_days ?? 30;
    document.getElementById("landscape").checked = !!settings.landscape_for_landscape_images;
    document.getElementById("max_concurrency").value = settings.max_concurrency ?? 2;
    document.getElementById("enable_gemini_reply").checked = !!settings.enable_gemini_reply;
    document.getElementById("gemini_model").value = settings.gemini_model || "gemini-1.5-flash";
    if (settings.gemini_api_key_masked) {
      document.getElementById("gemini_api_key").placeholder = settings.gemini_api_key_masked;
    }
  }

  const jobs = await fetch("/jobs", {headers: hdrs()}).then(r => r.json()).catch(_ => []);
  const tbody = document.getElementById("jobs-tbody");
  tbody.innerHTML = "";
  jobs.forEach(j => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${j.id}</td>
      <td>${j.set_id || ""}</td>
      <td>${j.state}</td>
      <td>${j.attempts}</td>
      <td>${j.sender || ""}</td>
      <td>${j.pdf_path ? '<a href="/download/pdf?path=' + encodeURIComponent(j.pdf_path) + '">PDF</a>' : ""}</td>
    `;
    tbody.appendChild(tr);
  });

  const files = await fetch("/files", {headers: hdrs()}).then(r => r.json()).catch(_ => []);
  const fbody = document.getElementById("files-tbody");
  fbody.innerHTML = "";
  files.forEach(f => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${f.name}</td>
      <td>${f.size_mb.toFixed(2)} MB</td>
      <td>${new Date(f.mtime * 1000).toLocaleString()}</td>
      <td>
        <a href="/download/pdf?path=${encodeURIComponent(f.path)}">Download</a>
        <button class="btn" onclick="manualSend('${encodeURIComponent(f.path)}')">Send</button>
      </td>
    `;
    fbody.appendChild(tr);
  });

  const logs = await fetch("/logs?limit=200", {headers: hdrs()}).then(r => r.json()).catch(_ => []);
  const logEl = document.getElementById("logs");
  logEl.textContent = logs.map(l => JSON.stringify(l)).join("\\n");
}
async function saveSettings() {
  TOKEN = document.getElementById("ui_token").value || "";
  localStorage.setItem("token", TOKEN);
  const payload = {
    admin_contact: document.getElementById("admin_contact").value,
    group_window_sec: parseInt(document.getElementById("group_window_sec").value || "45"),
    dpi: parseInt(document.getElementById("dpi").value || "300"),
    ui_token: TOKEN,
    allow_upscale: document.getElementById("allow_upscale").checked,
    retention_days: parseInt(document.getElementById("retention_days").value || "30"),
    landscape_for_landscape_images: document.getElementById("landscape").checked,
    max_concurrency: parseInt(document.getElementById("max_concurrency").value || "2"),
    enable_gemini_reply: document.getElementById("enable_gemini_reply").checked,
    gemini_api_key: document.getElementById("gemini_api_key").value,
    gemini_model: document.getElementById("gemini_model").value || "gemini-1.5-flash",
  };
  await fetch("/settings", {method: "POST", headers: {"Content-Type":"application/json", ...hdrs()}, body: JSON.stringify(payload)});
  alert("Saved.");
}
async function manualSend(pathEnc) {
  await fetch("/send?path=" + pathEnc, {method: "POST", headers: hdrs()}).then(r => r.json()).then(j => alert(JSON.stringify(j))).catch(e => alert("Error"));
}
setInterval(refresh, 3000);
window.onload = refresh;
</script>
</head>
<body>
<header>
  <h1>WhatsApp Image-to-PDF Bot</h1>
  <div>
    Status: <span id="status" class="status-bad">unknown</span>
  </div>
</header>
<div class="container">
  <div class="panel">
    <h2>Login</h2>
    <div id="qrwrap" style="display:none;">
      <img id="qrimg" src="/qr" alt="QR" />
      <div><small>Scan the QR with your WhatsApp app to log in.</small></div>
    </div>
    <div>
      <p><span class="tag">Session</span> Persistent browser profile at <span class="code">storage/browser_profile</span></p>
    </div>
    <h2>Settings</h2>
    <div id="settings">
      <label>UI Token (X-Token) <input id="ui_token" type="text" placeholder="Optional token"/></label>
      <label>Admin contact (name or phone) <input id="admin_contact" type="text" placeholder="e.g. +123456789 or Contact Name"/></label>
      <div class="row">
        <label>Group window (sec) <input id="group_window_sec" type="number" min="10" max="600" value="45"/></label>
        <label>DPI <input id="dpi" type="number" min="72" max="600" value="300"/></label>
        <label>Retention (days) <input id="retention_days" type="number" min="1" max="365" value="30"/></label>
        <label>Max concurrency <input id="max_concurrency" type="number" min="1" max="4" value="2"/></label>
      </div>
      <div class="row">
        <label><input id="allow_upscale" type="checkbox"/> Allow upscaling above 100%</label>
        <label><input id="landscape" type="checkbox" checked/> Landscape pages if mostly landscape images</label>
      </div>
      <h2>Gemini Auto-Reply</h2>
      <div class="row">
        <label><input id="enable_gemini_reply" type="checkbox"/> Enable auto-reply with Gemini</label>
      </div>
      <div class="row">
        <label>Gemini API Key <input id="gemini_api_key" type="password" placeholder="Not set"/></label>
        <label>Gemini Model <input id="gemini_model" type="text" value="gemini-1.5-flash" placeholder="gemini-1.5-flash"/></label>
      </div>
      <button class="btn" onclick="saveSettings()">Save Settings</button>
    </div>
  </div>
  <div class="panel">
    <h2>Live Logs</h2>
    <div id="logs" class="log" style="height: 45vh; overflow:auto;"></div>
    <h2>Job Queue</h2>
    <table>
      <thead><tr><th>ID</th><th>Set</th><th>State</th><th>Attempts</th><th>Sender</th><th>PDF</th></tr></thead>
      <tbody id="jobs-tbody"></tbody>
    </table>
  </div>
  <div class="panel">
    <h2>Files</h2>
    <table>
      <thead><tr><th>Name</th><th>Size</th><th>Modified</th><th>Actions</th></tr></thead>
      <tbody id="files-tbody"></tbody>
    </table>
    <h2>Actions</h2>
    <button class="btn" onclick="refresh()">Refresh</button>
  </div>
</div>
<footer class="panel" style="margin:12px;">
  <div>Storage at <span class="code">./storage</span> • Logs at <span class="code">storage/app.log</span> • Version {{VERSION}}</div>
</footer>
</body>
</html>
"""


class AppState:
    def __init__(self):
        self.settings = Settings()
        self.db = DB(DB_FILE)
        self.wa = WhatsAppBot(self.settings)
        self.ingest = IngestionManager(self.db, self.settings)
        self.composer = PDFComposer(self.settings)
        self.delivery = DeliveryManager(self.db, self.settings)
        self.worker = Worker(self.db, self.composer, self.delivery, self.wa)
        self.maint_stop = asyncio.Event()
        self.maint_task: Optional[asyncio.Task] = None
        self.worker_task: Optional[asyncio.Task] = None
        self.wa_incoming_task: Optional[asyncio.Task] = None


STATE = AppState()
app = FastAPI(title=APP_NAME)

# CORS for local usage
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def auth_dep():
    return Auth(STATE.settings)


@app.on_event("startup")
async def on_startup():
    ensure_dirs()
    await LOGGER.info("app", "starting", version=VERSION)
    await STATE.db.init()
    STATE.settings = await STATE.db.get_settings()
    # Ensure WA bot uses the up-to-date settings object
    if STATE.wa:
        STATE.wa.settings = STATE.settings
    else:
        STATE.wa = WhatsAppBot(STATE.settings)
    # rebind managers to new settings
    STATE.ingest = IngestionManager(STATE.db, STATE.settings)
    STATE.composer = PDFComposer(STATE.settings)
    STATE.delivery = DeliveryManager(STATE.db, STATE.settings)
    STATE.worker = Worker(STATE.db, STATE.composer, STATE.delivery, STATE.wa)

    await STATE.wa.start()
    # Start incoming loop
    STATE.wa_incoming_task = asyncio.create_task(STATE.wa.process_incoming_loop(STATE.db, STATE.ingest))
    # Start worker
    STATE.worker_task = asyncio.create_task(STATE.worker.run())
    # Maintenance
    STATE.maint_stop = asyncio.Event()
    STATE.maint_task = asyncio.create_task(periodic_maintenance(STATE.db, STATE.settings, STATE.maint_stop))


@app.on_event("shutdown")
async def on_shutdown():
    await LOGGER.info("app", "shutdown")
    if STATE.worker_task:
        STATE.worker._stop.set()
        await asyncio.sleep(0.2)
    if STATE.maint_task:
        STATE.maint_stop.set()
        await asyncio.sleep(0.2)
    if STATE.wa:
        await STATE.wa.stop()


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    html = DASHBOARD_HTML.replace("{{VERSION}}", VERSION)
    return HTMLResponse(html)


@app.get("/status")
async def status_ep(dep=Depends(auth_dep)):
    st = await STATE.wa.connection_status()
    return JSONResponse(st)


@app.get("/qr")
async def qr_ep(dep=Depends(auth_dep)):
    if not QR_FILE.exists():
        return PlainTextResponse("No QR available", status_code=404)
    return FileResponse(str(QR_FILE), media_type="image/png")


@app.get("/jobs")
async def jobs_ep(dep=Depends(auth_dep)):
    jobs = await STATE.db.list_jobs()
    return JSONResponse(jobs)


@app.get("/files")
async def files_ep(dep=Depends(auth_dep)):
    items = []
    for p in PDF_DIR.glob("*.pdf"):
        stat = p.stat()
        items.append({"name": p.name, "path": str(p), "mtime": int(stat.st_mtime), "size_mb": stat.st_size / (1024 * 1024)})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return JSONResponse(items)


@app.get("/logs")
async def logs_ep(limit: int = 200, dep=Depends(auth_dep)):
    logs = LOGGER.recent(limit)
    return JSONResponse(logs)


@app.post("/settings")
async def settings_ep(model: SettingsModel, dep=Depends(auth_dep)):
    s = STATE.settings
    changed = {}
    for field in model.model_fields_set:
        v = getattr(model, field)
        if v is not None:
            setattr(s, field, v)
            changed[field] = v
    await STATE.db.set_settings(s)
    await LOGGER.info("app", "settings updated", changed=changed)
    return JSONResponse({"ok": True, "changed": changed})

@app.get("/settings")
async def settings_get(dep=Depends(auth_dep)):
    d = STATE.settings.to_json()
    # Do not expose API key back to clients; provide masked hint
    if d.get("gemini_api_key"):
        d["gemini_api_key_masked"] = "•••••• (set)"
        d["gemini_api_key"] = ""
    return JSONResponse(d)


@app.get("/download/pdf")
async def download_pdf(path: str, dep=Depends(auth_dep)):
    p = Path(path)
    if not p.exists() or not p.is_file() or p.suffix.lower() != ".pdf":
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(p), filename=p.name)


@app.post("/send")
async def send_pdf(path: str, dep=Depends(auth_dep)):
    p = Path(path)
    if not p.exists() or not p.is_file() or p.suffix.lower() != ".pdf":
        raise HTTPException(status_code=404, detail="Not found")
    ok = await STATE.wa.send_pdf_to_admin(p)
    return JSONResponse({"ok": bool(ok)})


@app.get("/sets")
async def list_sets(dep=Depends(auth_dep)):
    sets = await STATE.db.list_sets()
    return JSONResponse(sets)


@app.post("/reprocess")
async def reprocess_set(set_id: str, dep=Depends(auth_dep)):
    s = await STATE.db.get_set(set_id)
    if not s:
        raise HTTPException(status_code=404, detail="Set not found")
    # increment version
    s["version"] = int(s.get("version", 1)) + 1
    await STATE.db.enqueue_job(set_id, state="PENDING")
    await LOGGER.info("app", "reprocess enqueued", set_id=set_id)
    return JSONResponse({"ok": True})


def main():
    import uvicorn

    # graceful shutdown
    def handle_sig(*_):
        try:
            loop = asyncio.get_event_loop()
            for task in asyncio.all_tasks(loop):
                task.cancel()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    uvicorn.run("whatsapp_pdf_bot:app", host=HOST, port=PORT, reload=False, log_level="info")


if __name__ == "__main__":
    ensure_dirs()
    main()
