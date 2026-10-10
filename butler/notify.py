"""Telegram notifications via the Hermes bot (token/chat from ~/.hermes/.env)."""
import json
import urllib.request

from .credentials import get_telegram


def send_telegram(text: str) -> bool:
    tok, chat = get_telegram()
    if not (tok and chat):
        return False
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{tok}/sendMessage",
        data=json.dumps({"chat_id": chat, "text": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return r.status == 200
    except Exception:
        return False
