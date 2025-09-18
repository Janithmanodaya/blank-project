import asyncio
import json
import logging
import os
import sys
import io
import re
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, TextIO

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from urllib.parse import quote_plus

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
VERSION = "0.5.0"

# Predeclare stream so type checkers (Pylance) see it before first use
_stdout_utf8: TextIO = sys.stdout

# Force a UTF-8 text stream for logging to avoid 'charmap' errors on Windows consoles
def _utf8_stream_for_stdout() -> TextIO:
    try:
        # If possible, wrap the underlying buffer with UTF-8 encoding and replacement on errors
        if hasattr(sys.stdout, "buffer"):
            return io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass
    # Fallback: try reconfigure on Py3.7+
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            return sys.stdout
    except Exception:
        pass
    # Last resort: return original
    return sys.stdout

_stdout_utf8 = _utf8_stream_for_stdout()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.StreamHandler(_stdout_utf8)],
    force=True,  # override any existing handlers (e.g., added by uvicorn) to enforce UTF-8 stream
)


def json_log(event: str, **kwargs):
    """
    Emit an ASCII-only JSON log line so Windows consoles with legacy codepages don't crash
    when messages contain emojis or non-ASCII characters.
    """
    payload = {"ts": datetime.utcnow().isoformat() + "Z", "event": event, **kwargs}
    line = json.dumps(payload, ensure_ascii=True)
    try:
        logging.info(line)
    except Exception:
        # Last resort: strip any non-ascii that slipped through
        try:
            safe_line = line.encode("ascii", "ignore").decode("ascii")
            logging.info(safe_line)
        except Exception:
            pass


app = FastAPI(title=APP_TITLE, version=VERSION)

# Static and templates (served by webui)
static_dir = Path("static")
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Global components
storage = Storage(base=Path("storage"))
composer = PDFComposer(storage=storage)

# Batching state per chat for media -> PDF
BATCH_WINDOW_SECONDS = int(os.getenv("BATCH_WINDOW_SECONDS", "60"))
pending_batches: Dict[str, Dict[str, Any]] = {}
pending_lock = asyncio.Lock()

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

    # Launch Green API notification poller (for setups without webhooks)
    workers.append(asyncio.create_task(notification_poller()))
    # Launch QA cleanup loop to purge sessions older than 24h
    workers.append(asyncio.create_task(qa_cleanup_loop()))

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
                        file_path = await storage.download_media(http_client, m["payload"], job)
                        db.update_media_local_path(m["id"], str(file_path))
                        downloaded_files.append(file_path)
                    except Exception as e:
                        json_log("media_download_error", error=str(e), media=m, job_id=job_id)
                        raise

                # Compose PDF
                try:
                    pdf_result: PDFComposeResult = composer.compose(job, downloaded_files)
                    db.update_job_pdf(job_id, pdf_result.pdf_path, pdf_result.meta_path)
                except Exception as e:
                    # Inform original sender if allowed, then mark failed
                    try:
                        if _is_sender_allowed(job.get("sender"), db):
                            await client.send_message(chat_id=(job.get("sender") or ""), message="I couldn't read the image(s) to create a PDF. Please resend clear images.")
                    except Exception:
                        pass
                    raise

                # Upload and send
                upload = await client.upload_file(pdf_result.pdf_path)
                db.update_job_upload(job_id, upload)
                # Choose destination chat: ADMIN_CHAT_ID if set, otherwise original sender
                dest_chat = os.getenv("ADMIN_CHAT_ID", "") or (job.get("sender") or "")
                send_resp = await client.send_file_by_url(
                    chat_id=dest_chat,
                    url_file=upload.get("urlFile", ""),
                    filename=pdf_result.pdf_path.name,
                    caption=f"PDF from {job['sender']} message {job['msg_id']}",
                )
                db.update_job_status(job_id, "SENT")
                db.append_job_log(job_id, {"upload": upload, "send": send_resp, "dest_chat": dest_chat})

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
    """
    Extract human text from common Green-API payload shapes.
    Handles:
      - textMessageData.textMessage (typeMessage == textMessage)
      - extendedTextMessageData.text (typeMessage == extendedTextMessage)
      - captions for image/file/document
    Fallback: None
    """
    md = payload.get("messageData") or {}
    if not md:
        return None

    t = (md.get("typeMessage") or "").lower()

    # Standard text
    if t == "textmessage":
        tmd = md.get("textMessageData") or {}
        if tmd.get("textMessage"):
            return tmd.get("textMessage")

    # Extended text (links often live here)
    if t == "extendedtextmessage":
        etd = md.get("extendedTextMessageData") or {}
        # Green-API usually uses 'text'
        for k in ("text", "description", "title"):
            v = etd.get(k)
            if isinstance(v, str) and v.strip():
                return v

    # Captions on images/documents
    for k in ("imageMessageData", "fileMessageData", "documentMessageData"):
        if k in md:
            cap = (md.get(k) or {}).get("caption")
            if isinstance(cap, str) and cap.strip():
                return cap

    return None


