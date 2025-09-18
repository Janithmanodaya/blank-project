import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import google.generativeai as genai

from .db import Database


# Simple in-memory per-chat state for resource-based Q&A and pending actions
class ChatState:
    def __init__(self):
        self.files: Dict[str, List[Path]] = {}
        self.gemini_files: Dict[str, List[object]] = {}  # uploaded Gemini file handles
        self.pending_ytdl: Dict[str, Optional[str]] = {}

    def add_files(self, chat_id: str, paths: List[Path]):
        self.files.setdefault(chat_id, []).extend(paths)

    def get_files(self, chat_id: str) -> List[Path]:
        return self.files.get(chat_id, [])

    def clear(self, chat_id: str):
        self.files.pop(chat_id, None)
        self.pending_ytdl.pop(chat_id, None)
        # NOTE: we do not call genai.delete_file on uploaded handles to avoid requiring extra perms
        self.gemini_files.pop(chat_id, None)

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
        model = model_name or db.get_setting("GEMINI_MODEL", None) or os.getenv("GEMINI_MODEL") or "gemini-1.5-flash"
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(model)

    def _upload_for_chat(self, chat_id: str, files: List[Path]) -> List[object]:
        uploaded = state.gemini_files.get(chat_id)
        if uploaded is not None and len(uploaded) > 0:
            return uploaded
        handles = []
        for p in files:
            try:
                handle = genai.upload_file(path=str(p))
                handles.append(handle)
            except Exception:
                # If upload fails for an image, try passing PIL image? Keep it simple: skip
                continue
        state.gemini_files[chat_id] = handles
        return handles

    def answer(self, chat_id: str, question: str, system_prompt: Optional[str] = None) -> str:
        files = state.get_files(chat_id)
        if not files:
            return "No resources available. Send an image or PDF first."
        handles = self._upload_for_chat(chat_id, files)
        if not handles:
            return "Couldn't prepare the files for answering. Please try re-sending the file(s)."
        instructions = system_prompt or "Answer strictly and only using the provided files. If the information is not present, say you don't know."
        parts = [{"text": instructions}]
        # Add user question last
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


YOUTUBE_RE = re.compile(r"https?://(?:www\\.)?(?:youtube\\.com/watch\\?v=[\\w-]+|youtu\\.be/[\\w-]+)[^\\s]*", re.IGNORECASE)


def find_youtube_url(text: str) -> Optional[str]:
    m = YOUTUBE_RE.search(text or "")
    return m.group(0) if m else None