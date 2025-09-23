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
        # rolling Q&A history (last 20) per chat_id per session_id
        # each item: {"ts": float, "q": str, "a": str, "corr": Optional[str]}
        self.history: Dict[str, Dict[str, List[Dict[str, Optional[str]]]]] = {}

    def _history_path(self, chat_id: str, session_id: str) -> Path:
        base = Storage().base / "qa_history"
        base.mkdir(parents=True, exist_ok=True)
        safe_chat = "".join(c for c in chat_id if c.isalnum() or c in ("@", "_", "-", "."))[:80]
        safe_sid = "".join(c for c in session_id if c.isalnum() or c in ("@", "_", "-", "."))[:40]
        return base / f"{safe_chat}__{safe_sid}.json"

    def _load_history_if_needed(self, chat_id: str, session_id: str) -> List[Dict[str, Optional[str]]]:
        chat_hist = self.history.setdefault(chat_id, {})
        if session_id in chat_hist:
            return chat_hist[session_id]
        # try load from disk
        p = self._history_path(chat_id, session_id)
        items: List[Dict[str, Optional[str]]] = []
        try:
            if p.exists():
                import json
                items = json.loads(p.read_text(encoding="utf-8")) or []
                # ensure structure
                if not isinstance(items, list):
                    items = []
        except Exception:
            items = []
        chat_hist[session_id] = items
        return items

    def get_recent_history(self, chat_id: str, session_id: str, limit: int = 20) -> List[Dict[str, Optional[str]]]:
        items = list(self._load_history_if_needed(chat_id, session_id))
        return items[-limit:]

    def append_history(self, chat_id: str, session_id: str, question: str, answer: str, correction: Optional[str]):
        items = self._load_history_if_needed(chat_id, session_id)
        items.append({"ts": time.time(), "q": question, "a": answer, "corr": correction})
        # keep only last 20
        if len(items) > 20:
            del items[:-20]
        # persist
        try:
            import json
            p = self._history_path(chat_id, session_id)
            p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

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

    def answer_with_correction(
        self,
        chat_id: str,
        question: str,
        system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Tuple[str, Optional[str]]:
        """
        Returns a tuple: (answer_from_files, corrected_answer_or_None).

        - The first answer is STRICTLY from the provided files (same as answer()).
        - The second answer, if present, is a concise corrected/verified answer based on model knowledge.
          If the model believes the file-derived answer is fully correct, None is returned.
        """
        # First, get the strict/file-based answer.
        file_only_answer = self.answer(chat_id, question, system_prompt, session_id)

        # If the first step failed in a clear way, do not attempt correction.
        if file_only_answer.startswith("Error while answering") or "can't find that file" in file_only_answer or "Couldn't read the file" in file_only_answer:
            return file_only_answer, None

        # Now ask the model to verify and, if needed, provide a corrected answer.
        try:
            verify_instructions = (
                "You will be given a user question and an answer derived strictly from the user's files. "
                "Act as a careful reviewer with general world knowledge. "
                "1) If the file-derived answer is fully correct, reply with EXACT_OK and nothing else. "
                "2) If the answer appears incomplete, incorrect, unsafe, or outdated, provide a short, corrected answer. "
                "Keep the corrected answer concise and clear in the user's language. Do not include references."
            )
            parts: List[object] = [{"text": verify_instructions}]
            parts.append({"text": f"User question: {question}"})
            parts.append({"text": f"Answer from files: {file_only_answer}"})
            resp = self.model.generate_content(parts)
            corr = ""
            try:
                corr = (resp.text or "").strip()
            except Exception:
                corr = ""
            if not corr or corr.upper().startswith("EXACT_OK"):
                corr_opt: Optional[str] = None
            else:
                corr_opt = corr

            # Save to rolling history (last 20)
            try:
                state.append_history(chat_id, state.get_session(chat_id, session_id).id if state.get_session(chat_id, session_id) else "unknown", question, file_only_answer, corr_opt)
            except Exception:
                pass

            return file_only_answer, corr_opt
        except Exception:
            # If verification fails, just return the file-only answer.
            try:
                state.append_history(chat_id, state.get_session(chat_id, session_id).id if state.get_session(chat_id, session_id) else "unknown", question, file_only_answer, None)
            except Exception:
                pass
            return file_only_answer, None


# Broader YouTube URL matcher: supports watch, youtu.be, shorts, and mobile links with extra params
YOUTUBE_RE = re.compile(
    r"(https?://(?:www\\.)?(?:m\\.)?(?:youtube\\.com/(?:watch\\?[^ \\n]+|shorts/[^ \\n]+)|youtu\\.be/[^ \\n]+))",
    re.IGNORECASE,
)

def find_youtube_url(text: str) -> Optional[str]:
    m = YOUTUBE_RE.search(text or "")
    return m.group(1) if m else None