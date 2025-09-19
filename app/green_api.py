import os
import mimetypes
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

    def _url_delete_notification_delete(self, receipt_id: int) -> str:
        # Official: DELETE /waInstance{id}/DeleteNotification/{token}/{receiptId}
        return f"{self.base_url}/waInstance{self.id_instance}/DeleteNotification/{self.api_token}/{receipt_id}"

    def _url_delete_notification_post(self) -> str:
        # Official: POST /waInstance{id}/DeleteNotification/{token} with {\"receiptId\": ...}
        return f"{self.base_url}/waInstance{self.id_instance}/DeleteNotification/{self.api_token}"

    async def upload_file(self, file_path: Path) -> Dict[str, Any]:
        """
        Upload any file. Content type is guessed from extension.
        Returns JSON with urlFile, etc.
        """
        url = self._url("uploadFile")
        ctype = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        async with httpx.AsyncClient(timeout=300) as client:
            with file_path.open("rb") as f:
                files = {"file": (file_path.name, f, ctype)}
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
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()

    async def send_image_by_url(self, chat_id: str, url_file: str, caption: Optional[str] = None, filename: Optional[str] = None) -> Dict[str, Any]:
        """
        Prefer the dedicated image endpoint so WhatsApp treats the media as an image.
        If the plan/instance forbids sendImageByUrl (403), gracefully fall back to sendFileByUrl.
        """
        url = self._url("sendImageByUrl")
        payload = {
            "chatId": chat_id,
            "urlFile": url_file,
        }
        if caption:
            payload["caption"] = caption
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            # Some Green API tariffs return 403 for sendImageByUrl; use generic file endpoint instead.
            if e.response is not None and e.response.status_code == 403:
                # Ensure we pass a sensible filename with image extension to avoid media misclassification
                fallback_name = filename or "image.jpg"
                return await self.send_file_by_url(chat_id=chat_id, url_file=url_file, filename=fallback_name, caption=caption)
            raise

    async def send_file_by_id(self, chat_id: str, file_id: str, filename: str, caption: Optional[str] = None) -> Dict[str, Any]:
        """
        Alternative to send by URL: some deployments prefer sending by previously uploaded file id.
        """
        url = self._url("sendFileById")
        payload = {
            "chatId": chat_id,
            "idMessage": file_id,  # some docs use 'idFile' or 'fileId', Green API expects idMessage for upload id
            "fileName": filename,
        }
        if caption:
            payload["caption"] = caption
        async with httpx.AsyncClient(timeout=60) as client:
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
        Polls Green API ReceiveNotification for incoming messages and routes them
        through the same handler as the /webhook.
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
        Order as per docs:
          1) DELETE /.../DeleteNotification/{token}/{receiptId}
          2) POST   /.../DeleteNotification/{token} with JSON {\"receiptId\": ...}
        """
        async with httpx.AsyncClient(timeout=30) as client:
            # Variant 1: DELETE with token before receiptId
            url_delete = self._url_delete_notification_delete(receipt_id)
            resp = await client.delete(url_delete)
            if resp.status_code in (200, 204):
                return
            # Variant 2: POST with JSON body
            url_post = self._url_delete_notification_post()
            resp2 = await client.post(url_post, json={"receiptId": receipt_id})
            if resp2.status_code in (200, 204):
                return
            # If both failed, raise last error
            resp2.raise_for_status()