import os
from typing import Optional

import google.generativeai as genai

from .db import Database


class GeminiResponder:
    """
    Responder for general intents, classification, and non-document answers.
    Always uses Gemma 3n by default regardless of the UI model selection, per requirements.
    """
    def __init__(self, api_key: Optional[str] = None, model_name: Optional[str] = None):
        db = Database()
        if api_key is None:
            api_key = db.get_setting("GEMINI_API_KEY", None) or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        # Force default to Gemma 3n unless explicitly overridden by parameter
        model = model_name or "gemma-3n"
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(model)

    def generate(self, user_text: str, system_prompt: Optional[str] = None) -> str:
        # Use multimodal-capable path; keep simple text for now
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