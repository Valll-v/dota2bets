"""Живые лиговые матчи. Источники по порядку: Steam Web API (если задан STEAM_API_KEY), затем OpenDota /live.

OpenDota отстаёт и иногда показывает уже завершённые карты. Steam отдаёт матчи напрямую, с пиками, банами,
счётом серии и преимуществом по золоту. Ключ Steam бесплатный: https://steamcommunity.com/dev/apikey
Формат ответа Steam записан по памяти и на живом ключе не проверялся — разбор сделан терпимым к пропускам.
"""

import json
import os
import time
from urllib.request import Request, urlopen

STEAM_URL = "https://api.steampowered.com/IDOTA2Match_570/GetLiveLeagueGames/v1/"


def steam_available():
    return bool(os.environ.get("STEAM_API_KEY", "").strip())


def from_steam(timeout=20):
    key = os.environ.get("STEAM_API_KEY", "").strip()
    resp = urlopen(Request(STEAM_URL + "?key=" + key, headers={"User-Agent": "dota2bets/0.1"}), timeout=timeout)
    data = json.loads(resp.read().decode("utf-8"))
    return [g for g in (parse_steam_game(x) for x in (data.get("result") or {}).get("games") or []) if g]


def parse_steam_game(g):
    rt, dt = g.get("radiant_team") or {}, g.get("dire_team") or {}
    if not (rt.get("team_id") and dt.get("team_id")):
        return None
    sb = g.get("scoreboard") or {}
    rad, dire = sb.get("radiant") or {}, sb.get("dire") or {}

    def side(block, team_flag):
        by_acc = {p.get("account_id"): p for p in block.get("players") or []}
        out = []
        for p in g.get("players") or []:
            if p.get("team") != team_flag:
                continue
            acc = p.get("account_id")
            hero = p.get("hero_id") or (by_acc.get(acc) or {}).get("hero_id") or 0
            out.append((acc, hero))
        if not any(h for _, h in out) and block.get("picks"):
            picks = [x.get("hero_id") for x in block["picks"]]
            out = [(acc, picks[i] if i < len(picks) else 0) for i, (acc, _) in enumerate(out)]
        return out

    def worth(block):
        return sum(p.get("net_worth") or 0 for p in block.get("players") or [])

    dur = int(sb.get("duration") or 0)
    return {
        "source": "steam", "match_id": g.get("match_id"), "league_id": g.get("league_id"), "series_id": g.get("series_id"),
        "team_id_radiant": rt.get("team_id"), "team_id_dire": dt.get("team_id"),
        "team_name_radiant": rt.get("team_name"), "team_name_dire": dt.get("team_name"),
        "game_time": dur, "radiant_score": rad.get("score") or 0, "dire_score": dire.get("score") or 0,
        "spectators": g.get("spectators") or 0,
        "series_type": g.get("series_type"), "radiant_series_wins": g.get("radiant_series_wins") or 0,
        "dire_series_wins": g.get("dire_series_wins") or 0,
        "gold_lead": (worth(rad) - worth(dire)) if rad.get("players") and dire.get("players") else None,
        "bans_radiant": [x.get("hero_id") for x in rad.get("bans") or []], "bans_dire": [x.get("hero_id") for x in dire.get("bans") or []],
        "radiant": side(rad, 0), "dire": side(dire, 1),
        "drafting": dur <= 0,
    }


def from_opendota():
    from .cli_data import live_games
    out = []
    for g in live_games():
        g = dict(g, source="opendota", gold_lead=g.get("radiant_lead"), series_type=None, radiant_series_wins=None, dire_series_wins=None,
                 bans_radiant=[], bans_dire=[], drafting=not (g.get("game_time") or 0) > 0)
        out.append(g)
    return out


def drop_finished(conn, games):
    """Убрать карты, которые уже лежат в базе как завершённые (OpenDota /live отстаёт)."""
    out = []
    for g in games:
        if g.get("match_id") and conn.execute("select 1 from matches where match_id = ? and radiant_win is not null",
                                              (g["match_id"],)).fetchone():
            continue
        out.append(g)
    return out


def games(conn=None):
    """Живые лиговые игры и источник. Ошибка Steam не мешает: откат на OpenDota."""
    err = None
    if steam_available():
        try:
            res = from_steam()
            return {"source": "steam", "games": drop_finished(conn, res) if conn else res, "error": None, "ts": int(time.time())}
        except Exception as e:
            err = "Steam недоступен: %r" % e
    res = from_opendota()
    return {"source": "opendota", "games": drop_finished(conn, res) if conn else res, "error": err, "ts": int(time.time())}
