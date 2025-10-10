import base64
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from .db import Database


class WahaClient:
    """
    Minimal WAHA (WhatsApp HTTP API) client for sending messages and media.

    Docs: https://waha.devlike.pro/docs/how-to/send-messages/
    """

    def __init__(self, base_url: str, api_key: str, session: str = "default"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.session = session or "default"

    @classmethod
    def from_env(cls) -> "WahaClient":
        db = Database()
        base_url = db.get_setting("WAHA_BASE_URL", None) or os.getenv("WAHA_BASE_URL", "http://localhost:3000")
        api_key = db.get_setting("WAHA_API_KEY", None) or os.getenv("WAHA_API_KEY", "")
        session = db.get_setting("WAHA_SESSION", None) or os.getenv("WAHA_SESSION", "default")
        return cls(base_url=base_url, api_key=api_key, session=session)

    def _headers(self) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
        }
        # WAHA authentication via X-Api-Key header
        if (self.api_key or "").strip():
            h["X-Api-Key"] = self.api_key
        return h

    async def send_message(self, chat_id: str, message: str) -> Dict[str, Any]:
        """
        POST /api/sendText
        """
        url = f"{self.base_url}/api/sendText"
        payload = {"session": self.session, "chatId": chat_id, "text": message}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, json=payload, headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def send_file_by_url(self, chat_id: str, url_file: str, filename: Optional[str] = None, caption: Optional[str] = None) -> Dict[str, Any]:
        """
        POST /api/sendFile with file.url
        """
        if not url_file:
            raise ValueError("send_file_by_url: url_file is empty")
        url = f"{self.base_url}/api/sendFile"
        # Try to infer mimetype from filename or URL, fallback to octet-stream
        mt = mimetypes.guess_type(filename or url_file)[0] or "application/octet-stream"
        payload: Dict[str, Any] = {
            "session": self.session,
            "chatId": chat_id,
            "file": {"mimetype": mt, "filename": filename or "file", "url": url_file},
        }
        if caption:
            payload["caption"] = caption
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, json=payload, headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def _encode_file(self, path: Path) -> Dict[str, Any]:
        """
        Read a local file and prepare WAHA file payload dict with base64 data.
        """
        mt = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        with path.open("rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return {"mimetype": mt, "filename": path.name, "data": b64}

    async def send_file_by_upload(self, chat_id: str, file_path: Path, caption: Optional[str] = None) -> Dict[str, Any]:
        """
        POST /api/sendFile with base64 data
        """
        url = f"{self.base_url}/api/sendFile"
        file_payload = await self._encode_file(file_path)
        payload: Dict[str, Any] = {"session": self.session, "chatId": chat_id, "file": file_payload}
        if caption:
            payload["caption"] = caption
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, json=payload, headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def send_image_by_upload(self, chat_id: str, file_path: Path, caption: Optional[str] = None) -> Dict[str, Any]:
        """
        POST /api/sendImage with base64 data
        WAHA works best when images are JPEG; caller should re-encode accordingly.
        """
        url = f"{self.base_url}/api/sendImage"
        file_payload = await self._encode_file(file_path)
        # If caller produced PNG with alpha, leave as-is; otherwise encourage JPEG by mimetype/filename
        payload: Dict[str, Any] = {"session": self.session, "chatId": chat_id, "file": file_payload}
        if caption:
            payload["caption"] = caption
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, json=payload, headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def send_voice(self, chat_id: str, file_path: Path, convert: bool = True) -> Dict[str, Any]:
        """
        POST /api/sendVoice - optionally let WAHA convert audio to required OGG/OPUS.
        """
        url = f"{self.base_url}/api/sendVoice"
        file_payload = await self._encode_file(file_path)
        payload: Dict[str, Any] = {"session": self.session, "chatId": chat_id, "file": file_payload, "convert": bool(convert)}
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(url, json=payload, headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def send_video(self, chat_id: str, file_path: Path, caption: Optional[str] = None, convert: bool = False, as_note: bool = False) -> Dict[str, Any]:
        """
        POST /api/sendVideo
        """
        url = f"{self.base_url}/api/sendVideo"
        file_payload = await self._encode_file(file_path)
        payload: Dict[str, Any] = {
            "session": self.session,
            "chatId": chat_id,
            "file": file_payload,
            "convert": bool(convert),
            "asNote": bool(as_note),
        }
        if caption:
            payload["caption"] = caption
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(url, json=payload, headers=self._headers())
            r.raise_for_status()
            return r.json()