from typing import Optional


def normalize_whatsapp_id(chat_id: Optional[str]) -> Optional[str]:
    """
    Convert WhatsApp chat IDs like '94770889232@c.us' or '94770889232@us'
    into a simple E.164-like phone representation: '+94770889232'.

    - Keeps only the part before '@'
    - Strips spaces, dashes, parentheses
    - Ensures a leading '+'
    - If input is already like '+94770889232', returns as-is
    - If input is None or empty, returns None
    """
    if not chat_id:
        return None
    s = str(chat_id).strip()
    if not s:
        return None
    # Keep only the local part for personal chats
    local = s.split("@", 1)[0]
    # Remove formatting characters
    cleaned = (
        local.replace(" ", "")
        .replace("-", "")
        .replace("(", "")
        .replace(")", "")
    )
    if not cleaned:
        return None
    # Avoid duplicating '+'
    if cleaned.startswith("+"):
        return cleaned
    # Only digits -> prefix with '+'
    # If there are non-digits (rare), fall back to original without domain
    if cleaned.isdigit():
        return f"+{cleaned}"
    return f"+{''.join(ch for ch in cleaned if ch.isdigit())}" or cleaned


def to_chat_jid(chat: Optional[str]) -> Optional[str]:
    """
    Convert '+94770889232' to '94770889232@c.us' for Green API when sending.
    If value already looks like a JID with '@', return as-is.
    """
    if not chat:
        return None
    s = str(chat).strip()
    if "@" in s:
        return s
    # Strip '+' and any formatting chars, then append '@c.us'
    local = normalize_whatsapp_id(s) or s
    local = local.lstrip("+")
    return f"{local}@c.us"