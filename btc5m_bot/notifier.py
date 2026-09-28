"""Notifications (log and optional Telegram) and optional Telegram control commands."""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable, Optional

import requests

from .config import TelegramSettings

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class Notifier:
    """Log-only notifier; also the interface for other channels."""

    def send(self, text: str) -> None:
        log.info("notify: %s", text)

    def close(self) -> None:
        pass


class _TelegramApi:
    def __init__(self, settings: TelegramSettings, timeout: float = 10.0):
        self.settings = settings
        self.timeout = timeout
        self.session = requests.Session()

    def _redact(self, text: Any) -> str:
        return str(text).replace(self.settings.token, "***")

    def call(self, method: str, payload: dict[str, Any], timeout: Optional[float] = None) -> Optional[dict[str, Any]]:
        url = f"{TELEGRAM_API}/bot{self.settings.token}/{method}"
        try:
            r = self.session.post(url, json=payload, timeout=timeout or self.timeout)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("telegram %s failed: %s", method, self._redact(e))
            return None
        if not data.get("ok"):
            log.warning("telegram %s error: %s", method, self._redact(data.get("description")))
            return None
        return data

    def send_message(self, text: str, thread_id: Optional[int] = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": self.settings.chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        thread_id = thread_id if thread_id is not None else self.settings.thread_id
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        self.call("sendMessage", payload)


class TelegramNotifier(Notifier):
    """Sends from a background thread so a slow Telegram API never delays trading."""

    def __init__(self, settings: TelegramSettings, prefix: str = "BTC5m"):
        self.api = _TelegramApi(settings)
        self.prefix = prefix
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=200)
        self._thread = threading.Thread(target=self._worker, name="telegram-notify", daemon=True)
        self._thread.start()

    def send(self, text: str) -> None:
        super().send(text)
        try:
            self._queue.put_nowait(f"{self.prefix} | {text}")
        except queue.Full:
            log.warning("telegram queue full, message dropped")

    def _worker(self) -> None:
        while True:
            text = self._queue.get()
            if text is None:
                return
            self.api.send_message(text)

    def close(self, timeout: float = 5.0) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            return
        self._thread.join(timeout)


class TelegramCommands:
    """Long-polls getUpdates and answers commands sent from the configured chat only.

    Telegram allows one getUpdates consumer per bot token: use a token that no other
    service (e.g. an agent already bound to the same bot) is polling.
    """

    def __init__(self, settings: TelegramSettings, handler: Callable[[str], str]):
        self.settings = settings
        self.handler = handler
        self.api = _TelegramApi(settings)
        self._offset: Optional[int] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="telegram-commands", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _skip_backlog(self) -> None:
        data = self.api.call("getUpdates", {"offset": -1, "timeout": 0})
        results = (data or {}).get("result") or []
        if results:
            self._offset = int(results[-1]["update_id"]) + 1

    def handle_update(self, update: dict[str, Any]) -> None:
        self._offset = int(update["update_id"]) + 1
        msg = update.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(self.settings.chat_id):
            return
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        command = text.split()[0].split("@")[0].lower()
        try:
            reply = self.handler(command)
        except Exception as e:
            log.exception("command %s failed", command)
            reply = f"error: {e}"
        if reply:
            self.api.send_message(reply, thread_id=msg.get("message_thread_id"))

    def _run(self) -> None:
        self._skip_backlog()
        while not self._stop.is_set():
            payload: dict[str, Any] = {"timeout": 25, "allowed_updates": ["message"]}
            if self._offset is not None:
                payload["offset"] = self._offset
            data = self.api.call("getUpdates", payload, timeout=35)
            if data is None:
                self._stop.wait(10)
                continue
            for update in data.get("result") or []:
                self.handle_update(update)
