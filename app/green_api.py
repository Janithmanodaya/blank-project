import os
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from .db import Database


class GreenAPIClient:
    def __init__(self, base_url: str, id_instance: str, api_token: str):
        self.base_url = base_url.rstrip("/")
        self.id_instance = id_instance
        self.api_token = api_token

    @classmethod
    def from_env(cls) -> "GreenAPIClient":
        # Prefer DB settings if available, fall back to environment variables
        db = Database()
        base_url = db.get_setting("GREEN_API_BASE_URL", None) or os.getenv("GREEN_API_BASE_URL", "https://api.green-api.com")
        id_instance = db.get_setting("GREEN_API_INSTANCE_ID", None) or os.getenv("GREEN_API_INSTANCE_ID", "")
        api_token = db.get_setting("GREEN_API_API_TOKEN", None) or os.getenv("GREEN_API_API_TOKEN", "")
        return cls(base_url=base_url, id_instance=id_instance, api_token=api_token)

    def _url(self, path: str) -> str:
        return f"{self.base_url}/waInstance{self.id_instance}/{path}/{self.api_token}"

    async def upload_file(self, file_path: Path) -> Dict[str, Any]:
        # Recommended flow: uploadFile -> returns urlFile
        url = self._url("uploadFile")
        async with httpx.AsyncClient(timeout=60) as client:
            with file_path.open("rb") as f:
                files = {"file": (file_path.name, f, "application/pdf")}
                resp = await client.post(url, files=files)
            resp.raise_for_status()
            return resp.json()

    async def send_file_by_url(self, chat_id: str, url_file: str, filename: str, caption: Optional[str] = None) -> Dict[str, Any]:
        url = self._url("sendFileByUrl")
        payload = {
            "chatId": chat_id,
            "urlFile": url_file,
            "fileName": filename,
        }
        if caption:
            payload["caption"] = caption
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()

    async def send_message(self, chat_id: str, message: str) -> Dict[str, Any]:
        """
        Send a text message to a chat.
        """
        url = self._url("sendMessage")
        payload = {"chatId": chat_id, "message": message}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()

    async def receive_notification(self) -> Optional[Dict[str, Any]]:
        """
        Polls Green API ReceiveNotification endpoint for the next incoming notification.
        Returns the JSON or None if there is no notification available.
        """
        url = self._url("ReceiveNotification")
        async with httpx.AsyncClient(timeout=65) as client:
            # Green API may use long polling; GET with long timeout
            resp = await client.get(url)
            if resp.status_code == 200 and resp.content:
                data = resp.json()
                # When no notification, API may return null
                return data
            if resp.status_code == 204:
                return None
            # On unexpected status, raise to caller
            resp.raise_for_status()
            return None

    async def delete_notification(self, receipt_id: int) -> None:
        """
        Acknowledge and remove a notification so it is not delivered again.
        """
        url = self._url(f"DeleteNotification/{receipt_id}")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.delete(url)
            # Some implementations return 200 with result: true
            if resp.status_code not in (200, 204):
                resp.raise_for_status()