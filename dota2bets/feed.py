"""Данные для экранов интерфейса: форма, личные встречи, проверки надёжности, история прогнозов,
последний разбор Claude и живые карты с вероятностью по ходу игры.

История прогнозов строится по `wf_pred`: это оценки walk-forward «как в тот день», модель в момент прогноза
матча не видела. Вероятность серии считается из оценки первой карты.
"""

import glob
import math
import os
import threading
import time

from . import DATA
from .series import series_distribution

ANALYSES_DIR = os.path.join(DATA, "analyses")
BO_MAPS = {0: 1, 1: 3, 2: 5, 3: 2}
DAY = 86400
TRACK_KEEP = 3 * DAY

_live_cache = {}                 # match_id -> {"key": герои, "pre": .., "post": .., "top": [...]}
_live_lock = threading.Lock()


def team_form(conn, team_id, n=10):
    """Исходы последних карт команды, свежие первыми: 1 — победа, 0 — поражение."""
    from .cli_data import team_matches
    return [1 if m["won"] else 0 for m in team_matches(conn, team_id, n)]


def h2h_rows(conn, team_a, team_b, limit=20):
    from .cli_data import h2h
    rows, wins_a = h2h(conn, team_a, team_b)
    return {"maps": len(rows), "wins_a": wins_a, "wins_b": len(rows) - wins_a,
            "rows": [{"ts": r["start_time"], "match_id": r["match_id"], "series_id": r["series_id"],
                      "a_radiant": r["radiant_team_id"] == team_a,
                      "won_a": (r["radiant_team_id"] == team_a) == bool(r["radiant_win"])} for r in rows[:limit]]}


def checks(conn, res):
    """Список проверок под «Уверенность данных»: что с составами, сыгранностью, простоем и свежестью базы."""
    from .db import kv_get
    stale = [w for w in res.get("warnings", []) if "играли за другие команды" in w]
    full = len(res["lineup_a"]) >= 5 and len(res["lineup_b"]) >= 5
    a, b = res["detail"]["a"], res["detail"]["b"]
    out = [{"ok": full and not stale,
            "text": "Составы обеих команд подтверждены последними матчами" if full and not stale else
            ("Состав устарел: часть игроков с тех пор играла за другие команды" if stale else "Неполный состав в базе")}]
    together = min(a["stability"], b["stability"])
    out.append({"ok": together >= 3, "text": "У пятёрок достаточно совместных карт" if together >= 3 else
                "Одна из пятёрок почти не играла вместе"})
    idle = max(math.expm1(a["rest"]), math.expm1(b["rest"]))
    out.append({"ok": idle <= 30, "text": "Обе команды играли в последние 30 дней" if idle <= 30 else
                "Одна из команд не играла %.0f дней" % idle})
    last = kv_get(conn, "last_sync")
    age = (time.time() - float(last)) / 3600.0 if last else None
    out.append({"ok": age is not None and age < 24,
                "text": "База обновлена %s" % time.strftime("%d.%m", time.localtime(float(last))) if age is not None and age < 24
                else ("База не обновлялась больше суток" if age is not None else "База ни разу не синхронизировалась")})
    return out


