import os
from typing import Dict, List, Optional, Tuple

import google.generativeai as genai

from .db import Database


# In-memory rolling chat history for normal (non-Q&A) mode.
# Stores the last 20 messages (user/assistant combined) per chat_id.
_CHAT_HISTORY: Dict[str, List[Dict[str, str]]] = {}


def _append_chat_history(chat_id: str, role: str, content: str, limit: int = 20) -> None:
    if not chat_id:
        return
    items = _CHAT_HISTORY.setdefault(chat_id, [])
    items.append({"role": role, "content": content})
    if len(items) > limit:
        del items[:-limit]


def _get_chat_history(chat_id: Optional[str], limit: int = 20) -> List[Dict[str, str]]:
    if not chat_id:
        return []
    return (_CHAT_HISTORY.get(chat_id) or [])[-limit:]


class GeminiResponder:
    """
    Responder for all intents and features. Always uses Gemini 2.5 Flash-Lite.
    """
    def __init__(self, api_key: Optional[str] = None, model_name: Optional[str] = None):
        db = Database()
        if api_key is None:
            api_key = db.get_setting("GEMINI_API_KEY", None) or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        # Force a single model across the app to avoid duplicate behaviors
        model = "gemini-2.5-flash-lite"
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(model)

    def generate(self, user_text: str, system_prompt: Optional[str] = None, chat_id: Optional[str] = None) -> str:
        """
        Text generation with short-term memory for normal chat mode.
        If chat_id is provided, we include the last 20 messages (user/assistant) as context and
        then append this turn to the rolling memory.
        """
        # Build parts: system, history turns, current user, and assistant cue
        parts: List[object] = []
        if system_prompt:
            parts.append({"text": system_prompt.strip()})

        # Include last N turns as simple "User:" / "Assistant:" text snippets
        history = _get_chat_history(chat_id, limit=20)
        for h in history:
            role = h.get("role", "user")
            content = h.get("content", "")
            prefix = "User" if role == "user" else "Assistant"
            parts.append({"text": f"{prefix}: {content}"})

        # Current user message and assistant cue
        parts.append({"text": f"User: {user_text.strip()}"})
        parts.append({"text": "Assistant:"})

        resp = self.model.generate_content(parts)
        text = ""
        try:
            text = resp.text or ""
        except Exception:
            try:
                text = "".join(p.text for p in resp.candidates[0].content.parts)
            except Exception:
                text = ""
        reply = (text.strip() or "Thanks for your message.")

        # Persist to in-memory history
        if chat_id:
            _append_chat_history(chat_id, "user", user_text.strip())
            _append_chat_history(chat_id, "assistant", reply)

        return reply

    def rewrite_search_query(self, user_query: str) -> str:
        """
        Make search queries sharper and add key terms/aliases.
        """
        try:
            prompt = (
                "Rewrite the following web search query to be concise and specific. "
                "Include key synonyms and proper nouns if relevant. Return only the improved query.\\n\\n"
                f"Query: {user_query}"
            )
            resp = self.model.generate_content(prompt)
            return (resp.text or "").strip() or user_query
        except Exception:
            return user_query

    def verify_image_against_query(self, image_path: str, query: str) -> Tuple[bool, str]:
        """
        Ask the model if the image matches the user's request.
        Returns (is_match, brief_reason).
        """
        try:
            handle = genai.upload_file(path=image_path)
            parts = [
                {"text": (
                    "You are verifying if an image matches a user's request. "
                    "Answer strictly in this JSON format: {\"match\": true|false, \"reason\": \"...\"}. "
                    "Be tolerant of close matches and typical variations. "
                    "If it's generic scenery or unrelated, respond with match:false."
                )},
                handle,
                {"text": f"User request: {query}"},
            ]
            resp = self.model.generate_content(parts)
            txt = (resp.text or "").strip()
            # naive parse
            low = txt.lower()
            is_true = "\"match\": true" in low or "match: true" in low or low.startswith("true")
            reason = txt
            return is_true, reason[:280]
        except Exception as e:
            return False, f"verification_error: {e}"