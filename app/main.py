import asyncio
import json
import logging
import os
import sys
import io
import re
import random
import ast
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, TextIO

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from urllib.parse import quote_plus, urlparse, parse_qs

from .db import Database, get_db
from .green_api import GreenAPIClient
from .pdf_packer import PDFComposer, PDFComposeResult
from .storage import Storage
from .tasks import job_queue, workers

# Transient store for pending yt-dlp choices per sender
ytdl_pending: Dict[str, Dict[str, Any]] = {}

# Suppress Gemini replies for a period after a PDF-from-images job completes
suppress_after_pdf: Dict[str, float] = {}  # chat_id -> unix_ts_until

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

                # Look for per-job PDF settings in logs (e.g., images_per_page from "PDF:N" command)
                try:
                    logs = db.get_job_logs(job_id)
                    imgs_per_page = None
                    for entry in logs:
                        data = entry.get("entry") or {}
                        if isinstance(data, dict) and "pdf_images_per_page" in data:
                            val = data.get("pdf_images_per_page")
                            try:
                                imgs_per_page = int(val)
                            except Exception:
                                pass
                    if imgs_per_page:
                        job["images_per_page"] = imgs_per_page
                except Exception:
                    pass

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

                # Send the PDF back to the destination chat.
                # Prefer direct upload-and-send to avoid 400s from sendFileByUrl on some tariffs.
                dest_chat = os.getenv("ADMIN_CHAT_ID", "") or (job.get("sender") or "")
                caption = f"PDF from {job['sender']} message {job['msg_id']}"
                try:
                    send_resp = await client.send_file_by_upload(
                        chat_id=dest_chat,
                        file_path=pdf_result.pdf_path,
                        caption=caption,
                    )
                    # store minimal upload info consistent with previous schema
                    db.update_job_upload(job_id, {"sentBy": "upload", "file": str(pdf_result.pdf_path)})
                except Exception:
                    # Fallback: upload to Green API storage then send by URL
                    upload = await client.upload_file(pdf_result.pdf_path)
                    db.update_job_upload(job_id, upload)
                    send_resp = await client.send_file_by_url(
                        chat_id=dest_chat,
                        url_file=upload.get("urlFile", ""),
                        filename=pdf_result.pdf_path.name,
                        caption=caption,
                    )
                db.update_job_status(job_id, "SENT")
                db.append_job_log(job_id, {"send": send_resp, "dest_chat": dest_chat})

                # Immediately delete source images used for this PDF
                try:
                    for fp in downloaded_files:
                        Path(fp).unlink(missing_ok=True)
                except Exception:
                    pass

                # Schedule deletion of generated PDF and its metadata after 3 hours (10800 seconds)
                try:
                    asyncio.create_task(_delete_files_after_delay([pdf_result.pdf_path, pdf_result.meta_path], 10800))
                except Exception:
                    pass

                # Suppress Gemini replies for a short period after a PDF-from-images job (PDF:N flow)
                try:
                    suppress_sec = int(os.getenv("SUPPRESS_GEMINI_AFTER_PDF_SECONDS", "300"))
                    # If job had 'images_per_page' set from PDF:N, treat as pdf_once job
                    if (job.get("images_per_page") is not None) and job.get("sender"):
                        suppress_after_pdf[str(job.get("sender"))] = datetime.utcnow().timestamp() + max(0, suppress_sec)
                except Exception:
                    pass

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


# --- Simple, safe math evaluator for common questions (e.g., "2+2", "what is 3^2?", "7 * (8-3)") ---

_ALLOWED_AST_NODES = {
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Num, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.UAdd, ast.USub, ast.Load, ast.Tuple
}

def _safe_eval_expr(expr: str) -> Optional[float]:
    """
    Evaluate a math expression safely using AST.
    Supports +, -, *, /, //, %, ^ (treated as **), parentheses, and unary +/-.
    Returns a float (or int-castable) or None if not evaluable.
    """
    try:
        # Normalize: caret to power
        s = expr.replace("^", "**")
        # Insert implicit multiplication: "2(3+4)" -> "2*(3+4)" and "(2+3)4" -> "(2+3)*4"
        s = re.sub(r"(?<=\d)\s*(?=\()", "*", s)
        s = re.sub(r"(?<=\))\s*(?=\d)", "*", s)
        # Handle simple 'x' between numbers as multiply: 2x3 -> 2*3
        s = re.sub(r"(?<=\d)\s*[xX]\s*(?=\d)", "*", s)
        # Parse
        node = ast.parse(s, mode="eval")
        # Validate nodes
        for n in ast.walk(node):
            if type(n) not in _ALLOWED_AST_NODES:
                return None
        def _eval(n):
            if isinstance(n, ast.Expression):
                return _eval(n.body)
            if isinstance(n, ast.Constant):
                if isinstance(n.value, (int, float)):
                    return n.value
                return None
            if isinstance(n, ast.Num):  # Py<3.8 compatibility
                return n.n
            if isinstance(n, ast.BinOp):
                l = _eval(n.left); r = _eval(n.right)
                if l is None or r is None:
                    return None
                if isinstance(n.op, ast.Add): return l + r
                if isinstance(n.op, ast.Sub): return l - r
                if isinstance(n.op, ast.Mult): return l * r
                if isinstance(n.op, ast.Div): return l / r
                if isinstance(n.op, ast.FloorDiv): return l // r
                if isinstance(n.op, ast.Mod): return l % r
                if isinstance(n.op, ast.Pow): return l ** r
                return None
            if isinstance(n, ast.UnaryOp):
                v = _eval(n.operand)
                if v is None:
                    return None
                if isinstance(n.op, ast.UAdd): return +v
                if isinstance(n.op, ast.USub): return -v
                return None
            return None
        res = _eval(node)
        if isinstance(res, (int, float)):
            return float(res)
        return None
    except Exception:
        return None

