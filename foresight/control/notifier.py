"""Real-time notifications: dashboard (always), webhook (Slack, Discord or any JSON endpoint),
and email over SMTP. Delivery runs in the background and never blocks the control loop."""
from __future__ import annotations

import asyncio
import os
import smtplib
import time
from email.message import EmailMessage

import aiohttp

from foresight.control.store import Store

DEDUP_S = 30.0


def webhook_payload(url: str, note: dict) -> dict:
    text = f"[{note['level'].upper()}] {note['title']}\n{note['body']}"
    if "discord.com" in url or "discordapp.com" in url:
        return {"content": text[:1900]}
    if "slack.com" in url:
        return {"text": text}
    return {"text": text, **{k: note[k] for k in ("level", "title", "body", "ts", "incident_id")}}


class Notifier:
    def __init__(self, store: Store, session: aiohttp.ClientSession) -> None:
        self.store = store
        self.session = session
        self.subscribers: list = []
        self.recent: dict[str, float] = {}
        self.tasks: set[asyncio.Task] = set()

    @property
    def webhook_url(self) -> str:
        return self.store.get_setting("webhook_url", "") or ""

    @staticmethod
    def smtp_config() -> dict | None:
        host, to = os.environ.get("FORESIGHT_SMTP_HOST"), os.environ.get("FORESIGHT_SMTP_TO")
        if not host or not to:
            return None
        return {
            "host": host, "port": int(os.environ.get("FORESIGHT_SMTP_PORT", "587")),
            "user": os.environ.get("FORESIGHT_SMTP_USER", ""),
            "password": os.environ.get("FORESIGHT_SMTP_PASSWORD", ""),
            "sender": os.environ.get("FORESIGHT_SMTP_FROM", os.environ.get("FORESIGHT_SMTP_USER", "foresight@localhost")),
            "to": to, "tls": os.environ.get("FORESIGHT_SMTP_TLS", "1") == "1",
        }

    def notify(self, level: str, title: str, body: str, incident_id: int | None = None, dedup_key: str | None = None) -> dict | None:
        key = dedup_key or f"{level}:{title}"
        now = time.time()
        if now - self.recent.get(key, 0.0) < DEDUP_S:
            return None
        self.recent[key] = now
        note = {"ts": now, "level": level, "title": title, "body": body, "incident_id": incident_id, "delivery": {}}
        note["id"] = self.store.add_notification(note)
        for fn in list(self.subscribers):
            fn(note)
        t = asyncio.create_task(self._deliver(note))
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return note

    async def _deliver(self, note: dict) -> None:
        url = self.webhook_url
        changed = False
        if url:
            note["delivery"]["webhook"] = await self._post_webhook(url, note)
            changed = True
        smtp = self.smtp_config()
        if smtp and note["level"] in ("warning", "critical"):
            note["delivery"]["email"] = await asyncio.to_thread(self._send_email, smtp, note)
            changed = True
        if changed:
            self.store.update_notification(note["id"], note)

    async def _post_webhook(self, url: str, note: dict) -> str:
        for attempt in range(3):
            try:
                async with self.session.post(url, json=webhook_payload(url, note),
                                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status < 300:
                        return "sent"
                    result = f"rejected with HTTP {r.status}"
                    if r.status < 500:
                        return result
            except Exception as e:  # noqa: BLE001 - reported in the notification record
                result = f"failed: {type(e).__name__}"
            await asyncio.sleep(1.5 * (attempt + 1))
        return result

    @staticmethod
    def _send_email(smtp: dict, note: dict) -> str:
        msg = EmailMessage()
        msg["Subject"] = f"[Academia Shield {note['level']}] {note['title']}"
        msg["From"] = smtp["sender"]
        msg["To"] = smtp["to"]
        msg.set_content(note["body"])
        try:
            with smtplib.SMTP(smtp["host"], smtp["port"], timeout=10) as s:
                if smtp["tls"]:
                    s.starttls()
                if smtp["user"]:
                    s.login(smtp["user"], smtp["password"])
                s.send_message(msg)
            return "sent"
        except Exception as e:  # noqa: BLE001 - reported in the notification record
            return f"failed: {type(e).__name__}"
