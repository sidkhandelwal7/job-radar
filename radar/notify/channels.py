"""Transports (§20). Pluggable: each channel implements send(payload) and optionally poll_actions().

Telegram first (inline buttons), then an iOS Shortcuts webhook (less reliable; no inline actions),
then email (always-works fallback for digests). Credentials come from the environment only — never
from config files that could end up in git.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from typing import Any

import httpx

log = logging.getLogger("radar.notify")


@dataclass
class Payload:
    """Every payload carries: company, title, location, base estimate with confidence, beats_baseline,
    strongest single reason, days to close, and the direct apply link (§20)."""

    tier: str
    title: str
    body_lines: list[str]
    url: str | None = None
    posting_id: int | None = None
    buttons: list[tuple[str, str]] = field(default_factory=list)  # (label, callback_data)
    html: bool = True

    def text(self) -> str:
        return (
            f"{self.title}\n" + "\n".join(self.body_lines) + (f"\n{self.url}" if self.url else "")
        )


class Channel:
    name = "base"

    def available(self) -> bool:
        return False

    def send(self, p: Payload) -> str | None:  # returns external id
        raise NotImplementedError


def chat_from_update(u: dict[str, Any]) -> tuple[str, str] | None:
    """(chat_id, who) if this update carries a private-chat message; used by setup AND by the
    listener so a chat id is captured the instant it is seen, whichever path reads the queue."""
    m = u.get("message") or u.get("edited_message") or {}
    chat = m.get("chat") or {}
    if chat.get("type") == "private" and chat.get("id"):
        who = chat.get("username") or " ".join(
            x for x in (chat.get("first_name"), chat.get("last_name")) if x
        )
        return str(chat["id"]), who or "?"
    return None


class Telegram(Channel):
    name = "telegram"

    def __init__(self) -> None:
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        self.base = f"https://api.telegram.org/bot{self.token}" if self.token else None

    def available(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, p: Payload) -> str | None:
        assert self.base
        import html as htmllib

        esc = htmllib.escape
        text = f"<b>{esc(p.title)}</b>\n" + "\n".join(esc(x) for x in p.body_lines)
        if p.url:
            text += f'\n<a href="{esc(p.url)}">Open posting ↗</a>'
        markup = (
            {"inline_keyboard": [[{"text": lbl, "callback_data": data} for lbl, data in p.buttons]]}
            if p.buttons
            else None
        )
        body: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if markup:
            body["reply_markup"] = markup
        r = httpx.post(f"{self.base}/sendMessage", json=body, timeout=20)
        if r.status_code != 200:
            log.warning("telegram send failed: %s %s", r.status_code, r.text[:200])
            return None
        return str(r.json().get("result", {}).get("message_id"))

    def get_updates(
        self,
        offset: int | None,
        *,
        timeout: int = 0,
        allowed: tuple[str, ...] = ("callback_query",),
    ) -> list[dict[str, Any]]:
        """timeout > 0 = Telegram long-polling: the request blocks until an update arrives or the
        timeout elapses, at the cost of one open HTTPS connection and ~zero CPU."""
        assert self.base
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": json.dumps(list(allowed))}
        if offset is not None:
            params["offset"] = offset
        r = httpx.get(f"{self.base}/getUpdates", params=params, timeout=timeout + 15)
        if r.status_code != 200:
            log.warning("telegram getUpdates failed: %s %s", r.status_code, r.text[:120])
            return []
        return r.json().get("result", [])

    def get_me(self) -> dict[str, Any]:
        assert self.base
        r = httpx.get(f"{self.base}/getMe", timeout=15)
        r.raise_for_status()
        return r.json().get("result", {})

    def discover_chat_id(self) -> tuple[str, str] | None:
        """The private chat that has messaged this bot most recently → (chat_id, who). Needs only the
        token (the chat id is what we're looking for). Does not consume the updates offset."""
        assert self.token
        r = httpx.get(
            f"https://api.telegram.org/bot{self.token}/getUpdates",
            params={"timeout": 0, "allowed_updates": json.dumps(["message"])},
            timeout=20,
        )
        r.raise_for_status()
        for u in reversed(r.json().get("result", [])):  # newest first
            found = chat_from_update(u)
            if found:
                return found
        return None

    def answer_callback(self, callback_id: str, text: str) -> None:
        assert self.base
        httpx.post(
            f"{self.base}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text[:200]},
            timeout=10,
        )

    def edit_markup_done(self, chat_id: str | int, message_id: int, note: str) -> None:
        assert self.base
        httpx.post(
            f"{self.base}/editMessageReplyMarkup",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": [[{"text": note, "callback_data": "noop"}]]},
            },
            timeout=10,
        )


class IOSShortcut(Channel):
    """POST JSON to a webhook URL that an iOS Shortcut / automation listens on (e.g. a Shortcuts
    'Get contents of URL' trigger through a relay you control). Less reliable for time-critical
    delivery and cannot do inline actions — documented in the README."""

    name = "ios_shortcut"

    def __init__(self) -> None:
        self.url = os.environ.get("IOS_SHORTCUT_WEBHOOK_URL")

    def available(self) -> bool:
        return bool(self.url)

    def send(self, p: Payload) -> str | None:
        assert self.url
        r = httpx.post(
            self.url,
            json={
                "tier": p.tier,
                "title": p.title,
                "body": "\n".join(p.body_lines),
                "url": p.url,
                "posting_id": p.posting_id,
            },
            timeout=20,
        )
        return str(r.status_code) if r.status_code < 300 else None


class Email(Channel):
    """SMTP (e.g. Gmail with an app password): SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, NOTIFY_EMAIL_TO."""

    name = "email"

    def __init__(self) -> None:
        self.host = os.environ.get("SMTP_HOST")
        self.port = int(os.environ.get("SMTP_PORT") or 587)
        self.user = os.environ.get("SMTP_USER")
        self.password = os.environ.get("SMTP_PASS")
        self.to = os.environ.get("NOTIFY_EMAIL_TO") or self.user

    def available(self) -> bool:
        return bool(self.host and self.user and self.password and self.to)

    def send(self, p: Payload) -> str | None:
        msg = MIMEText(p.text(), "plain", "utf-8")
        msg["Subject"] = p.title
        msg["From"] = self.user or ""
        msg["To"] = self.to or ""
        with smtplib.SMTP(self.host, self.port, timeout=30) as s:  # type: ignore[arg-type]
            s.starttls()
            s.login(self.user, self.password)  # type: ignore[arg-type]
            s.send_message(msg)
        return "sent"


class FileChannel(Channel):
    """Always available: appends payloads to data/notifications.log so nothing is ever silently lost."""

    name = "file"

    def __init__(self, path: str) -> None:
        self.path = path

    def available(self) -> bool:
        return True

    def send(self, p: Payload) -> str | None:
        from pathlib import Path

        from radar.util import utcnow_iso

        pp = Path(self.path)
        pp.parent.mkdir(parents=True, exist_ok=True)
        with pp.open("a", encoding="utf-8") as f:
            f.write(f"[{utcnow_iso()}] {p.tier.upper()} {p.text()}\n\n")
        return "file"


def all_channels(file_path: str) -> list[Channel]:
    return [Telegram(), IOSShortcut(), Email(), FileChannel(file_path)]