def _extract_event_time(payload: Dict[str, Any]) -> Optional[datetime]:
    """
    Try to get the message event time as UTC datetime.
    Looks for common Green API fields: 'timestamp' (epoch seconds).
    """
    candidates: List[Optional[Union[int, float, str]]] = []
    # top-level
    candidates.append(payload.get("timestamp"))
    # typical locations
    md = payload.get("messageData") or {}
    candidates.append(md.get("timestamp"))
    # sometimes stored under 'sendTime' or similar
    candidates.append(payload.get("sendTime"))
    candidates.append(md.get("sendTime"))
    # process
    for c in candidates:
        if c is None:
            continue
        try:
            # parse numeric epoch seconds
            if isinstance(c, (int, float)) or (isinstance(c, str) and c.isdigit()):
                sec = float(c)
                # treat values that look like ms
                if sec > 1e12:
                    sec = sec / 1000.0
                return datetime.fromtimestamp(sec, tz=timezone.utc)
        except Exception:
            continue
    return None


WINDOW_SECONDS = 180  # 3 minutes


def _is_sender_allowed(chat_id: Optional[str], db: Database) -> bool:
    if not chat_id:
        return False
    mode = (db.get_setting("REPLY_MODE", "everyone") or "everyone").lower()
    allow_raw = db.get_setting("ALLOW_NUMBERS", "") or ""
    block_raw = db.get_setting("BLOCK_NUMBERS", "") or ""
    def parse_list(s: str) -> List[str]:
        parts = [p.strip() for p in s.replace("\n", ",").split(",") if p.strip()]
        return [p for p in parts]
    allows = set(parse_list(allow_raw))
    blocks = set(parse_list(block_raw))
    if mode == "allowlist":
        return chat_id in allows
    if mode == "blocklist":
        return chat_id not in blocks
    return True  # everyone

async def maybe_auto_reply(payload: Dict[str, Any], db: Database):
    # settings gate
    enabled = (db.get_setting("auto_reply_enabled", "0") or "0") == "1"
    chat_id = payload.get("senderData", {}).get("chatId")
    if not chat_id:
        return
    if not _is_sender_allowed(chat_id, db):
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
    if not enabled:
        return

    try:
        responder = GeminiResponder()
        reply = responder.generate(text, system_prompt)
        client = GreenAPIClient.from_env()
        await client.send_message(chat_id=chat_id, message=reply)
        json_log("auto_reply_sent", chat_id=chat_id)
    except Exception as e:
        json_log("auto_reply_failed", error=str(e))


async def _enqueue_batch_later(sender: str, db: Database):
    try:
        await asyncio.sleep(BATCH_WINDOW_SECONDS)
        async with pending_lock:
            b = pending_batches.get(sender)
            if not b:
                return
            job_id = b["job_id"]
            # Move to queue only if still pending
            db.update_job_status(job_id, "PENDING")
            await job_queue.put(job_id)
            json_log("batch_enqueued", sender=sender, job_id=job_id)
            # Remove batch
            pending_batches.pop(sender, None)
    except asyncio.CancelledError:
        # Batch was cancelled due to new batch or shutdown
        raise
    except Exception as e:
        json_log("batch_enqueue_error", sender=sender, error=str(e))


