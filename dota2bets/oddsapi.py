"""Клиент OddsPapi (https://api.oddspapi.io/v4): расписание и кэфы по Dota 2, включая историю движения линии.

Ключ — в переменной окружения ODDSPAPI_KEY (получить на сайте сервиса; в репозиторий не попадает).
Эндпоинты и формат ответа взяты из документации сервиса от 2026-09; идентификаторы рынков не зашиты, а
ищутся по названию через /markets.
"""

import json
import os
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import DATA
from .opendota import atomic_write, read_bytes

BASE = "https://api.oddspapi.io/v4"
SPORT_DOTA = 16
CACHE = os.path.join(DATA, "cache", "odds")
PAUSE = 0.3
DEFAULT_BOOKS = ("pinnacle", "bet365", "1xbet")
_last = [0.0]


class OddsError(Exception):
    pass


def key():
    k = os.environ.get("ODDSPAPI_KEY", "").strip()
    if not k:
        raise OddsError("нет ключа: задайте переменную окружения ODDSPAPI_KEY (ключ выдаётся на oddspapi.io)")
    return k


def available():
    return bool(os.environ.get("ODDSPAPI_KEY", "").strip())


def get(path, params=None, ttl=0):
    """GET с дисковым кешем (ttl секунд) и паузой между запросами."""
    params = dict(params or {})
    cache_key = re.sub(r"[^A-Za-z0-9_=-]", "_", path + "_" + urlencode(sorted(params.items())))[:180]
    fname = os.path.join(CACHE, cache_key + ".json")
    if ttl and os.path.exists(fname) and time.time() - os.path.getmtime(fname) < ttl:
        return json.loads(read_bytes(fname).decode("utf-8"))
    params["apiKey"] = key()
    wait = PAUSE - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    try:
        resp = urlopen(Request(BASE + path + "?" + urlencode(params), headers={"User-Agent": "dota2bets/0.1"}), timeout=60)
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise OddsError("OddsPapi %d на %s: %s" % (e.code, path, body))
    data = resp.read()
    obj = json.loads(data.decode("utf-8"))
    if ttl:
        atomic_write(fname, data)
    return obj


def fixtures(date_from, date_to, has_odds=True):
    """Матчи Dota 2 за период (строки YYYY-MM-DD, не больше 10 дней)."""
    p = {"sportId": SPORT_DOTA, "from": date_from, "to": date_to}
    if has_odds:
        p["hasOdds"] = "true"
    return get("/fixtures", p, ttl=600) or []


def markets():
    return get("/markets", {"sportId": SPORT_DOTA}, ttl=7 * 86400) or []


def odds(fixture_id):
    return get("/odds", {"fixtureId": fixture_id}, ttl=120)


def historical(fixture_id, books=DEFAULT_BOOKS):
    return get("/historical-odds", {"fixtureId": fixture_id, "bookmakers": ",".join(books[:3])}, ttl=30 * 86400)


# ---------------------------------------------------------------- разбор ответов

WINNER_NAMES = ("match winner", "winner", "moneyline", "match result", "to win match", "1x2", "full time result")


def winner_market(catalog):
    """(marketId, outcome команды 1, outcome команды 2) для рынка «победитель матча». None, если не найден."""
    best = None
    for m in catalog or []:
        name = str(m.get("marketName") or "").strip().lower()
        outs = m.get("outcomes") or []
        if len(outs) != 2:
            continue
        rank = next((i for i, w in enumerate(WINNER_NAMES) if name == w), None)
        if rank is None:
            rank = next((10 + i for i, w in enumerate(WINNER_NAMES) if w in name and "map" not in name), None)
        if rank is None:
            continue
        if best is None or rank < best[0]:
            best = (rank, str(m["marketId"]), str(outs[0]["outcomeId"]), str(outs[1]["outcomeId"]))
    return best[1:] if best else None


def parse_ts(s):
    """'2026-04-05T14:58:17Z' → unix."""
    if not s:
        return None
    try:
        return int(time.mktime(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone)
    except ValueError:
        return None


def price_series(node):
    """Узел исхода → список (ts, price). Поддерживает и историю (список снимков), и текущую линию (один объект)."""
    players = (node or {}).get("players") or {}
    snap = players.get("0") if isinstance(players, dict) else None
    if snap is None:
        return []
    if isinstance(snap, dict):
        snap = [snap]
    out = []
    for s in snap:
        price = s.get("price")
        if price and float(price) > 1.0:
            out.append((parse_ts(s.get("createdAt")) or 0, float(price)))
    return sorted(out)


def winner_lines(payload, market):
    """Ответ /odds или /historical-odds → {букмекер: {'a': [(ts, price)], 'b': [...]}}."""
    if not market:
        return {}
    mid, o1, o2 = market
    books = payload.get("bookmakers") or payload.get("bookmakerOdds") or {}
    out = {}
    for slug, b in books.items():
        mk = ((b or {}).get("markets") or {}).get(mid)
        if not mk:
            continue
        outs = mk.get("outcomes") or {}
        a, c = price_series(outs.get(o1)), price_series(outs.get(o2))
        if a and c:
            out[slug] = {"a": a, "b": c}
    return out


def open_close(series, start_ts):
    """Открывающая и закрывающая цена: первая запись и последняя до начала матча."""
    if not series:
        return None, None
    before = [p for t, p in series if not start_ts or not t or t <= start_ts]
    return series[0][1], (before[-1] if before else series[0][1])