_MATH_TRIGGER_WORDS = ("what is", "calculate", "calc", "solve", "evaluate")

def _maybe_answer_math_text(txt: str) -> Optional[str]:
    """
    Try to detect a math question and compute it.
    Returns a short answer string if computed, else None.
    """
    if not txt or not isinstance(txt, str):
        return None
    s = txt.strip()
    low = s.lower()

    # Heuristic: if string contains only math characters (plus some spaces), treat as expression
    if re.fullmatch(r"[0-9\.\s\+\-\*\/\^\%\(\)xX]+", s):
        val = _safe_eval_expr(s)
        if val is not None:
            # Beautify: show as int if close
            if abs(val - round(val)) < 1e-12:
                return f"{int(round(val))}"
            return f"{val}"
        return None

    # Otherwise, try to extract the math expression from common phrasings
    if any(w in low for w in _MATH_TRIGGER_WORDS) or re.search(r"\d", s):
        # Keep only math-relevant characters
        expr = "".join(ch for ch in s if ch in "0123456789.+-*/%^()xX ")
        expr = re.sub(r"\s+", "", expr)
        # Require at least one operator
        if re.search(r"[\+\-\*\/\^\%\)]", expr):
            val = _safe_eval_expr(expr)
            if val is not None:
                if abs(val - round(val)) < 1e-12:
                    return f"{int(round(val))}"
                return f"{val}"
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

def _is_suppressed_from_gemini(chat_id: Optional[str]) -> bool:
    try:
        if not chat_id:
            return False
        until = suppress_after_pdf.get(chat_id)
        if not until:
            return False
        return (datetime.utcnow().timestamp() < until)
    except Exception:
        return False

async def maybe_auto_reply(payload: Dict[str, Any], db: Database):
    """
    Send a single concise auto-reply (no duplicates, no secondary variants).
    """
    chat_id = payload.get("senderData", {}).get("chatId")
    if not chat_id:
        return
    if not _is_sender_allowed(chat_id, db):
        return
    if _is_suppressed_from_gemini(chat_id):
        return
    text = _extract_text_from_payload(payload)
    if not text:
        return

    base_system = db.get_setting("auto_reply_system_prompt", "") or os.getenv(
        "GEMINI_SYSTEM_PROMPT", "You are a concise helpful WhatsApp assistant."
    )

    if GeminiResponder is None:
        json_log("auto_reply_error", reason="gemini_module_missing")
        return

    try:
        client = GreenAPIClient.from_env()
        responder = GeminiResponder()
        prompt = f"{base_system}\nRespond in one short sentence. Plain text only."
        reply = await asyncio.to_thread(responder.generate, text, prompt)
        await client.send_message(chat_id=chat_id, message=reply)
        json_log("auto_reply_sent_single", chat_id=chat_id)
    except Exception as e:
        json_log("auto_reply_failed", error=str(e))


async def _delete_files_after_delay(paths: List[Path], delay_seconds: int):
    try:
        await asyncio.sleep(delay_seconds)
        for p in paths:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass
    except asyncio.CancelledError:
        raise
    except Exception:
        pass

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

async def _enqueue_pdf_once_later(sender: str, db: Database, window: int = 60):
    client = GreenAPIClient.from_env()
    try:
        await asyncio.sleep(window)
        async with pending_lock:
            b = pending_batches.get(sender)
            if not b or b.get("mode") != "pdf_once":
                return
            job_id = b["job_id"]
            # Notify time over
            try:
                if _is_sender_allowed(sender, db):
                    await client.send_message(chat_id=sender, message="Time over. Creating your PDF now.")
            except Exception:
                pass
            # Move to queue
            db.update_job_status(job_id, "PENDING")
            await job_queue.put(job_id)
            json_log("pdf_once_enqueued", sender=sender, job_id=job_id)
            pending_batches.pop(sender, None)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        json_log("pdf_once_enqueue_error", sender=sender, error=str(e))


def _fmt_mb(nbytes: Optional[Union[int, float]]) -> str:
    try:
        if not nbytes or nbytes <= 0:
            return "unknown"
        return f"{int(round(nbytes / (1024*1024)))}MB"
    except Exception:
        return "unknown"


def _normalize_youtube_url(url: str) -> str:
    """
    Convert youtu.be and shorts URLs to canonical watch?v= form where possible.
    """
    try:
        u = urlparse(url)
        host = (u.netloc or "").lower()
        path = u.path or ""
        if "youtu.be" in host:
            vid = path.strip("/").split("/")[0]
            if vid:
                return f"https://www.youtube.com/watch?v={vid}"
        if "youtube.com" in host and "/shorts/" in path:
            vid = path.split("/shorts/")[1].split("/")[0]
            if vid:
                return f"https://www.youtube.com/watch?v={vid}"
        return url
    except Exception:
        return url