async def _download_and_send_image(sender: str, query: str, prefer_ext: str, db: Database) -> bool:
    client = GreenAPIClient.from_env()
    tmp_dir = storage.base / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    ext = ".jpg" if prefer_ext.lower() in {"jpg", "jpeg"} else ".png"
    name = f"img_{int(datetime.utcnow().timestamp())}_{random.randint(1000,9999)}{ext}"
    out_path = tmp_dir / name

    # Select a reliable source
    url = ""
    qlow = (query or "").strip().lower()
    if "cat" in qlow and ("image" in qlow or "photo" in qlow or "jpg" in qlow or "jpeg" in qlow):
        # Deterministic cute cat
        w, h = 800, 600
        seed = random.randint(1, 1_000_000)
        url = f"https://placekitten.com/seed/{seed}/{w}/{h}"
        ext = ".jpg"
        out_path = tmp_dir / (out_path.stem + ".jpg")
    else:
        # Unsplash Source supports keyword without API key
        url = f"https://source.unsplash.com/800x600/?{quote_plus(query)}"

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as hc:
            r = await hc.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200 or not r.content:
                return False
            with out_path.open("wb") as f:
                f.write(r.content)
        up = await client.upload_file(out_path)
        if _is_sender_allowed(sender, db):
            await client.send_file_by_url(chat_id=sender, url_file=up.get("urlFile", ""), filename=out_path.name, caption=f"Image for: {query}")
        try:
            out_path.unlink(missing_ok=True)
        except Exception:
            pass
        return True
    except Exception as e:
        json_log("image_fetch_error", error=str(e), query=query)
        try:
            out_path.unlink(missing_ok=True)
        except Exception:
            pass
        if _is_sender_allowed(sender, db):
            try:
                await client.send_message(chat_id=sender, message="I couldn't fetch an image right now.")
            except Exception:
                pass
        return False


