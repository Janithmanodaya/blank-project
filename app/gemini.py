import os
from typing import Optional, Tuple

import google.generativeai as genai

from .db import Database


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

    def rewrite_search_query(self, user_query: str) -> str:
        """
        Make search queries sharper and add key terms/aliases.
        """
        try:
            prompt = (
                "Rewrite the following web search query to be concise and specific. "
                "Include key synonyms and proper nouns if relevant. Return only the improved query.\n\n"
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