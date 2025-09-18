import os
from typing import Optional

import google.generativeai as genai

from .db import Database


class GeminiResponder:
    """
    Responder for general intents, classification, and non-document answers.
    Uses a configurable Gemini model; defaults to 'gemini-1.5-flash' which supports generateContent.
    """
    def __init__(self, api_key: Optional[str] = None, model_name: Optional[str] = None):
        db = Database()
        if api_key is None:
            api_key = db.get_setting("GEMINI_API_KEY", None) or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        # Prefer explicit parameter, then DB/env, then sane default
        configured = (
            model_name
            or db.get_setting("GEMINI_MODEL", None)
            or os.getenv("GEMINI_MODEL")
        )
        model = configured or "gemini-1.5-flash"
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(model)

    def generate(self, user_text: str, system_prompt: Optional[str] = None) -> str:
        # Simple text generation path
        prompt = ""
        if system_prompt:
            prompt += f"{system_prompt.strip()}\\n\\n"
        prompt += f"User: {user_text.strip()}\\nAssistant:"
        resp = self.model.generate_content(prompt)
        text = ""
        try:
            text = resp.text or ""
        except Exception:
            try:
                text = "".join(p.text for p in resp.candidates[0].content.parts)
            except Exception:
                text = ""
        return text.strip() or "Thanks for your message."