async def handle_incoming_payload(payload: Dict[str, Any], db: Database) -> Dict[str, Any]:
    # Persist raw payload
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    storage.save_incoming_payload(payload, f"{ts}.json")

    # Validate minimal structure (Green-API incomingMessageReceived)
    webhook_type = payload.get("typeWebhook")
    if not webhook_type:
        json_log("webhook_ignored", reason="missing_typeWebhook")
        return {"ok": True, "ignored": True}

    # Extract sender, message id, media list heuristically
    instance_id = payload.get("instanceData", {}).get("idInstance") or os.getenv("GREEN_API_INSTANCE_ID", "")
    # Try multiple locations for sender/chat id; some notifications omit senderData
    message_data = payload.get("messageData") or {}
    sender = (
        payload.get("senderData", {}).get("chatId")
        or payload.get("senderData", {}).get("sender")
        or payload.get("chatId")
        or message_data.get("chatId")
        or payload.get("author")
        or "unknown"
    )
    msg_id = payload.get("idMessage") or message_data.get("idMessage") or payload.get("receiptId") or ts

    # Time window filter (3 minutes)
    now = datetime.now(tz=timezone.utc)
    evt_time = _extract_event_time(payload) or now
    age = (now - evt_time).total_seconds()
    if age > WINDOW_SECONDS:
        json_log("message_skipped_outside_window", sender=sender, msg_id=str(msg_id), age_seconds=int(age))
        return {"ok": True, "skipped": "outside_window", "age_seconds": int(age)}

    # Idempotency: skip if we've already handled this message id
    if db.has_processed(str(msg_id)):
        json_log("duplicate_message_skipped", msg_id=str(msg_id), sender=sender)
        return {"ok": True, "duplicate": True, "msg_id": str(msg_id)}

    # Mark as processed early to avoid races on re-delivery
    db.mark_processed(str(msg_id))

    media_list: List[Dict[str, Any]] = []
    try:
        type_message = message_data.get("typeMessage")
        if type_message in {"imageMessage", "videoMessage", "documentMessage", "audioMessage"}:
            img = (
                message_data.get("imageMessageData")
                or message_data.get("fileMessageData")
                or message_data.get("documentMessageData")
                or message_data.get("audioMessageData")
                or {}
            )
            if img:
                media_list.append(img)
        if "medias" in message_data and isinstance(message_data["medias"], list):
            media_list.extend(message_data["medias"])
    except Exception:
        pass

    # Feature: OCR/QA, YouTube, and search handling
    from .ocr_qa import GeminiFileQA, state as qa_state, find_youtube_url

    client = GreenAPIClient.from_env()

    text_msg = _extract_text_from_payload(payload) or ""

    # If text contains a YouTube link
    yt_url = find_youtube_url(text_msg or "")
    if yt_url:
        json_log("youtube_link_detected", sender=sender, url=yt_url)
        # ask for confirmation
        qa_state.set_pending_ytdl(sender, yt_url)
        if _is_sender_allowed(sender, db) and sender != "unknown":
            await client.send_message(chat_id=sender, message="You sent a YouTube link. Do you want me to download this video and send it back? Reply YES to confirm.")
        return {"ok": True, "job_id": None}

    # If awaiting yt-dlp confirmation
    pending_url = qa_state.get_pending_ytdl(sender)
    if pending_url and (text_msg or "").strip().lower() in {"yes", "y", "download", "ok"}:
        # download video, upload, send, delete
        try:
            import subprocess
            import shlex
            tmp_dir = storage.base / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            out_tpl = str(tmp_dir / "yt_video.%(ext)s")
            cmd = f"yt-dlp -f mp4 -o {shlex.quote(out_tpl)} {shlex.quote(pending_url)}"
            proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                if _is_sender_allowed(sender, db):
                    await client.send_message(chat_id=sender, message=f"Failed to download video. Error: {stderr.decode('utf-8', 'ignore')[:200]}")
            else:
                # find the produced file (mp4 preferred)
                candidates = sorted(tmp_dir.glob("yt_video.*"), key=lambda p: p.stat().st_mtime, reverse=True)
                if not candidates:
                    if _is_sender_allowed(sender, db):
                        await client.send_message(chat_id=sender, message="Download finished but no file was produced.")
                else:
                    video_path = candidates[0]
                    up = await client.upload_file(video_path)
                    if _is_sender_allowed(sender, db):
                        await client.send_file_by_url(chat_id=sender, url_file=up.get("urlFile", ""), filename=video_path.name, caption="Here is your video.")
                    try:
                        video_path.unlink()
                    except Exception:
                        pass
            qa_state.set_pending_ytdl(sender, None)
        except Exception as e:
            if _is_sender_allowed(sender, db):
                await client.send_message(chat_id=sender, message=f"Error while downloading video: {e}")
            qa_state.set_pending_ytdl(sender, None)
        return {"ok": True, "job_id": None}

    # Simple internet search command: "search: ..."
    if text_msg.lower().startswith("search:") or text_msg.lower().startswith("search "):
        query = text_msg.split(":", 1)[1].strip() if ":" in text_msg else text_msg.split(" ", 1)[1].strip()
        links = await _web_search_links(query)
        if _is_sender_allowed(sender, db):
            if not links:
                await client.send_message(chat_id=sender, message="No results found.")
            else:
                head = "\n".join(links[:8])
                await client.send_message(chat_id=sender, message=f"Top results for \"{query}\":\n{head}")
        return {"ok": True, "job_id": None}

    # Image fetch command: "image: cats jpg" or "img: cat"
    low = (text_msg or "").strip().lower()
    if low.startswith("image:") or low.startswith("img:") or (("image" in low or "photo" in low or "picture" in low) and ("jpg" in low or "jpeg" in low or "png" in low or "cat" in low)):
        body = text_msg.split(":", 1)[1].strip() if ":" in text_msg[:6] else text_msg
        prefer_ext = "jpg"
        if "png" in low:
            prefer_ext = "png"
        elif "jpeg" in low or "jpg" in low:
            prefer_ext = "jpg"
        ok_img = await _download_and_send_image(sender, body, prefer_ext, db)
        if ok_img:
            return {"ok": True, "job_id": None}
        # if failed, fall through to other handlers

    # Toggle: if pdf_packer_enabled -> existing batching to PDF, else switch to QA mode
    pdf_packer_enabled = (db.get_setting("pdf_packer_enabled", "1") or "1") == "1"

    if media_list and pdf_packer_enabled:
        # Media batching logic: if images present, accumulate per sender for BATCH_WINDOW_SECONDS
        async with pending_lock:
            batch = pending_batches.get(sender)
            if batch:
                # Append to existing batch job
                job_id = batch["job_id"]
                for m in media_list:
                    db.add_media(job_id, m)
                json_log("batch_appended", sender=sender, job_id=job_id, added=len(media_list))
                result_job_id = job_id
            else:
                # Create new job and start timer
                job_id = db.create_job(sender=sender, msg_id=str(msg_id), payload=payload, instance_id=str(instance_id))
                for m in media_list:
                    db.add_media(job_id, m)
                db.update_job_status(job_id, "NEW")
                task = asyncio.create_task(_enqueue_batch_later(sender, db))
                pending_batches[sender] = {"job_id": job_id, "started_at": now.isoformat(), "task": task}
                json_log("batch_started", sender=sender, job_id=job_id, window_seconds=BATCH_WINDOW_SECONDS, medias=len(media_list))
                result_job_id = job_id
        # Try auto reply (non-blocking)
        asyncio.create_task(maybe_auto_reply(payload, db))
        return {"ok": True, "job_id": result_job_id}

    if media_list and not pdf_packer_enabled:
        # Immediate download and create a separate session for this message (no batching)
        async with httpx.AsyncClient(timeout=60) as http_client:
            job_id = db.create_job(sender=sender, msg_id=str(msg_id), payload=payload, instance_id=str(instance_id))
            db.update_job_status(job_id, "PROCESSING")
            downloaded: List[Path] = []
            for m in media_list:
                try:
                    fp = await storage.download_media(http_client, m, {"sender": sender, "msg_id": str(msg_id)})
                    db.add_media(job_id, m)
                    downloaded.append(fp)
                except Exception as e:
                    json_log("media_download_error", error=str(e))
            db.update_job_status(job_id, "COMPLETED")
        # Filter to files Gemini can read (images, pdf, audio)
        valid_ext = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".mp3", ".wav", ".m4a", ".ogg", ".webm"}
        valid_files = [p for p in downloaded if p.suffix.lower() in valid_ext]
        from .ocr_qa import state as _state
        # Create a new session id from msg_id (short)
        session_id = str(msg_id)[-8:]
        if valid_files:
            _state.create_session(sender, session_id, valid_files)
            if _is_sender_allowed(sender, db):
                await client.send_message(
                    chat_id=sender,
                    message=f"Received {len(valid_files)} file(s). Saved as session {session_id}. Ask questions about this file. You can switch with 'use {session_id}', list sessions with 'list', delete with 'delete {session_id}', or send 'Stop' to end and delete.",
                )
        else:
            # Delete any downloaded non-usable files
            storage.delete_files(downloaded)
            if _is_sender_allowed(sender, db):
                await client.send_message(chat_id=sender, message="I couldn't read the file(s) you sent. Please send images, audio, or PDFs.")
        return {"ok": True, "job_id": job_id}

    # If text and we are in QA mode for this chat
    if text_msg:
        low = text_msg.strip().lower()
        from .ocr_qa import state as _state
        # Commands for sessions
        if low in {"stop", "exit", "quit"}:
            _state.clear_all(sender, storage)
            if _is_sender_allowed(sender, db):
                await client.send_message(chat_id=sender, message="Okay, exiting document Q&A mode. I deleted your files.")
            return {"ok": True, "job_id": None}
        if low == "list":
            sessions = _state.list_sessions(sender)
            if _is_sender_allowed(sender, db):
                if not sessions:
                    await client.send_message(chat_id=sender, message="No saved sessions.")
                else:
                    lines = [f"{s.id} · {len(s.files)} file(s)" for s in sessions]
                    await client.send_message(chat_id=sender, message="Sessions:\n" + "\n".join(lines))
            return {"ok": True, "job_id": None}
        if low.startswith("use "):
            sid = text_msg.strip().split(" ", 1)[1].strip()
            ok = _state.set_active(sender, sid)
            if _is_sender_allowed(sender, db):
                await client.send_message(chat_id=sender, message=("Switched to session " + sid) if ok else "I can't find that session id.")
            return {"ok": True, "job_id": None}
        if low.startswith("delete "):
            sid = text_msg.strip().split(" ", 1)[1].strip()
            _state.delete_session(sender, sid, storage)
            if _is_sender_allowed(sender, db):
                await client.send_message(chat_id=sender, message=f"Deleted session {sid}.")
            return {"ok": True, "job_id": None}
        # If we have any sessions, answer from active one
        if _state.list_sessions(sender):
            try:
                system_prompt = db.get_setting("auto_reply_system_prompt", "") or os.getenv(
                    "GEMINI_SYSTEM_PROMPT", "Answer strictly from the provided file(s)."
                )
                qa = GeminiFileQA()
                ans = qa.answer(sender, text_msg, system_prompt)
                if _is_sender_allowed(sender, db):
                    await client.send_message(chat_id=sender, message=ans)
            except Exception as e:
                if _is_sender_allowed(sender, db):
                    await client.send_message(chat_id=sender, message=f"Error answering from files: {e}")
            return {"ok": True, "job_id": None}

    # If no media and not QA/text special, create a job just to track non-media message; complete immediately
    job_id = db.create_job(sender=sender, msg_id=str(msg_id), payload=payload, instance_id=str(instance_id))
    db.update_job_status(job_id, "COMPLETED")
    json_log("no_media_payload_stored", job_id=job_id)

    # Fallback intent understanding with Gemini when auto-reply is disabled or nothing matched
    if text_msg and GeminiResponder is not None and _is_sender_allowed(sender, db):
        auto_enabled = (db.get_setting("auto_reply_enabled", "0") or "0") == "1"
        if not auto_enabled:
            try:
                system_prompt = db.get_setting("auto_reply_system_prompt", "") or os.getenv(
                    "GEMINI_SYSTEM_PROMPT", "You are a helpful assistant. Identify the user's intent and respond concisely."
                )
                responder = GeminiResponder()
                reply = responder.generate(text_msg, system_prompt)
                await client.send_message(chat_id=sender, message=reply)
                json_log("fallback_gemini_reply_sent", chat_id=sender)
            except Exception as e:
                json_log("fallback_gemini_reply_error", error=str(e))

    # Try auto reply (non-blocking) if enabled
    asyncio.create_task(maybe_auto_reply(payload, db))

    return {"ok": True, "job_id": job_id}