def history(conn, days=14, limit=400, now=None):
    """Сыгранные серии за последние дни с прогнозом «как в тот день» и отметкой, сбылся ли он."""
    now = int(now or time.time())
    rows = conn.execute(
        "select m.match_id, m.start_time, m.radiant_team_id, m.dire_team_id, m.radiant_win, m.series_id, m.series_type, "
        "m.tier, m.leagueid, w.p_pre from matches m left join wf_pred w on w.match_id = m.match_id "
        "where m.start_time >= ? and m.radiant_win is not null and m.radiant_team_id is not null "
        "and m.dire_team_id is not null order by m.start_time", (now - days * DAY,)).fetchall()
    groups, order = {}, []
    for r in rows:
        pair = frozenset((r["radiant_team_id"], r["dire_team_id"]))
        key = (r["series_id"], pair) if r["series_id"] else ("m", r["match_id"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)
    teams = {r["team_id"]: r for r in conn.execute("select team_id, name, logo_url from teams")}
    leagues = {r["leagueid"]: r["name"] for r in conn.execute("select leagueid, name from leagues")}

    def name(tid):
        return teams[tid]["name"] if tid in teams and teams[tid]["name"] else str(tid)

    out = []
    for key in order:
        maps = groups[key]
        first = maps[0]
        if first["p_pre"] is None:
            continue
        a, b = first["radiant_team_id"], first["dire_team_id"]
        wa = sum(1 for r in maps if (r["radiant_team_id"] == a) == bool(r["radiant_win"]))
        wb = len(maps) - wa
        bo = BO_MAPS.get(first["series_type"], 1) if first["series_id"] else 1
        if bo == 2:
            if len(maps) < 2:
                continue
        elif max(wa, wb) < bo // 2 + 1:
            continue                                   # серия не доиграна или карты ещё не в базе
        p_map = first["p_pre"]
        if bo > 1:
            d = series_distribution(p_map, bo)
            p_a, p_b = d["p_a"], d["p_b"]
        else:
            p_a, p_b = p_map, 1.0 - p_map
        pick_a = p_a >= p_b
        draw = wa == wb
        out.append({"ts": first["start_time"], "team_a": a, "team_b": b, "name_a": name(a), "name_b": name(b),
                    "logo_a": teams[a]["logo_url"] if a in teams else None,
                    "logo_b": teams[b]["logo_url"] if b in teams else None,
                    "bo": bo, "tier": first["tier"], "tournament": leagues.get(first["leagueid"]),
                    "p_a": p_a, "p_b": p_b, "p_map": p_map, "pick": name(a) if pick_a else name(b),
                    "p_pick": p_a if pick_a else p_b, "score_a": wa, "score_b": wb,
                    "ok": None if draw else (wa > wb) == pick_a})
    out.reverse()
    judged = [x for x in out if x["ok"] is not None]
    return {"items": out[:limit], "n": len(judged), "hits": sum(1 for x in judged if x["ok"]), "days": days}


def analysis_latest(name_a, name_b):
    """Последний разбор Claude для пары из data/analyses: текст, время, вердикт. None, если разборов не было."""
    from .dossier import slug
    from .ai import parse_verdict
    sa, sb = slug(name_a), slug(name_b)
    files = glob.glob(os.path.join(ANALYSES_DIR, "%s--vs--%s--*.md" % (sa, sb)))
    swapped = glob.glob(os.path.join(ANALYSES_DIR, "%s--vs--%s--*.md" % (sb, sa)))
    best = max(files + swapped, key=os.path.getmtime, default=None)
    if not best:
        return None
    with open(best, encoding="utf-8") as f:
        text = f.read()
    return {"key": os.path.splitext(os.path.basename(best))[0], "ts": int(os.path.getmtime(best)), "text": text,
            "verdict": parse_verdict(text), "swapped": best in swapped}


# ---------------------------------------------------------------- live

def logit(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def series_chance(p_now, p_later, bo, lead_a, lead_b):
    """Шанс A взять серию: текущая карта с вероятностью p_now, следующие — с p_later."""
    if bo <= 1:
        return p_now
    need = bo // 2 + 1

    def after(la, lb):
        if bo != 2 and la >= need:
            return 1.0
        if bo != 2 and lb >= need:
            return 0.0
        if la + lb >= bo:
            return 1.0 if la > lb else 0.0
        return series_distribution(p_later, bo, la, lb)["p_a"]
    return p_now * after(lead_a + 1, lead_b) + (1 - p_now) * after(lead_a, lead_b + 1)


def _pregame(pr, g):
    """Оценка карты до начала: без драфта и с драфтом. Кеш по match_id и набору героев."""
    rad = [(a, h) for a, h in g["radiant"] if h]
    dire = [(a, h) for a, h in g["dire"] if h]
    full = len(rad) == 5 and len(dire) == 5
    key = (tuple(h for _, h in rad), tuple(h for _, h in dire)) if full else None
    with _live_lock:
        c = _live_cache.get(g["match_id"])
    if c and c["key"] == key:
        return c
    a, b = g["team_id_radiant"], g["team_id_dire"]
    la = [x for x, _ in g["radiant"] if x]
    lb = [x for x, _ in g["dire"] if x]
    la, lb = (la if len(la) == 5 else None), (lb if len(lb) == 5 else None)
    pre = pr.predict(a, b, la, lb, side="radiant")
    c = {"key": key, "pre": pre["p"], "post": None, "contrib": pre["contributions"], "warnings": pre.get("warnings", [])}
    if full:
        known = all(x for x, _ in rad) and all(x for x, _ in dire)
        post = pr.predict(a, b, la, lb, [h for _, h in rad], [h for _, h in dire], side="radiant",
                          assign_a=rad if known else None, assign_b=dire if known else None)
        if post["mode"] == "post":
            c.update({"post": post["p"], "contrib": post["contributions"],
                      "draft": (post.get("draft_only") or {}).get("p")})
    with _live_lock:
        _live_cache[g["match_id"]] = c
        for k in list(_live_cache)[:-40]:
            _live_cache.pop(k, None)
    return c


def live_view(conn, pr, games):
    """Живые карты с вероятностью Radiant: до драфта, после драфта, сейчас. Снимки копятся в live_track."""
    from . import ingame
    bundle = ingame.load(conn)
    now = int(time.time())
    names = pr.player_names
    out = []
    for g in games:
        g = dict(g)
        a, b = g.get("team_id_radiant"), g.get("team_id_dire")
        g["players_radiant"] = [{"id": x, "name": names.get(x, str(x)) if x else None, "hero": h} for x, h in g["radiant"]]
        g["players_dire"] = [{"id": x, "name": names.get(x, str(x)) if x else None, "hero": h} for x, h in g["dire"]]
        g.update({"p_pre": None, "p_post": None, "p_now": None, "p_series": None, "track": [], "factors": []})
        if not (a and b and g.get("match_id")):
            out.append(g)
            continue
        try:
            c = _pregame(pr, g)
        except (Exception, SystemExit) as e:
            g["error"] = "прогноз недоступен: %s" % e
            out.append(g)
            continue
        base = c["post"] if c["post"] is not None else c["pre"]
        minute = int((g.get("game_time") or 0) // 60)
        p_now, gold_shift = base, None
        if bundle and g.get("gold_lead") is not None and minute > 1:
            p_now = ingame.predict(bundle, base, minute, float(g["gold_lead"]))
            gold_shift = logit(p_now) - logit(base)
        bo = BO_MAPS.get(g.get("series_type"), None) if g.get("series_type") is not None else None
        if g.get("radiant_series_wins") is None:       # OpenDota счёт серии не отдаёт: берём из базы
            from .schedule import series_state
            st = series_state(conn, a, b, now - 14 * 3600)
            if st and st["bo"] and st["bo"] > 1:
                bo = st["bo"]
                g["radiant_series_wins"], g["dire_series_wins"] = st["a"], st["b"]
        if bo and g.get("radiant_series_wins") is not None:
            g["bo"] = bo
            g["p_series"] = series_chance(p_now, c["pre"], bo, int(g["radiant_series_wins"] or 0),
                                          int(g["dire_series_wins"] or 0))
        top = sorted(((k, v) for k, v in c["contrib"].items() if k != "intercept"), key=lambda kv: -abs(kv[1]))[:5]
        factors = [{"key": k, "v": round(v, 3)} for k, v in top]
        factors.append({"key": "radiant_side", "v": round(c["contrib"].get("intercept", 0.0), 3)})
        if gold_shift is not None:
            factors.append({"key": "gold_now", "v": round(gold_shift, 3)})
        factors.sort(key=lambda f: -abs(f["v"]))
        g.update({"p_pre": c["pre"], "p_post": c["post"], "p_draft": c.get("draft"), "p_now": p_now, "minute": minute, "factors": factors,
                  "warnings": c["warnings"]})
        if not g.get("drafting"):
            last = conn.execute("select game_time from live_track where match_id = ? order by ts desc limit 1",
                                (g["match_id"],)).fetchone()
            if not last or (g.get("game_time") or 0) - (last["game_time"] or 0) >= 20:
                conn.execute("insert into live_track(match_id, ts, game_time, gold_lead, p) values (?,?,?,?,?)",
                             (g["match_id"], now, int(g.get("game_time") or 0), g.get("gold_lead"), p_now))
        g["track"] = [{"t": r["game_time"], "p": r["p"], "gold": r["gold_lead"]} for r in conn.execute(
            "select game_time, p, gold_lead from live_track where match_id = ? order by game_time", (g["match_id"],))]
        out.append(g)
    conn.execute("delete from live_track where ts < ?", (now - TRACK_KEEP,))
    conn.commit()
    return out