async def _ytdl_prepare_choices(url: str) -> List[Dict[str, Any]]:
    """
    Inspect available formats with yt-dlp -J and pick reasonable 480p and 720p progressive formats.
    Returns a list of dicts: [{"key":"1","label":"480p","format_id":"XXX","size_mb":80}, ...]
    """
    try:
        norm_url = _normalize_youtube_url(url)
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "-J", "--no-playlist", "--force-ipv4",
            "--extractor-args", "youtube:player_client=android",
            norm_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            json_log("ytdl_probe_error", url=norm_url, stderr=stderr.decode("utf-8", "ignore")[:200])
            return []
        import json as _json
        info = _json.loads(stdout.decode("utf-8", "ignore") or "{}")
        formats = info.get("formats") or []
        duration = info.get("duration") or None  # seconds

        # helper to size estimation
        def est_size_bytes(fmt: Dict[str, Any]) -> Optional[int]:
            # direct size fields
            for k in ("filesize", "filesize_approx"):
                v = fmt.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    return int(v)
            # fallback via tbr (kbps) * duration
            tbr = fmt.get("tbr") or fmt.get("abr") or fmt.get("vbr")
            if duration and isinstance(tbr, (int, float)) and tbr > 0:
                # tbr is in kbps → bytes = kbps * 1000/8 * seconds
                return int((tbr * 1000 / 8) * float(duration))
            return None

        # filter progressive if possible (both audio and video present)
        prog = [f for f in formats if (f.get("vcodec") != "none" and f.get("acodec") != "none")]
        if not prog:
            prog = formats

        # choose closest heights
        def pick_closest(target_h: int):
            cand = None
            best_delta = 10**9
            for f in prog:
                h = f.get("height")
                if not isinstance(h, int):
                    continue
                delta = abs(h - target_h)
                # prefer mp4 if tie
                ext = (f.get("ext") or "").lower()
                if (delta < best_delta) or (delta == best_delta and ext == "mp4"):
                    cand = f
                    best_delta = delta
            return cand

        c480 = pick_closest(480)
        c720 = pick_closest(720)

        choices: List[Dict[str, Any]] = []
        idx = 1
        for label, fmt in (("480p", c480), ("720p", c720)):
            if fmt:
                size_b = est_size_bytes(fmt)
                choices.append({
                    "key": str(idx),
                    "label": label,
                    "format_id": fmt.get("format_id"),
                    "size_mb": None if size_b is None else int(round(size_b / (1024*1024))),
                })
                idx += 1
        return choices
    except Exception as e:
        json_log("ytdl_prepare_exception", error=str(e))
        return []


async def _google_images_candidates(query: str) -> List[str]:
    """
    Scrape Google Images results page (tbm=isch) and extract original image URLs.
    Note: This uses simple HTML parsing; may be brittle if Google changes markup.
    """
    try:
        q = quote_plus(query)
        url = f"https://www.google.com/search?tbm=isch&q={q}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as hc:
            r = await hc.get(url, headers=headers)
            if r.status_code != 200:
                return []
            html = r.text

        # Strategy 1: extract from /imgres?imgurl=... links
        import re
        from urllib.parse import unquote, urlparse, parse_qs
        hrefs = re.findall(r'href="/imgres\\?([^"]+)"', html)
        urls: List[str] = []
        for h in hrefs:
            qs = parse_qs(h)
            iu = qs.get("imgurl", [None])[0]
            if iu and iu.startswith("http"):
                try:
                    urls.append(unquote(iu))
                except Exception:
                    urls.append(iu)

        # Strategy 2: fallback to direct img src attributes (thumbnails often, but sometimes originals)
        if not urls:
            srcs = re.findall(r'<img[^>]+src="(https?://[^"]+)"', html)
            urls = [s for s in srcs if s.lower().startswith("http") and "gstatic" not in s.lower()]

        # Dedup and limit
        out: List[str] = []
        seen = set()
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
            if len(out) >= 8:
                break
        return out
    except Exception:
        return []

async def _wiki_image_candidates(query: str) -> List[str]:
    """
    Fetch a few candidate image URLs from Wikimedia Commons for a query.
    Prefer thumbnail (scaled) URLs to avoid huge TIFF originals.
    """
    try:
        params = {
            "action": "query",
            "generator": "search",
            "gsrsearch": query,
            "gsrlimit": "6",
            "gsrnamespace": "6",  # File namespace
            "prop": "imageinfo",
            "iiprop": "url",
            "iiurlwidth": "1280",
            "format": "json",
            "origin": "*",
        }
        async with httpx.AsyncClient(timeout=20) as hc:
            r = await hc.get("https://commons.wikimedia.org/w/api.php", params=params, headers={"User-Agent": "RelayBot/1.0"})
            if r.status_code != 200:
                return []
            data = r.json()
        pages = (data.get("query") or {}).get("pages") or {}
        urls: List[str] = []
        for _, p in pages.items():
            ii = (p.get("imageinfo") or [])
            if ii and isinstance(ii, list):
                # Prefer thumburl (scaled) if available, fall back to original url
                u = ii[0].get("thumburl") or ii[0].get("url")
                if isinstance(u, str) and u.lower().startswith("http"):
                    urls.append(u)
        return urls[:5]
    except Exception:
        return []

