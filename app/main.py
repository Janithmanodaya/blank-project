import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .db import Database, get_db
from .green_api import GreenAPIClient
from .pdf_packer import PDFComposer, PDFComposeResult
from .storage import Storage
from .tasks import job_queue, workers

try:
    from .gemini import GeminiResponder  # optional; only used if enabled
except Exception:
    GeminiResponder = None  # type: ignore

APP_TITLE = "GreenAPI Image→PDF Relay"
VERSION = "0.2.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


def json_log(event: str, **kwargs):
    payload = {"ts": datetime.utcnow().isoformat() + "Z", "event": event, **kwargs}
    logging.info(json.dumps(payload))


app = FastAPI(title=APP_TITLE, version=VERSION)

# Static and templates (served by webui)
static_dir = Path("static")
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Global components
storage = Storage(base=Path("storage"))
composer = PDFComposer(storage=storage)

# Background queue and worker are defined in app.tasks to avoid circular imports


@app.on_event("startup")
async def on_startup():
    # Ensure storage directories exist
    storage.ensure_layout()

    # Initialize DB and tables
    db = Database()
    db.init()
    json_log("startup", version=VERSION)

    # Launch workers
    worker_count = int(os.getenv("WORKERS", "2"))
    for i in range(worker_count):
        workers.append(asyncio.create_task(worker_loop(i)))

    # Attach web router after components are ready (import here to avoid circular import)
    from .webui import router as web_router  # local import
    app.include_router(web_router)


@app.on_event("shutdown")
async def on_shutdown():
    json_log("shutdown")
    for w in workers:
        w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)


async def worker_loop(worker_id: int):
    db = Database()
    client = GreenAPIClient.from_env()
    async with httpx.AsyncClient(timeout=30) as http_client:
        while True:
            try:
                job_id = await job_queue.get()
                job = db.get_job(job_id)
                if not job:
                    json_log("worker_skip_missing_job", worker_id=worker_id, job_id=job_id)
                    continue
                db.update_job_status(job_id, "PROCESSING")
                json_log("job_processing", worker_id=worker_id, job_id=job_id, msg_id=job["msg_id"])

                # Download media
                media_items = db.get_media_for_job(job_id)
                downloaded_files = []
                for m in media_items:
                    try:
                        file_path = await storage.download_media(http_client, m, job)
                        db.update_media_local_path(m["id"], str(file_path))
                        downloaded_files.append(file_path)
                    except Exception as e:
                        json_log("media_download_error", error=str(e), media=m, job_id=job_id)
                        raise

                # Compose PDF
                pdf_result: PDFComposeResult = composer.compose(job, downloaded_files)
                db.update_job_pdf(job_id, pdf_result.pdf_path, pdf_result.meta_path)

                # Upload and send
                upload = await client.upload_file(pdf_result.pdf_path)
                db.update_job_upload(job_id, upload)
                send_resp = await client.send_file_by_url(
                    chat_id=os.getenv("ADMIN_CHAT_ID", ""),
                    url_file=upload["urlFile"],
                    filename=pdf_result.pdf_path.name,
                    caption=f"PDF from {job['sender']} message {job['msg_id']}",
                )
                db.update_job_status(job_id, "SENT")
                db.append_job_log(job_id, {"upload": upload, "send": send_resp})

                json_log("job_sent", worker_id=worker_id, job_id=job_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Mark failed and move to quarantine
                try:
                    db.update_job_status(job_id, "FAILED")
                    storage.quarantine_job(job_id)
                except Exception:
                    pass
                json_log("job_failed", worker_id=worker_id, job_id=locals().get("job_id"), error=str(e))
            finally:
                if "job_id" in locals():
                    job_queue.task_done()


def _extract_text_from_payload(payload: Dict[str, Any]) -> Optional[str]:
    md = payload.get("messageData") or {}
    if not md:
        return None
    # Green-API text messages carry typeMessage == "textMessage"
    if md.get("typeMessage") == "textMessage":
        tmd = md.get("textMessageData") or {}
        return tmd.get("textMessage") or None
    # Some media messages may include caption; optionally reply on caption too
    if "imageMessageData" in md:
        return md.get("imageMessageData", {}).get("caption")
    return None


async def maybe_auto_reply(payload: Dict[str, Any], db: Database):
    # settings gate
    enabled = (db.get_setting("auto_reply_enabled", "0") or "0") == "1"
    if not enabled:
        return
    chat_id = payload.get("senderData", {}).get("chatId")
    if not chat_id:
        return
    text = _extract_text_from_payload(payload)
    if not text:
        return

    system_prompt = db.get_setting("auto_reply_system_prompt", "") or os.getenv(
        "GEMINI_SYSTEM_PROMPT", "You are a concise helpful WhatsApp assistant."
    )

    if GeminiResponder is None:
        json_log("auto_reply_error", reason="gemini_module_missing")
        return

    try:
        responder = GeminiResponder()
        reply = responder.generate(text, system_prompt)
        client = GreenAPIClient.from_env()
        await client.send_message(chat_id=chat_id, message=reply)
        json_log("auto_reply_sent", chat_id=chat_id)
    except Exception as e:
        json_log("auto_reply_failed", error=str(e))


@app.post("/webhook")
async def webhook(request: Request, db: Database = Depends(get_db)):
    try:
        payload = await request.json()
    except Exception:
        raw = await request.body()
        return JSONResponse({"ok": False, "error": "invalid_json", "raw": raw.decode("utf-8", "ignore")}, status_code=400)

    # Persist raw payload
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    storage.save_incoming_payload(payload, f"{ts}.json")

    # Validate minimal structure (Green-API incomingMessageReceived)
    webhook_type = payload.get("typeWebhook")
    if not webhook_type:
        json_log("webhook_ignored", reason="missing_typeWebhook")
        return JSONResponse({"ok": True, "ignored": True})

    # Extract sender, message id, media list heuristically
    instance_id = payload.get("instanceData", {}).get("idInstance") or os.getenv("GREEN_API_INSTANCE_ID", "")
    sender = payload.get("senderData", {}).get("chatId") or payload.get("senderData", {}).get("sender") or "unknown"
    msg_id = payload.get("idMessage") or payload.get("messageData", {}).get("idMessage") or payload.get("receiptId") or ts
    message_data = payload.get("messageData") or {}

    media_list: List[Dict[str, Any]] = []
    try:
        type_message = message_data.get("typeMessage")
        if type_message in {"imageMessage", "videoMessage", "documentMessage"}:
            img = message_data.get("imageMessageData") or message_data.get("fileMessageData") or message_data.get("documentMessageData") or {}
            if img:
                media_list.append(img)
        if "medias" in message_data and isinstance(message_data["medias"], list):
            media_list.extend(message_data["medias"])
    except Exception:
        pass

    # Create job
    job_id = db.create_job(sender=sender, msg_id=str(msg_id), payload=payload, instance_id=str(instance_id))
    for m in media_list:
        db.add_media(job_id, m)

    # Enqueue if media present
    if media_list:
        await job_queue.put(job_id)
        db.update_job_status(job_id, "PENDING")
        json_log("job_enqueued", job_id=job_id, msg_id=msg_id, sender=sender, medias=len(media_list))
    else:
        db.update_job_status(job_id, "COMPLETED")
        json_log("no_media_payload_stored", job_id=job_id)

    # Try auto reply (non-blocking)
    asyncio.create_task(maybe_auto_reply(payload, db))

    return JSONResponse({"ok": True, "job_id": job_id})


@app.get("/")
async def root():
    return RedirectResponse(url="/ui")


def run():
    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    run()