"""Minimal Telegram client: send proposals with Approve/Reject buttons,
read button taps and a few commands. Only your own chat is obeyed."""
import logging
import time

import requests

log = logging.getLogger(__name__)


class Telegram:
    def __init__(self, token, chat_id):
        self.base = f"https://api.telegram.org/bot{token}" if token else None
        self.chat_id = str(chat_id) if chat_id else None
        self.offset = 0
        self._warned = False

    @property
    def enabled(self):
        return bool(self.base and self.chat_id)

    def send(self, text, buttons=None):
        if not self.enabled:
            log.info("[telegram disabled] %s", text)
            return
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [buttons]}
        try:
            r = requests.post(f"{self.base}/sendMessage", json=payload, timeout=20)
            if r.status_code == 429:  # Telegram rate limit: wait and retry once
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                time.sleep(min(wait, 30))
                r = requests.post(f"{self.base}/sendMessage", json=payload, timeout=20)
            if not r.ok:
                log.error("telegram send failed: %s", r.text[:200])
        except requests.RequestException as exc:
            log.warning("telegram send failed: %s", exc)

    def updates(self):
        """Returns list of ('button', data) or ('command', text) from YOUR chat only."""
        if not self.base:
            return []
        try:
            r = requests.get(f"{self.base}/getUpdates",
                             params={"offset": self.offset, "timeout": 0}, timeout=20)
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("telegram poll failed: %s", exc)
            return []
        if not data.get("ok"):
            desc = data.get("description", "unknown")
            if data.get("error_code") == 429:
                log.warning("telegram rate limit: %s", desc)
            elif not self._warned:
                hint = " - check TELEGRAM_BOT_TOKEN" if data.get("error_code") == 401 else ""
                log.error("TELEGRAM ERROR: %s%s", desc, hint)
                self._warned = True
            return []
        self._warned = False
        items = data.get("result", [])
        events = []
        for u in items:
            self.offset = u["update_id"] + 1
            if "callback_query" in u:
                cq = u["callback_query"]
                if str(cq["message"]["chat"]["id"]) != self.chat_id:
                    continue
                requests.post(f"{self.base}/answerCallbackQuery",
                              json={"callback_query_id": cq["id"]}, timeout=20)
                events.append(("button", cq.get("data", "")))
            elif "message" in u:
                msg = u["message"]
                chat = str(msg["chat"]["id"])
                if not self.chat_id:
                    log.info("message from chat id %s - put this in TELEGRAM_CHAT_ID", chat)
                    continue
                if chat == self.chat_id and msg.get("text", "").startswith("/"):
                    events.append(("command", msg["text"].strip()))
        return events
