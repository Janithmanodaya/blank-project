import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx


class Storage:
    def __init__(self, base: Path = Path("storage")):
        self.base = base

    def ensure_layout(self):
        for p in [
            self.base,
            self.base / "incoming_payloads",
            self.base / "raw",
            self.base / "pdf",
            self.base / "pdf_meta",
            self.base / "quarantine",
            self.base / "tmp",
        ]:
            p.mkdir(parents=True, exist_ok=True)

    def save_incoming_payload(self, payload: Dict[str, Any], name: str) -> Path:
        p = self.base / "incoming_payloads" / name
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        return p

    def raw_dir_for(self, sender: str, msg_id: str) -> Path:
        ts = datetime.utcnow().strftime("%Y%m%d")
        p = self.base / "raw" / sender / f"{ts}_{msg_id}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    async def download_media(self, http_client: httpx.AsyncClient, media: Dict[str, Any], job: Dict[str, Any]) -> Path:
        """
        Download a media file from a Green-API style payload. We look for these keys:
        - downloadUrl
        - url
        - directUrl
        - fileUrl
        Handles any file type (image, pdf, video, etc.) and preserves provided filename if present.
        """
        url = media.get("downloadUrl") or media.get("url") or media.get("directUrl") or media.get("fileUrl")
        filename = media.get("fileName") or media.get("caption") or "media.bin"

        if not url:
            raise ValueError("No media URL in payload")

        raw_dir = self.raw_dir_for(job["sender"], job["msg_id"])
        # ensure deterministic index ordering by creating a numbered filename if collision
        target = raw_dir / filename
        base = target.stem
        ext = target.suffix or ".bin"
        i = 1
        while target.exists():
            target = raw_dir / f"{base}_{i:03d}{ext}"
            i += 1

        # stream download to tmp then move
        tmp = self.base / "tmp" / f"dl_{target.name}"
        tmp.parent.mkdir(parents=True, exist_ok=True)

        # retry 3x with backoff
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                async with http_client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with tmp.open("wb") as f:
                        async for chunk in resp.aiter_bytes():
                            f.write(chunk)
                break
            except Exception as e:
                last_exc = e
        if last_exc:
            raise last_exc

        shutil.move(str(tmp), str(target))
        # write meta next to file
        meta = {"source_url": url, "saved_at": datetime.utcnow().isoformat() + "Z"}
        (raw_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        return target

    def pdf_output_paths(self, sender: str, msg_id: str, suggest_name: Optional[str] = None):
        ts = datetime.utcnow().strftime("%Y%m%d")
        name = suggest_name or f"{ts}_{sender}_{msg_id}.pdf"
        pdf_path = self.base / "pdf" / name
        meta_path = self.base / "pdf_meta" / (pdf_path.stem + ".json")
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        return pdf_path, meta_path

    def quarantine_job(self, job_id: int):
        qdir = self.base / "quarantine" / str(job_id)
        qdir.mkdir(parents=True, exist_ok=True)
        # best-effort; in this scaffold we do not move per-job artifacts beyond this call