"""Уведомления в Telegram о сериях, по которым политика предлагает ставку.

Нужны TELEGRAM_BOT_TOKEN (бот создаётся у @BotFather) и TELEGRAM_CHAT_ID (ваш чат с ботом).
Без них модуль только печатает текст. Каждая рекомендация отправляется один раз: ключи отправленных хранятся в базе.
"""

import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .db import kv_get, kv_set


def configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and os.environ.get("TELEGRAM_CHAT_ID", "").strip())


def send(text):
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"].strip(), os.environ["TELEGRAM_CHAT_ID"].strip()
    data = urlencode({"chat_id": chat, "text": text[:3900], "disable_web_page_preview": "true"}).encode("utf-8")
    resp = urlopen(Request("https://api.telegram.org/bot%s/sendMessage" % token, data=data), timeout=20)
    return json.loads(resp.read().decode("utf-8")).get("ok", False)


def message(f):
    v = f["verdict"]
    lines = ["%s — %s, bo%d" % (f["name_a"], f["name_b"], f["bo"]),
             "%s %.0f%% / %.0f%%" % ("серия" if f["bo"] > 1 else "карта", 100 * f["p_bet"], 100 * (1 - f["p_bet"])),
             v["recommendation"]]
    if v.get("fair"):
        lines.append("эталон %s: %.2f / %.2f" % (v["fair"]["book"], v["fair"]["odds_a"], v["fair"]["odds_b"]))
    if v.get("reliability") != "высокая":
        lines.append("надёжность данных: %s" % v["reliability"])
    for ln in ((f.get("players") or {}).get("lines") or [])[:2]:
        lines.append("• " + ln["text"])
    return "\n".join(lines)


def notify_forecasts(conn, items, dry=False, log=print):
    """Отправить новые рекомендации «СТАВКА». Возвращает число отправленных."""
    sent = set(kv_get(conn, "notified", []) or [])
    n = 0
    for f in items:
        v = f.get("verdict") or {}
        if f.get("error") or not v.get("bet"):
            continue
        key = "%s:%s:%s:%s:%s" % (f.get("team_a"), f.get("team_b"), f.get("starts") or f.get("id"), v.get("side"), v.get("flats"))
        if key in sent:
            continue
        text = message(f)
        if dry or not configured():
            log("[не отправлено: %s]\n%s\n" % ("пробный прогон" if dry else "Telegram не настроен", text))
        else:
            if not send(text):
                log("Telegram отклонил сообщение")
                continue
        sent.add(key)
        n += 1
    if not dry and configured():
        kv_set(conn, "notified", sorted(sent)[-500:])
        conn.commit()
    return n
