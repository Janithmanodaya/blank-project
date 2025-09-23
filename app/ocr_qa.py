import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import google.generativeai as genai

from .db import Database
from .storage import Storage


# Sessions keep files separate per chat. Each session has its own files and timestamp.
@dataclass
class Session:
    id: str
    files: List[Path]
    created_at: float  # epoch seconds


class ChatState:
    def __init__(self):
        # sessions per chat_id
        self.sessions: Dict[str, Dict[str, Session]] = {}
        self.active: Dict[str, str] = {}
        # gemini uploaded handles per chat_id per session_id
        self.gemini_files: Dict[str, Dict[str, List[object]]] = {}
        self.pending_ytdl: Dict[str, Optional[str]] = {}

    def create_session(self, chat_id: str, session_id: str, paths: List[Path]):
        sess = Session(id=session_id, files=list(paths), created_at=time.time())
        self.sessions.setdefault(chat_id, {})[session_id] = sess
        # make this the active session
        self.active[chat_id] = session_id

    def get_session(self, chat_id: str, session_id: Optional[str]) -> Optional[Session]:
        if session_id:
            return (self.sessions.get(chat_id) or {}).get(session_id)
        # fallback to active
        sid = self.active.get(chat_id)
        if not sid:
            # fallback to latest by created time
            m = self.sessions.get(chat_id) or {}
            if not m:
                return None
            sid = sorted(m.values(), key=lambda s: s.created_at, reverse=True)[0].id
            self.active[chat_id] = sid
        return (self.sessions.get(chat_id) or {}).get(sid)

    def list_sessions(self, chat_id: str) -> List[Session]:
        return sorted((self.sessions.get(chat_id) or {}).values(), key=lambda s: s.created_at, reverse=True)

    def set_active(self, chat_id: str, session_id: str) -> bool:
        if (self.sessions.get(chat_id) or {}).get(session_id):
            self.active[chat_id] = session_id
            return True
        return False

    def delete_session(self, chat_id: str, session_id: str, storage: Storage):
        sess = (self.sessions.get(chat_id) or {}).pop(session_id, None)
        if not sess:
            return
        # delete files
        storage.delete_files(sess.files)
        # clear handles
        try:
            self.gemini_files.get(chat_id, {}).pop(session_id, None)
        except Exception:
            pass
        # adjust active if needed
        if self.active.get(chat_id) == session_id:
            self.active.pop(chat_id, None)

    def clear_all(self, chat_id: str, storage: Storage):
        for sid in list((self.sessions.get(chat_id) or {}).keys()):
            self.delete_session(chat_id, sid, storage)
        self.sessions.pop(chat_id, None)
        self.gemini_files.pop(chat_id, None)
        self.pending_ytdl.pop(chat_id, None)

    def purge_old(self, storage: Storage, max_age_seconds: int = 24 * 3600):
        now = time.time()
        for chat_id in list(self.sessions.keys()):
            for sid, sess in list(self.sessions[chat_id].items()):
                if now - sess.created_at >= max_age_seconds:
                    self.delete_session(chat_id, sid, storage)

    def set_pending_ytdl(self, chat_id: str, url: Optional[str]):
        if url:
            self.pending_ytdl[chat_id] = url
        else:
            self.pending_ytdl.pop(chat_id, None)

    def get_pending_ytdl(self, chat_id: str) -> Optional[str]:
        return self.pending_ytdl.get(chat_id)


state = ChatState()


class GeminiFileQA:
    def __init__(self, model_name: Optional[str] = None):
        db = Database()
        api_key = db.get_setting("GEMINI_API_KEY", None) or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        # Prefer explicit parameter, then DB/env, then sane default
        configured = (
            model_name
            or db.get_setting("GEMINI_MODEL", None)
            or os.getenv("GEMINI_MODEL")
        )
        # Default to a multimodal model suitable for PDFs/images and multilingual (Sinhala) answers
        model = configured or "gemini-2.5-flash-lite"
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(model)

    def _upload_for_session(self, chat_id: str, session_id: str, files: List[Path]) -> List[object]:
        uploaded_by_chat = state.gemini_files.setdefault(chat_id, {})
        cached = uploaded_by_chat.get(session_id)
        if cached is not None and len(cached) > 0:
            return cached
        handles = []
        for p in files:
            try:
                handle = genai.upload_file(path=str(p))
                handles.append(handle)
            except Exception:
                continue
        uploaded_by_chat[session_id] = handles
        return handles

    def answer(self, chat_id: str, question: str, system_prompt: Optional[str] = None, session_id: Optional[str] = None) -> str:
        sess = state.get_session(chat_id, session_id)
        if not sess:
            return "I can't find that file. Can you send it again?"
        files = sess.files
        if not files:
            return "I can't find that file. Can you send it again?"
        handles = self._upload_for_session(chat_id, sess.id, files)
        if not handles:
            return "Couldn't read the file(s). Please try sending them again."
        # Instruction upgraded to support Sinhala and any language explicitly.
        instructions = system_prompt or (
            "You are a helpful assistant that answers STRICTLY using the provided files. "
            "Detect the user's requested language and respond in that language. "
            "If the user requests Sinhala (සිංහල) translation or explanation, answer in natural, fluent Sinhala. "
            "Translate as needed even if the source content is in another language. "
            "If the requested information is not present in the files, say you don't know."
        )
        # Use list-of-parts; google-generativeai supports passing a list
        parts: List[object] = [{"text": instructions}]
        parts.extend(handles)
        parts.append({"text": f"User question: {question}"})
        try:
            resp = self.model.generate_content(parts)
            text = ""
            try:
                text = resp.text or ""
            except Exception:
                text = ""
            return text.strip() or "I couldn't extract an answer from the provided files."
        except Exception as e:
            return f"Error while answering: {e}"


# Broader YouTube URL matcher: supports watch, youtu.be, shorts, and mobile links with extra params
YOUTUBE_RE = re.compile(
    r"(https?://(?:www\\.)?(?:m\\.)?(?:youtube\\.com/(?:watch\\?[^ \\n]+|shorts/[^ \\n]+)|youtu\\.be/[^ \\n]+))",
    re.IGNORECASE,
)

def find_youtube_url(text: str) -> Optional[str]:
    m = YOUTUBE_RE.search(text or "")
    return m.group(1) if m else None