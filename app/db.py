import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DB_PATH = Path("storage/app.db")


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def init(self):
        with self._conn() as con:
            cur = con.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT,
                    msg_id TEXT,
                    instance_id TEXT,
                    status TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    pdf_path TEXT,
                    pdf_meta_path TEXT,
                    upload_meta TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS media (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER,
                    payload_json TEXT,
                    local_path TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS job_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER,
                    entry_json TEXT,
                    created_at TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            con.commit()

    @contextmanager
    def _conn(self):
        con = sqlite3.connect(self.path)
        try:
            yield con
        finally:
            con.close()

    def create_job(self, sender: str, msg_id: str, payload: Dict[str, Any], instance_id: str) -> int:
        from datetime import datetime

        with self._conn() as con:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO jobs (sender, msg_id, instance_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (sender, msg_id, instance_id, "NEW", datetime.utcnow().isoformat() + "Z", datetime.utcnow().isoformat() + "Z"),
            )
            job_id = cur.lastrowid
            # Store raw payload log entry
            cur.execute(
                "INSERT INTO job_logs (job_id, entry_json, created_at) VALUES (?, ?, ?)",
                (job_id, json.dumps({"incoming_payload": payload}), datetime.utcnow().isoformat() + "Z"),
            )
            con.commit()
            return int(job_id)

    def add_media(self, job_id: int, media_payload: Dict[str, Any]):
        with self._conn() as con:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO media (job_id, payload_json) VALUES (?, ?)", (job_id, json.dumps(media_payload))
            )
            con.commit()

    def get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as con:
            cur = con.cursor()
            row = cur.execute("SELECT id, sender, msg_id, instance_id, status, created_at, updated_at, pdf_path, pdf_meta_path, upload_meta FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            keys = ["id", "sender", "msg_id", "instance_id", "status", "created_at", "updated_at", "pdf_path", "pdf_meta_path", "upload_meta"]
            data = dict(zip(keys, row))
            return data

    def get_media_for_job(self, job_id: int) -> List[Dict[str, Any]]:
        with self._conn() as con:
            cur = con.cursor()
            rows = cur.execute("SELECT id, payload_json, local_path FROM media WHERE job_id=?", (job_id,)).fetchall()
            res = []
            for r in rows:
                res.append({"id": r[0], "payload": json.loads(r[1]), "local_path": r[2]})
            return res

    def update_media_local_path(self, media_id: int, local_path: str):
        with self._conn() as con:
            cur = con.cursor()
            cur.execute("UPDATE media SET local_path=? WHERE id=?", (local_path, media_id))
            con.commit()

    def update_job_status(self, job_id: int, status: str):
        from datetime import datetime

        with self._conn() as con:
            cur = con.cursor()
            cur.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (status, datetime.utcnow().isoformat() + "Z", job_id))
            con.commit()

    def update_job_pdf(self, job_id: int, pdf_path: Path, meta_path: Path):
        with self._conn() as con:
            cur = con.cursor()
            cur.execute("UPDATE jobs SET pdf_path=?, pdf_meta_path=? WHERE id=?", (str(pdf_path), str(meta_path), job_id))
            con.commit()

    def update_job_upload(self, job_id: int, upload_meta: Dict[str, Any]):
        with self._conn() as con:
            cur = con.cursor()
            cur.execute("UPDATE jobs SET upload_meta=? WHERE id=?", (json.dumps(upload_meta), job_id))
            con.commit()

    def append_job_log(self, job_id: int, entry: Dict[str, Any]):
        from datetime import datetime

        with self._conn() as con:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO job_logs (job_id, entry_json, created_at) VALUES (?, ?, ?)",
                (job_id, json.dumps(entry), datetime.utcnow().isoformat() + "Z"),
            )
            con.commit()

    def get_job_logs(self, job_id: int) -> List[Dict[str, Any]]:
        with self._conn() as con:
            cur = con.cursor()
            rows = cur.execute(
                "SELECT id, job_id, entry_json, created_at FROM job_logs WHERE job_id=? ORDER BY id ASC",
                (job_id,),
            ).fetchall()
            res = []
            for r in rows:
                res.append({"id": r[0], "job_id": r[1], "entry": json.loads(r[2]) if r[2] else None, "created_at": r[3]})
            return res

    def get_recent_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._conn() as con:
            cur = con.cursor()
            rows = cur.execute(
                "SELECT id, job_id, entry_json, created_at FROM job_logs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            res = []
            for r in rows:
                res.append({"id": r[0], "job_id": r[1], "entry": json.loads(r[2]) if r[2] else None, "created_at": r[3]})
            return res

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._conn() as con:
            cur = con.cursor()
            row = cur.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if not row:
                return default
            return row[0]

    def set_setting(self, key: str, value: str):
        with self._conn() as con:
            cur = con.cursor()
            cur.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            con.commit()


def get_db() -> Database:
    return Database()