async def _search_verify_send_image(sender: str, query: str, prefer_ext: str, db: Database) -> bool:
    """
    Search for an image, download the best candidate, verify it with Gemini, then send.
    - Only accept real image content-types.
    - Always re-encode to a WhatsApp-friendly format (JPEG/PNG) and size (<5MB).
    - Avoid TIFF/SVG/HEIC and other unsupported formats before sending.
    Sends a short 'please wait' message to the user up-front.
    """
    client = GreenAPIClient.from_env()
    # Avoid sending a preliminary message to prevent duplicate-looking replies.

    tmp_dir = storage.base / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # Normalize requested extension
    prefer_ext_norm = "jpg" if prefer_ext.lower() in {"jpg", "jpeg"} else "png"

    # Try sharpening the query with Gemini
    try:
        if GeminiResponder is not None:
            gr = GeminiResponder()
            query = await asyncio.to_thread(gr.rewrite_search_query, query)
    except Exception:
        pass

    # Candidate sources:
    # - Prefer Wikimedia (stable, permissive)
    # - Also try Google Images scrape (best-effort)
    # - Finally, fall back to Unsplash random endpoint as a last resort
    wiki_candidates = await _wiki_image_candidates(query)
    google_candidates = await _google_images_candidates(query)
    candidates: List[str] = []
    # Combine with de-dup keeping order
    seen: set = set()
    for u in (wiki_candidates + google_candidates):
        if isinstance(u, str) and u and u not in seen:
            seen.add(u)
            candidates.append(u)
    # If still nothing, use Unsplash last (rate-limited and may 503)
    if not candidates:
        candidates = [f"https://source.unsplash.com/1280x800/?{quote_plus(query)}"]
    json_log("image_search_candidates", query=query, count=len(candidates))

    # Helper: open, downscale and re-encode to guaranteed-supported format
    def _reencode_supported(src_path: Path, prefer: str = "jpg") -> Path:
        """
        Returns a path to a re-encoded image:
          - JPEG if prefer == 'jpg' (RGB, quality sweep to keep under 5MB)
          - PNG if prefer == 'png' or if image has alpha
        Falls back to original if Pillow cannot process.
        """
        try:
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = 50_000_000
            with Image.open(src_path) as im:
                # Decide output format and mode
                has_alpha = (im.mode in ("RGBA", "LA")) or ("transparency" in im.info)
                target_fmt = "PNG" if (prefer == "png" or has_alpha) else "JPEG"
                # Ensure mode compatible with target
                if target_fmt == "JPEG":
                    im = im.convert("RGB")
                # Resize if very large
                max_side = 1600
                w, h = im.size
                scale = min(1.0, max_side / max(w, h))
                if scale < 1.0:
                    im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
                # Output file path
                out_path = src_path.with_suffix(".png" if target_fmt == "PNG" else ".jpg")
                if target_fmt == "PNG":
                    # PNG compress level; try to keep reasonable size (<5MB) but PNG may be larger
                    im.save(out_path, format="PNG", optimize=True)
                    # If PNG still huge and no alpha, fallback to JPEG
                    if out_path.stat().st_size > 5 * 1024 * 1024 and not has_alpha:
                        out_path.unlink(missing_ok=True)
                        target_fmt = "JPEG"
                        im = im.convert("RGB")
                if target_fmt == "JPEG":
                    for q in (85, 80, 75, 70, 65, 60):
                        im.save(out_path, format="JPEG", quality=q, optimize=True)
                        if out_path.stat().st_size <= 5 * 1024 * 1024:
                            break
                return out_path
        except Exception:
            return src_path

    # Content types we consider acceptable to try to decode
    acceptable_ct_prefix = ("image/",)
    unacceptable_ct = {
        "image/tiff", "image/x-tiff", "image/svg+xml", "image/heic", "image/heif",
        "image/x-icon", "image/vnd.microsoft.icon",
    }

    for idx, url in enumerate(candidates, start=1):
        # Use a neutral temporary name first; we'll re-encode to final extension later
        tmp_name = f"img_{int(datetime.utcnow().timestamp())}_{random.randint(1000,9999)}_{idx}.bin"
        bin_path = tmp_dir / tmp_name
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as hc:
                r = await hc.get(url, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code != 200 or not r.content:
                    json_log("image_candidate_fetch_failed", url=url, status=r.status_code)
                    continue
                ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                json_log("image_candidate_content_type", url=url, content_type=ct or "unknown", size=len(r.content))
                # Skip obvious non-image or unsupported types before writing
                if not any(ct.startswith(p) for p in acceptable_ct_prefix) or ct in unacceptable_ct:
                    json_log("image_candidate_skipped", url=url, content_type=ct or "unknown")
                    continue
                with bin_path.open("wb") as f:
                    f.write(r.content)

            # Always re-encode to supported format/size
            out_proc = _reencode_supported(bin_path, prefer=prefer_ext_norm)

            # Verify with Gemini if available
            verified = True
            reason = "ok"
            if GeminiResponder is not None:
                try:
                    gr = GeminiResponder()
                    verified, reason = await asyncio.to_thread(gr.verify_image_against_query, str(out_proc), query)
                except Exception as e:
                    # If verification fails due to model issues, don't block sending a valid image
                    verified = True
                    reason = f"verify_error_ignored: {e}"

            if not verified:
                json_log("image_candidate_rejected", url=url, reason=reason)
                try:
                    bin_path.unlink(missing_ok=True)
                    if out_proc != bin_path:
                        out_proc.unlink(missing_ok=True)
                except Exception:
                    pass
                continue

            # Prefer direct upload-and-send to ensure WhatsApp treats it as an image and avoid URL/plan issues.
            if _is_sender_allowed(sender, db):
                cap = f"Image for: {query}"
                try:
                    await client.send_file_by_upload(chat_id=sender, file_path=out_proc, caption=cap)
                except Exception:
                    # Fallback: upload to Green API storage then send by image endpoint (with internal fallback to file)
                    up = await client.upload_file(out_proc)
                    await client.send_image_by_url(
                        chat_id=sender,
                        url_file=up.get("urlFile", ""),
                        caption=cap,
                        filename=out_proc.name,
                    )
            try:
                bin_path.unlink(missing_ok=True)
                if out_proc != bin_path:
                    out_proc.unlink(missing_ok=True)
            except Exception:
                pass
            return True
        except Exception as e:
            json_log("image_fetch_error", error=str(e), query=query, source=url)
            try:
                bin_path.unlink(missing_ok=True)
            except Exception:
                pass
            continue

    # If none verified/sent
    if _is_sender_allowed(sender, db):
        try:
            await client.send_message(chat_id=sender, message="I couldn't find a suitable image for that request.")
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
    # Only process incoming messages; ignore outgoing echoes to avoid replying to ourselves
    if str(webhook_type).lower() not in {"incomingmessagereceived"}:
        json_log("webhook_ignored", reason="not_incoming", type=str(webhook_type))
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
        type_message = (message_data.get("typeMessage") or "").lower()
        # Collect known single-media payloads
        candidates = [
            message_data.get("imageMessageData"),
            message_data.get("videoMessageData"),
            message_data.get("fileMessageData"),
            message_data.get("documentMessageData"),
            message_data.get("audioMessageData"),
            message_data.get("voiceMessageData"),  # some providers use this for PTT/voice notes
        ]
        for c in candidates:
            if isinstance(c, dict) and c:
                media_list.append(c)
        # Heuristic: if type mentions 'voice' and we have no explicit payload, treat audioMessageData as voice
        if ("voice" in type_message or "ptt" in type_message) and not media_list:
            amd = message_data.get("audioMessageData")
            if isinstance(amd, dict) and amd:
                media_list.append(amd)
        # Multiple medias array
        if "medias" in message_data and isinstance(message_data["medias"], list):
            media_list.extend([m for m in message_data["medias"] if isinstance(m, dict)])
    except Exception:
        pass

    # Feature: OCR/QA, YouTube, and search handling
    from .ocr_qa import GeminiFileQA, state as qa_state, find_youtube_url

    client = GreenAPIClient.from_env()

    text_msg = _extract_text_from_payload(payload) or ""

    # Simple greeting and math handlers (single concise replies)
    if text_msg:
        try:
            low_txt = text_msg.strip().lower()

            # Greeting intent
            greeting_words = {"hi", "hello", "hey", "good morning", "good afternoon", "good evening"}
            if any(low_txt.startswith(w) or w in low_txt for w in greeting_words):
                if _is_sender_allowed(sender, db) and sender != "unknown":
                    await client.send_message(chat_id=sender, message="Hello! How can I help you today?")
                return {"ok": True, "job_id": None}

            # General math detection and answer (covers 2+2, 7*(3+4), 3^2, etc.)
            math_ans = _maybe_answer_math_text(text_msg)
            if math_ans is not None:
                if _is_sender_allowed(sender, db) and sender != "unknown":
                    await client.send_message(chat_id=sender, message=math_ans)
                return {"ok": True, "job_id": None}

            # If user just says "addition" without numbers, guide them once
            if low_txt.strip() in {"addition", "add"}:
                if _is_sender_allowed(sender, db) and sender != "unknown":
                    await client.send_message(chat_id=sender, message="Send a calculation like 2+2 or 7*(3+4).")
                return {"ok": True, "job_id": None}
        except Exception:
            # fall through to other handlers
            pass

    # One-time PDF packer command: "PDF:N" where N = images per page
    if text_msg:
        m = re.match(r"^\s*pdf\s*:\s*(\d+)\s*$", text_msg, flags=re.IGNORECASE)
        if m:
            try:
                per_page = max(1, min(12, int(m.group(1))))
            except Exception:
                per_page = 4
            # Prepare a dedicated one-time PDF batch; timer will start after first image is received
            async with pending_lock:
                # Cancel existing batch for this sender if any
                prev = pending_batches.get(sender)
                if prev:
                    try:
                        t = prev.get("task")
                        if t:
                            t.cancel()
                    except Exception:
                        pass
                    pending_batches.pop(sender, None)
                # Create a new job and store per-page setting in job logs
                job_id = db.create_job(sender=sender, msg_id=str(msg_id), payload=payload, instance_id=str(instance_id))
                db.append_job_log(job_id, {"pdf_images_per_page": per_page})
                db.update_job_status(job_id, "NEW")
                # Don't start the timer yet; wait for first image
                pending_batches[sender] = {
                    "job_id": job_id,
                    "started_at": now.isoformat(),
                    "task": None,
                    "mode": "pdf_once",
                    "per_page": per_page,
                    "window": 60,
                }
            if _is_sender_allowed(sender, db) and sender != "unknown":
                await client.send_message(
                    chat_id=sender,
                    message=f"PDF mode enabled for one job. Send images within 1 minute after your first image.\nI'll pack {per_page} image(s) per page."
                )
            return {"ok": True, "job_id": job_id}

    # If user sends text (without images) while a one-time PDF batch is active -> do NOT cancel; just inform
    try:
        if text_msg:
            md0 = payload.get("messageData") or {}
            has_image_in_msg = bool(md0.get("imageMessageData")) or (isinstance(md0.get("medias"), list) and any(isinstance(x, dict) and str((x.get("mimeType") or x.get("mimetype") or "")).lower().startswith("image/") for x in md0.get("medias") or []))
            if not has_image_in_msg:
                async with pending_lock:
                    b = pending_batches.get(sender)
                    if b and b.get("mode") == "pdf_once":
                        if _is_sender_allowed(sender, db):
                            await client.send_message(chat_id=sender, message="PDF mode is active. Please continue sending images. Reply 'cancel' to cancel.")
                        return {"ok": True, "job_id": b.get("job_id")}
    except Exception:
        pass

    # If awaiting yt-dlp resolution choice or confirmation
    pending_url = qa_state.get_pending_ytdl(sender)
    if pending_url:
        choice_text = (text_msg or "").strip().lower()
        # Cancellation
        if choice_text in {"cancel", "stop", "no"}:
            qa_state.set_pending_ytdl(sender, None)
            ytdl_pending.pop(sender, None)
            if _is_sender_allowed(sender, db):
                await client.send_message(chat_id=sender, message="Okay, canceled the download.")
            return {"ok": True, "job_id": None}

        # Resolve choice
        entry = ytdl_pending.get(sender)
        selected_fmt = None
        if entry and isinstance(entry.get("choices"), list):
            # numeric choice (1/2/...)
            for c in entry["choices"]:
                if choice_text == str(c.get("key")).strip().lower():
                    selected_fmt = c
                    break
            # resolution text like "480" or "480p"
            if not selected_fmt and any(tok in choice_text for tok in ("480", "480p")):
                for c in entry["choices"]:
                    if str(c.get("label", "")).lower().startswith("480"):
                        selected_fmt = c
                        break
            if not selected_fmt and any(tok in choice_text for tok in ("720", "720p")):
                for c in entry["choices"]:
                    if str(c.get("label", "")).lower().startswith("720"):
                        selected_fmt = c
                        break

        # Fallback: accept yes/ok -> first choice or best<=480
        if not selected_fmt and choice_text in {"yes", "y", "download", "ok"}:
            if entry and entry.get("choices"):
                selected_fmt = entry["choices"][0]
            else:
                selected_fmt = {"format_id": "best[height<=480]", "label": "480p"}

        if not selected_fmt:
            # If we still don't understand, re-show menu if available
            if entry and entry.get("choices") and _is_sender_allowed(sender, db):
                items = []
                for c in entry["choices"]:
                    size_txt = f" (~{c['size_mb']}MB)" if c.get("size_mb") is not None else ""
                    items.append(f"{c['key']}. {c['label']}{size_txt}")
                await client.send_message(chat_id=sender, message="Please reply with one of the options:\n" + "\n".join(items))
            return {"ok": True, "job_id": None}

        json_log("ytdl_choice_selected", sender=sender, choice=selected_fmt.get("label"), fmt=selected_fmt.get("format_id"))

        # download video, upload, send, delete
        try:
            tmp_dir = storage.base / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            out_tpl = str(tmp_dir / "yt_video.%(ext)s")
            fmt_selector = str(selected_fmt.get("format_id") or "best")
            url_norm = _normalize_youtube_url(pending_url)
            # Build exec args to avoid shell quoting issues on Windows
            args = [
                "yt-dlp",
                "--no-playlist",
                "--force-ipv4",
                "--extractor-args", "youtube:player_client=android",
                "--no-part",
                "--retries", "3",
                "--fragment-retries", "3",
                "-f", fmt_selector,
                "-o", out_tpl,
                url_norm,
            ]
            json_log("ytdl_download_started", sender=sender, cmd=" ".join(args))
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await proc.communicate()
            err_txt = stderr.decode('utf-8', 'ignore')
            if proc.returncode != 0:
                json_log("ytdl_download_failed", sender=sender, code=proc.returncode, stderr=err_txt[:300])
                if _is_sender_allowed(sender, db):
                    # Common hint if ffmpeg missing or geo/consent restricted
                    hint = ""
                    if "ffmpeg" in err_txt.lower():
                        hint = " (converter missing on server)"
                    elif "Sign in to confirm" in err_txt or "This video is only available" in err_txt:
                        hint = " (video requires login/consent; cannot fetch without cookies)"
                    await client.send_message(chat_id=sender, message=f"Failed to download video{hint}.")
            else:
                # find the produced file (prefer non-part, newest)
                exts = (".mp4", ".mkv", ".webm", ".mov", ".m4v")
                candidates = [p for p in tmp_dir.glob("yt_video.*") if p.suffix.lower() in exts and not str(p).endswith(".part")]
                candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                if not candidates:
                    json_log("ytdl_download_no_output", sender=sender)
                    if _is_sender_allowed(sender, db):
                        await client.send_message(chat_id=sender, message="Download finished but no file was produced.")
                else:
                    video_path = candidates[0]
                    json_log("ytdl_download_succeeded", sender=sender, file=str(video_path))
                    up = await client.upload_file(video_path)
                    if _is_sender_allowed(sender, db):
                        cap = f"Here is your video ({selected_fmt.get('label','')})."
                        await client.send_file_by_url(chat_id=sender, url_file=up.get("urlFile", ""), filename=video_path.name, caption=cap)
                    try:
                        video_path.unlink()
                    except Exception:
                        pass
            # clear pending
            qa_state.set_pending_ytdl(sender, None)
            ytdl_pending.pop(sender, None)
        except Exception as e:
            json_log("ytdl_download_exception", sender=sender, error=str(e))
            if _is_sender_allowed(sender, db):
                await client.send_message(chat_id=sender, message=f"Error while downloading video: {e}")
            qa_state.set_pending_ytdl(sender, None)
            ytdl_pending.pop(sender, None)
        return {"ok": True, "job_id": None}

    # If text contains a YouTube link
    yt_url = find_youtube_url(text_msg or "")
    if yt_url:
        yt_norm = _normalize_youtube_url(yt_url)
        json_log("youtube_link_detected", sender=sender, url=yt_norm)
        # Prepare choices (480p/720p) and ask user to choose
        choices = await _ytdl_prepare_choices(yt_norm)
        if not choices:
            # fallback to a simple yes/no with default 480p
            qa_state.set_pending_ytdl(sender, yt_norm)
            ytdl_pending[sender] = {"url": yt_norm, "choices": [{"key": "1", "label": "480p", "format_id": "best[height<=480]/best"}]}
            if _is_sender_allowed(sender, db) and sender != "unknown":
                await client.send_message(chat_id=sender, message="You sent a YouTube link. Reply 1 to download at 480p.")
            return {"ok": True, "job_id": None}
        # Save pending menu
        qa_state.set_pending_ytdl(sender, yt_norm)
        ytdl_pending[sender] = {"url": yt_norm, "choices": choices}
        json_log("ytdl_menu_prepared", sender=sender, choices=[{"key": c["key"], "label": c["label"], "size_mb": c.get("size_mb")} for c in choices])
        if _is_sender_allowed(sender, db) and sender != "unknown":
            items = []
            for c in choices:
                size_txt = f" (~{c['size_mb']}MB)" if c.get("size_mb") is not None else ""
                items.append(f"{c['key']}. {c['label']}{size_txt}")
            menu = "Choose a resolution to download:\n" + "\n".join(items)
            await client.send_message(chat_id=sender, message=menu)
        return {"ok": True, "job_id": None}

    # Simple internet search command: "search: ..."
    if text_msg.lower().startswith("search:") or text_msg.lower().startswith("search "):
        raw_query = text_msg.split(":", 1)[1].strip() if ":" in text_msg else text_msg.split(" ", 1)[1].strip()
        # Let Gemma/Gemini sharpen the query
        query = raw_query
        try:
            if GeminiResponder is not None:
                gr = GeminiResponder()
                query = await asyncio.to_thread(gr.rewrite_search_query, raw_query)
        except Exception:
            query = raw_query
        links = await _web_search_links(query)
        if _is_sender_allowed(sender, db):
            if not links:
                await client.send_message(chat_id=sender, message=f"No results found for \"{query}\".")
            else:
                # Nicely formatted top results. We can ask model to create a short intro.
                intro = f"Top results for \"{query}\":"
                try:
                    if GeminiResponder is not None:
                        gr = GeminiResponder()
                        intro = await asyncio.to_thread(
                            gr.generate,
                            f"Write a short, friendly one-line intro for search results about: {query}",
                            "You are a concise assistant."
                        )
                except Exception:
                    pass
                top = links[:8]
                numbered = "\n".join(f"{i+1}. {u}" for i, u in enumerate(top))
                await client.send_message(chat_id=sender, message=f"{intro}\n{numbered}")
        return {"ok": True, "job_id": None}

    # Image fetch command: "image: cats jpg" or "img: cat" or any text mentioning image/photo/picture/img
    low = (text_msg or "").strip().lower()
    def _looks_like_image_intent(s: str) -> bool:
        if s.startswith("image:") or s.startswith("img:"):
            return True
        keywords = ("image", "photo", "picture", "img")
        return any(k in s for k in keywords)
    if text_msg and _looks_like_image_intent(low):
        # Extract the body/query
        if ":" in text_msg[:10]:
            body = text_msg.split(":", 1)[1].strip()
        else:
            body = text_msg.strip()
        # Pick preferred extension (default jpg)
        prefer_ext = "jpg"
        if "png" in low:
            prefer_ext = "png"
        elif "jpeg" in low or "jpg" in low:
            prefer_ext = "jpg"
        json_log("image_search_start", sender=sender, query=body, prefer_ext=prefer_ext)
        ok_img = await _search_verify_send_image(sender, body, prefer_ext, db)
        if ok_img:
            return {"ok": True, "job_id": None}
        json_log("image_search_no_candidates", sender=sender, query=body)
        # if failed, fall through to other handlers

    # Toggle: if pdf_packer_enabled -> existing batching to PDF, else switch to QA mode
    # Default disabled; can be enabled per-chat via a one-time "PDF:N" command
    pdf_packer_enabled = (db.get_setting("pdf_packer_enabled", "0") or "0") == "1"

    # Split media into images vs others (audio/voice/pdf/etc.)
    def _is_image_media(m: Dict[str, Any]) -> bool:
        mt = (m.get("mimeType") or m.get("mimetype") or "").lower()
        if mt.startswith("image/"):
            return True
        name = (m.get("fileName") or m.get("caption") or "").lower()
        return any(name.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"))

    image_media = [m for m in media_list if _is_image_media(m)]
    other_media = [m for m in media_list if not _is_image_media(m)]

    # If we have image media and a one-time PDF batch is active, always append to that batch
    if image_media:
        async with pending_lock:
            b = pending_batches.get(sender)
            if b and b.get("mode") == "pdf_once":
                job_id = b["job_id"]
                for m in image_media:
                    db.add_media(job_id, m)
                # Start countdown timer on first image if not already started
                if not b.get("task"):
                    try:
                        t = asyncio.create_task(_enqueue_pdf_once_later(sender, db, window=int(b.get("window", 60))))
                        b["task"] = t
                        pending_batches[sender] = b
                    except Exception:
                        pass
                    # Notify timer started
                    if _is_sender_allowed(sender, db):
                        try:
                            await client.send_message(chat_id=sender, message="Timer started. I'll create the PDF in 1 minute.")
                        except Exception:
                            pass
                json_log("pdf_once_batch_appended", sender=sender, job_id=job_id, added=len(image_media))
                if not other_media:
                    if _is_sender_allowed(sender, db):
                        try:
                            await client.send_message(chat_id=sender, message=f"Added {len(image_media)} image(s).")
                        except Exception:
                            pass
                    return {"ok": True, "job_id": job_id}

    # If we have image media and packer is enabled, batch only images for PDF
    if image_media and pdf_packer_enabled:
        async with pending_lock:
            batch = pending_batches.get(sender)
            if batch:
                job_id = batch["job_id"]
                for m in image_media:
                    db.add_media(job_id, m)
                json_log("batch_appended", sender=sender, job_id=job_id, added=len(image_media))
                result_job_id = job_id
            else:
                job_id = db.create_job(sender=sender, msg_id=str(msg_id), payload=payload, instance_id=str(instance_id))
                for m in image_media:
                    db.add_media(job_id, m)
                db.update_job_status(job_id, "NEW")
                task = asyncio.create_task(_enqueue_batch_later(sender, db))
                pending_batches[sender] = {"job_id": job_id, "started_at": now.isoformat(), "task": task}
                json_log("batch_started", sender=sender, job_id=job_id, window_seconds=BATCH_WINDOW_SECONDS, medias=len(image_media))
                result_job_id = job_id
        # Continue processing any non-image media immediately below
        if not other_media:
            return {"ok": True, "job_id": result_job_id}

    # If we have any non-image media OR packer is disabled (process all media immediately)
    if other_media or (media_list and not pdf_packer_enabled):
        process_list = other_media if other_media else media_list

        # Inform user that we are processing/converting media (can take time)
        try:
            if _is_sender_allowed(sender, db):
                await client.send_message(chat_id=sender, message="Processing your file(s)… converting formats if needed. Please wait.")
        except Exception:
            pass

        # Immediate download and create a separate session for this message (no batching)
        async with httpx.AsyncClient(timeout=60) as http_client:
            job_id = db.create_job(sender=sender, msg_id=str(msg_id), payload=payload, instance_id=str(instance_id))
            db.update_job_status(job_id, "PROCESSING")
            downloaded: List[Path] = []
            for m in process_list:
                try:
                    fp = await storage.download_media(http_client, m, {"sender": sender, "msg_id": str(msg_id)})
                    db.add_media(job_id, m)
                    downloaded.append(fp)
                except Exception as e:
                    json_log("media_download_error", error=str(e))
            db.update_job_status(job_id, "COMPLETED")

        # Optional conversion: convert audio to mp3 for better support
        async def _convert_audio_to_mp3(path: Path) -> Path:
            try:
                if path.suffix.lower() == ".mp3":
                    return path
                # Use ffmpeg to convert to mono 64kbps mp3
                out = path.with_suffix(".mp3")
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", str(path), "-vn", "-ac", "1", "-b:a", "64k", str(out),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                await proc.communicate()
                if proc.returncode == 0 and out.exists():
                    return out
            except Exception:
                pass
            return path

        # Detect audio files for conversion
        audio_exts = {".oga", ".ogg", ".m4a", ".wav", ".webm", ".aac", ".flac", ".opus"}
        converted: List[Path] = []
        for p in downloaded:
            if p.suffix.lower() in audio_exts:
                try:
                    newp = await _convert_audio_to_mp3(p)
                    converted.append(newp)
                except Exception:
                    converted.append(p)
            else:
                converted.append(p)

        # Keep files Gemini can read for Q&A: PDFs, images, audio, presentations, Word docs, and text
        valid_ext = {
            ".pdf",
            ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp",
            ".mp3", ".wav", ".m4a", ".ogg", ".oga", ".webm",
            ".ppt", ".pptx",
            ".doc", ".docx",
            ".txt"
        }
        valid_files = [p for p in converted if p.suffix.lower() in valid_ext]
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
            storage.delete_files(converted)
            if _is_sender_allowed(sender, db):
                await client.send_message(chat_id=sender, message="I couldn't read the file(s) you sent. Please send PDFs, presentations, Word documents, text, images, or audio.")
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
        # If we have any sessions, answer from the active session (PDF, presentation, doc, text, image, or audio)
        sessions = _state.list_sessions(sender)
        if sessions:
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

    # Use Gemini for general questions when no document session handled it, unless suppressed after PDF generation
    if text_msg and GeminiResponder is not None and _is_sender_allowed(sender, db) and not _is_suppressed_from_gemini(sender):
        try:
            system_prompt = db.get_setting("auto_reply_system_prompt", "") or os.getenv(
                "GEMINI_SYSTEM_PROMPT", "You are a helpful assistant. Identify the user's intent and respond concisely."
            )
            responder = GeminiResponder()
            # Offload blocking SDK call to a thread to keep loop responsive
            reply = await asyncio.to_thread(responder.generate, text_msg, system_prompt)
            await client.send_message(chat_id=sender, message=reply)
            json_log("fallback_gemini_reply_sent", chat_id=sender)
        except Exception as e:
            json_log("fallback_gemini_reply_error", error=str(e))

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

@app.head("/")
async def root_head():
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


@app.get("/health")
async def health():
    return {"ok": True, "version": VERSION}


def run():
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    run()
