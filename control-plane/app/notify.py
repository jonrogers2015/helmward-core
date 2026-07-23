"""Telegram notification helper for the Helmward control plane.

Fires and forgets — runs in a background thread so it never blocks a request.
Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from environment; silently
no-ops if either is missing so dev environments stay clean.
"""
from __future__ import annotations

import os
import threading
from typing import Optional

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def _send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import httpx
        httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception:
        pass  # never crash the caller


def notify(text: str) -> None:
    """Send a Telegram message in a background thread (non-blocking)."""
    threading.Thread(target=_send, args=(text,), daemon=True).start()


def approval_pending(approval_id: str, task_id: Optional[str],
                     action: str, details: str = "") -> None:
    """Standard notification for a new pending approval."""
    lines = [
        "🔔 <b>Approval needed</b>",
        f"<b>Action:</b> {action}",
    ]
    if task_id:
        lines.append(f"<b>Task:</b> {task_id}")
    if details:
        lines.append(f"<b>Details:</b> {details}")
    lines.append(f"<b>ID:</b> <code>{approval_id}</code>")
    lines.append("👉 http://127.0.0.1:8080/dashboard.html#approvals")
    notify("\n".join(lines))
