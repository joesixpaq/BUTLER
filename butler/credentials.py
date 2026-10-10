"""Central credential resolution for BUTLER.

Sources, first hit wins:
  1. ~/.config/butler/deepseek.key      explicit override (single line)
  2. ~/.hermes/.env -> DEEPSEEK_API_KEY Hermes's own store (single source of truth)
  3. environment variable DEEPSEEK_API_KEY

Telegram uses the Hermes bot: TELEGRAM_BOT_TOKEN + TELEGRAM_HOME_CHANNEL from ~/.hermes/.env.

Key material is never logged or printed.
"""
import os
import pathlib
import re

HOME = pathlib.Path.home()
HERMES_ENV = HOME / ".hermes" / ".env"
OVERRIDE_KEY = HOME / ".config" / "butler" / "deepseek.key"


def _read_env_file(path: pathlib.Path, name: str) -> str | None:
    try:
        txt = path.read_text(errors="replace")
    except OSError:
        return None
    m = re.search(rf"^{re.escape(name)}\s*=\s*(\S+)", txt, re.M)
    if not m:
        return None
    return m.group(1).strip().strip('"').strip("'")


def get_deepseek_key() -> str | None:
    if OVERRIDE_KEY.exists():
        v = OVERRIDE_KEY.read_text().strip()
        if v:
            return v
    v = _read_env_file(HERMES_ENV, "DEEPSEEK_API_KEY")
    if v:
        return v
    return os.environ.get("DEEPSEEK_API_KEY") or None


def get_telegram() -> tuple[str | None, str | None]:
    """Returns (bot_token, chat_id) from the Hermes store."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN") or _read_env_file(HERMES_ENV, "TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_HOME_CHANNEL") or _read_env_file(HERMES_ENV, "TELEGRAM_HOME_CHANNEL")
    return tok, chat
