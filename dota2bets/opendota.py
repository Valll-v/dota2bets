"""Клиент OpenDota: бюджет запросов, дисковый кеш, REST и SQL-explorer.

Все обращения к OpenDota в проекте идут только через этот модуль. Бюджет — скользящее окно на минуту плюс
суточный счётчик, который переживает перезапуск (data/cache/budget.json). Паттерн взят из «Угадайки».
"""

import json
import os
import re
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from . import DATA

OPENDOTA = "https://api.opendota.com/api/"
API_KEY = os.environ.get("OPENDOTA_API_KEY", "")
CACHE = os.environ.get("CACHE_DIR") or os.path.join(DATA, "cache")
USER_AGENT = "dota2bets/0.1"


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


PER_MIN = env_int("OPENDOTA_PER_MIN", 50)     # бесплатный лимит: 60 в минуту
PER_DAY = env_int("OPENDOTA_PER_DAY", 2500)   # и около 3000 в сутки

HOUR = 3600
DAY = 24 * HOUR


def log(msg):
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))


# ---------------------------------------------------------------- файлы

def atomic_write(path, data):
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.current_thread().ident or 0)
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def load_json(data):
    return json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)


# ---------------------------------------------------------------- бюджет

class BudgetError(Exception):
    pass


class Budget:
    """Скользящее окно на минуту плюс суточный счётчик (переживает перезапуск)."""

    def __init__(self, per_min, per_day, path):
        self.per_min = per_min
        self.per_day = per_day
        self.path = path
        self.lock = threading.Lock()
        self.window = []
        self.day = ""
        self.used = 0
        self.blocked_until = 0.0
        try:
            saved = load_json(read_bytes(path))
            self.day, self.used = saved.get("day", ""), int(saved.get("used", 0))
        except Exception:
            pass

    @staticmethod
    def _today():
        return time.strftime("%Y-%m-%d", time.gmtime())

    def take(self, wait=8.0):
        deadline = time.time() + wait
        while True:
            with self.lock:
                now = time.time()
                if self.day != self._today():
                    self.day, self.used = self._today(), 0
                self.window = [t for t in self.window if now - t < 60]
                if now < self.blocked_until:
                    raise BudgetError("OpenDota просит подождать (429)")
                if self.used >= self.per_day:
                    raise BudgetError("исчерпан суточный бюджет запросов к OpenDota")
                if len(self.window) < self.per_min:
                    self.window.append(now)
                    self.used += 1
                    try:
                        atomic_write(self.path, json.dumps({"day": self.day, "used": self.used}).encode())
                    except Exception:
                        pass
                    return
            if time.time() >= deadline:
                raise BudgetError("слишком много запросов в минуту, попробуйте позже")
            time.sleep(0.25)

    def block(self, seconds):
        self.blocked_until = max(self.blocked_until, time.time() + seconds)

    def status(self):
        with self.lock:
            now = time.time()
            return {"day": self.day, "used": self.used, "per_day": self.per_day,
                    "last_minute": len([t for t in self.window if now - t < 60]), "per_min": self.per_min}


BUDGET = Budget(PER_MIN, PER_DAY, os.path.join(CACHE, "budget.json"))


# ---------------------------------------------------------------- запросы

def fetch_upstream(path, wait=8.0, timeout=90):
    """GET к OpenDota в пределах бюджета. Возвращает байты проверенного JSON."""
    BUDGET.take(wait=wait)
    url = OPENDOTA + path
    if API_KEY:
        url += ("&" if "?" in url else "?") + "api_key=" + API_KEY
    try:
        resp = urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)
    except HTTPError as e:
        if e.code == 429:
            BUDGET.block(60)
            raise BudgetError("OpenDota: слишком много запросов")
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            body = ""
        raise RuntimeError("OpenDota %d на %s: %s" % (e.code, path[:120], body))
    data = resp.read()
    load_json(data)  # мусор не кешируем
    return data


LOCKS = [threading.Lock() for _ in range(64)]


def cache_file(key):
    return os.path.join(CACHE, re.sub(r"[^A-Za-z0-9_=-]", "_", key)[:180] + ".json")


def cached_fetch(path, ttl):
    """Ответ с диска, если моложе ttl; иначе запрос, при ошибке — устаревший кеш."""
    fname = cache_file(path)
    with LOCKS[hash(path) % len(LOCKS)]:
        if os.path.exists(fname) and time.time() - os.path.getmtime(fname) < ttl:
            return read_bytes(fname)
        try:
            data = fetch_upstream(path)
        except Exception as e:
            if os.path.exists(fname):
                log("stale cache for %s (%s)" % (path, e))
                return read_bytes(fname)
            raise
        atomic_write(fname, data)
        return data


def get(path, ttl=0):
    """JSON-ответ REST-эндпоинта; ttl > 0 включает дисковый кеш."""
    if ttl > 0:
        return load_json(cached_fetch(path, ttl))
    return load_json(fetch_upstream(path))


def explorer(sql, timeout=180):
    """SQL-explorer OpenDota: список строк (dict). Один запрос = одна единица бюджета."""
    data = load_json(fetch_upstream("explorer?sql=" + quote(sql), timeout=timeout))
    if data.get("err"):
        raise RuntimeError("explorer: %s" % data["err"])
    return data.get("rows") or []


def reachable():
    try:
        urlopen(Request(OPENDOTA + "health", headers={"User-Agent": USER_AGENT}), timeout=10)
        return True
    except (HTTPError, URLError, OSError):
        return False
