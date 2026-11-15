"""Private-chat authorization for optional Telegram command polling.

This module never starts polling or changes Telegram settings on import.
"""
import hmac


def is_authorized_update(update: dict, allowed_chat_id: str, allowed_user_id: str) -> bool:
    """Fail closed: only a private message from the configured user in configured chat."""
    if not allowed_chat_id or not allowed_user_id:
        return False
    message = update.get("message")
    if not isinstance(message, dict):
        return False
    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return False
    if chat.get("type") != "private" or sender.get("is_bot") is True:
        return False
    return (
        hmac.compare_digest(str(chat.get("id", "")), str(allowed_chat_id))
        and hmac.compare_digest(str(sender.get("id", "")), str(allowed_user_id))
    )


def authorized_command(update: dict, allowed_chat_id: str, allowed_user_id: str) -> str | None:
    if not is_authorized_update(update, allowed_chat_id, allowed_user_id):
        return None
    message = update["message"]
    text = message.get("text")
    if not isinstance(text, str) or not text.startswith("/"):
        return None
    return text.split(maxsplit=1)[0].split("@", 1)[0]
