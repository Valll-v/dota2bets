"""Боковые панели на экране разбора матча: фоновое видео слева и справа, место под рекламу.

Настройки — в `notes/rails.json` (необязательный файл, перекрывает умолчания целиком по ключам left/right/enabled).
Типы элементов:
  {"type": "youtube", "id": "<id ролика>", "title": "...", "aspect": "16:9" | "9:16"}
  {"type": "video", "src": "soap.mp4" | "https://...", "title": "..."}       файл из data/media/ или ссылка
  {"type": "ad", "image": "banner.png" | "https://...", "link": "https://...", "title": "...", "text": "...",
   "label": "Реклама"}
Ролики YouTube встраиваются штатным плеером, файлы не скачиваются.
"""

import json
import os

from . import DATA, ROOT

CONFIG_PATH = os.path.join(ROOT, "notes", "rails.json")
MEDIA_DIR = os.path.join(DATA, "media")
MEDIA_MIME = {".mp4": "video/mp4", ".webm": "video/webm", ".png": "image/png", ".jpg": "image/jpeg",
              ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml"}

DEFAULTS = {
    "enabled": True,
    "left": {"items": [
        {"type": "youtube", "id": "J9dvPQuHz-I", "title": "Нарезка мыла", "aspect": "16:9"},
        {"type": "youtube", "id": "nbXz5p1eQyo", "title": "Конфеты", "aspect": "16:9"},
        {"type": "youtube", "id": "q9BtYEnrkg4", "title": "Пресс", "aspect": "16:9"},
    ]},
    "right": {"items": [
        {"type": "youtube", "id": "plUsPpUjOv0", "title": "Subway Surfers", "aspect": "9:16"},
        {"type": "youtube", "id": "XYC1Qtu1kZo", "title": "Subway Surfers 4K", "aspect": "9:16"},
    ]},
}


def media_url(src):
    if not src or "://" in src or src.startswith("/"):
        return src
    return "/media/" + src


def load():
    cfg = dict(DEFAULTS)
    error = None
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
        for k in ("enabled", "left", "right"):
            if k in user:
                cfg[k] = user[k]
    except OSError:
        pass
    except ValueError as e:
        error = "notes/rails.json не читается: %s" % e
    out = {"enabled": bool(cfg.get("enabled", True)), "error": error}
    for side in ("left", "right"):
        items = []
        for it in (cfg.get(side) or {}).get("items") or []:
            it = dict(it)
            if it.get("type") not in ("youtube", "video", "ad"):
                continue
            for k in ("src", "image"):
                if it.get(k):
                    it[k] = media_url(it[k])
            items.append(it)
        out[side] = items
    return out


def media_path(rel):
    """Путь к файлу в data/media или None, если имя выходит за папку или тип не разрешён."""
    path = os.path.normpath(os.path.join(MEDIA_DIR, rel))
    if not path.startswith(MEDIA_DIR + os.sep) or os.path.splitext(path)[1].lower() not in MEDIA_MIME:
        return None
    return path if os.path.isfile(path) else None
