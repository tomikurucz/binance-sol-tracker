"""
Push ertesites kuldese a telefonra - tobb szolgaltatast tamogat.

Beallitas .env fajlban vagy kornyezeti valtozokban (mind opcionalis,
amelyik be van allitva, azon a csatornan megy az ertesites):

    # 1) ntfy.sh  - INGYENES, nem kell regisztracio (ajanlott kezdesnek)
    #    Telepitsd a "ntfy" appot a telefonra, es iratkozz fel a topicra.
    NTFY_TOPIC=sol-pivot-jelezes-9f3a2c

    # 2) Telegram bot - @BotFather-nal csinalj botot
    TELEGRAM_BOT_TOKEN=123456:ABC...
    TELEGRAM_CHAT_ID=987654321

    # 3) Discord webhook
    DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

    # 4) Pushover (fizetos, de nagyon megbizhato)
    PUSHOVER_TOKEN=...
    PUSHOVER_USER=...
"""

import os
import re
from typing import List, Optional

import requests

ENV_FILE = ".env"

# Az ntfy topic neve csak ezeket tartalmazhatja (kulonben a HTTP POST 400-at ad).
_TOPIC_RE = re.compile(r"\A[-_A-Za-z0-9]{1,64}\Z")


def _validate_topic(topic: str) -> None:
    """
    Ellenorzi az ntfy topic nevet. A tipikus hiba: a teljes 'NTFY_TOPIC=...' sort
    masoltad be ertekkent, vagy szokoz / sortores kerult a vegere.
    FONTOS: a topic erteket soha nem irjuk ki (a log publikus lehet).
    """
    if _TOPIC_RE.match(topic):
        return
    raise ValueError(
        f"NTFY_TOPIC hibas formatum: hossz={len(topic)}, "
        f"szokoz/sortores={any(ch.isspace() for ch in topic)}, "
        f"'=' jel={'=' in topic}. "
        "Csak betu, szam, '-' es '_' engedelyezett (max 64 karakter)."
    )


def load_env(path: str = ENV_FILE) -> None:
    """Egyszeru .env betolto (kulcs=ertek soronkent, # = komment)."""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ[key] = value


def _ascii(text: str) -> str:
    """HTTP headerekhez ASCII-biztos szoveg (eKEzetek nelkul)."""
    return text.encode("ascii", "ignore").decode("ascii").strip()


class Notifier:
    """Tobb csatornan kuld ertesitest; amelyik nincs beallitva, azt kihagyja."""

    def __init__(self, session: Optional[requests.Session] = None, timeout: int = 10):
        self.session = session or requests.Session()
        self.timeout = timeout

    @property
    def channels(self) -> List[str]:
        found = []
        if os.environ.get("NTFY_TOPIC"):
            found.append("ntfy")
        if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
            found.append("telegram")
        if os.environ.get("DISCORD_WEBHOOK_URL"):
            found.append("discord")
        if os.environ.get("PUSHOVER_TOKEN") and os.environ.get("PUSHOVER_USER"):
            found.append("pushover")
        return found

    def send(self, title: str, message: str, priority: str = "default",
             tags: Optional[List[str]] = None) -> List[str]:
        """Elkuld minden beallitott csatornara. Visszaadja a sikeres csatornakat."""
        sent = []
        for chan in self.channels:
            try:
                getattr(self, "_send_" + chan)(title, message, priority, tags or [])
                sent.append(chan)
            except requests.exceptions.RequestException as exc:
                print(f"[notify] {chan} hiba: {exc}")
            except Exception as exc:  # pl. hianyzo valtozo, JSON hiba
                print(f"[notify] {chan} hiba: {exc}")
        return sent

    # ── ntfy.sh ──────────────────────────────────────────────
    def _send_ntfy(self, title, message, priority, tags):
        topic = os.environ["NTFY_TOPIC"]
        _validate_topic(topic)
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        headers = {
            "Title": _ascii(title),
            "Priority": "high" if priority == "high" else "default",
            "Tags": ",".join(tags) if tags else "chart_with_upwards_trend",
        }
        token = os.environ.get("NTFY_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = self.session.post(f"{server}/{topic}", data=message.encode("utf-8"),
                                 headers=headers, timeout=self.timeout)
        resp.raise_for_status()

    # ── Telegram ─────────────────────────────────────────────
    def _send_telegram(self, title, message, priority, tags):
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = os.environ["TELEGRAM_CHAT_ID"]
        resp = self.session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": f"*{title}*\n{message}",
                "parse_mode": "Markdown",
                "disable_notification": priority != "high",
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API: {data.get('description')}")

    # ── Discord ──────────────────────────────────────────────
    def _send_discord(self, title, message, priority, tags):
        url = os.environ["DISCORD_WEBHOOK_URL"]
        resp = self.session.post(url, json={"content": f"**{title}**\n{message}"},
                                 timeout=self.timeout)
        resp.raise_for_status()

    # ── Pushover ─────────────────────────────────────────────
    def _send_pushover(self, title, message, priority, tags):
        resp = self.session.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": os.environ["PUSHOVER_TOKEN"],
                "user": os.environ["PUSHOVER_USER"],
                "title": _ascii(title),
                "message": message,
                "priority": 1 if priority == "high" else 0,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