async def notification_poller():
    """
    Polls Green API ReceiveNotification for incoming messages and routes them
    through the same handler as the /webhook.
    """
    db = Database()
    client = GreenAPIClient.from_env()
    while True:
        try:
            data = await client.receive_notification()
            if not data:
                await asyncio.sleep(0.5)
                continue
            receipt_id = data.get("receiptId")
            body = data.get("body") or data
            res = await handle_incoming_payload(body, db)
            json_log("receive_notification_handled", **{"ok": res.get("ok", False), "job_id": res.get("job_id")})
            if receipt_id is not None:
                try:
                    await client.delete_notification(int(receipt_id))
                except Exception as e:
                    json_log("delete_notification_error", error=str(e), receipt_id=receipt_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            json_log("receive_notification_error", error=str(e))
            await asyncio.sleep(2.0)


async def qa_cleanup_loop():
    """
    Periodically purge per-chat sessions and files older than 24 hours.
    """
    from .ocr_qa import state as _state
    while True:
        try:
            _state.purge_old(storage, 24 * 3600)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            json_log("qa_cleanup_error", error=str(e))
        await asyncio.sleep(1800)  # every 30 minutes


@app.post("/webhook")
async def webhook(request: Request, db: Database = Depends(get_db)):
    try:
        payload = await request.json()
    except Exception:
        raw = await request.body()
        return JSONResponse({"ok": False, "error": "invalid_json", "raw": raw.decode("utf-8", "ignore")}, status_code=400)

    res = await handle_incoming_payload(payload, db)
    status = 200 if res.get("ok") else 400
    return JSONResponse(res, status_code=status)


@app.get("/")
async def root():
    return RedirectResponse(url="/ui")


async def _web_search_links(query: str) -> List[str]:
    # Minimal search via DuckDuckGo html
    q = query.strip().replace(" ", "+")
    url = f"https://duckduckgo.com/html/?q={q}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                return []
            html = r.text
            import re
            links = re.findall(r'<a rel="nofollow" class="result__a" href="([^"]+)"', html)
            # Clean /l/?kh=-1&uddg= encoded
            cleaned: List[str] = []
            from urllib.parse import urlparse, parse_qs, unquote
            for L in links:
                if "/l/?" in L and "uddg=" in L:
                    qs = parse_qs(urlparse(L).query)
                    tgt = qs.get("uddg", [""])[0]
                    cleaned.append(unquote(tgt))
                else:
                    cleaned.append(L)
            # de-dup
            out = []
            seen = set()
            for u in cleaned:
                if u not in seen:
                    seen.add(u)
                    out.append(u)
            return out[:10]
    except Exception:
        return []


def run():
    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    run()