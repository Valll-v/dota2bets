"""Расписание и лента прогнозов.

Расписание ведёт пользователь (OpenDota его не знает): строки «Команда A vs Команда B bo3 1.85 1.95» или форма.
Лента для каждой записи считает прогноз, счёт идущей серии по базе, вердикт и рекомендацию во флетах.
Серии, которые идут по базе, но не внесены в расписание, добавляются в ленту автоматически.
"""

import re
import time

from . import policy
from .cli_data import resolve_team
from .series import series_distribution

BO_MAPS = {0: 1, 1: 3, 2: 5}
RECENT = 14 * 3600          # окно, в котором карты считаются частью идущей серии


def add(conn, team_a, team_b, bo=3, tier="professional", starts=None, odds_a=None, odds_b=None, note=None):
    a, b = resolve_team(conn, team_a), resolve_team(conn, team_b)
    cur = conn.execute(
        "insert into schedule(created, team_a, team_b, name_a, name_b, bo, tier, starts, odds_a, odds_b, note, done) "
        "values (?,?,?,?,?,?,?,?,?,?,?,0)",
        (int(time.time()), a["team_id"], b["team_id"], a["name"], b["name"], bo, tier, starts, odds_a, odds_b, note))
    conn.commit()
    return cur.lastrowid


LINE_RE = re.compile(r"\s+(?:vs\.?|против|—|–|-)\s+", re.IGNORECASE)


def parse_line(line):
    """'Team Liquid vs Yakult Brothers bo3 1.25 3.80 premium' → dict или None."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = LINE_RE.split(line, maxsplit=1)
    if len(parts) != 2:
        raise ValueError("нет разделителя «vs»: %s" % line)
    a, rest = parts[0].strip(), parts[1].strip()
    bo, tier, odds = 3, "professional", []
    tokens = rest.split()
    while tokens:
        t = tokens[-1].lower().replace(",", ".")
        if re.fullmatch(r"bo[1235]", t):
            bo = int(t[2])
        elif t in ("premium", "lan"):
            tier = "premium"
        elif t in ("professional", "online"):
            tier = "professional"
        elif re.fullmatch(r"\d+\.\d+", t):
            odds.insert(0, float(t))
        else:
            break
        tokens.pop()
    b = " ".join(tokens).strip()
    if not a or not b:
        raise ValueError("не распознаны команды: %s" % line)
    return {"team_a": a, "team_b": b, "bo": bo, "tier": tier,
            "odds_a": odds[0] if len(odds) == 2 else None, "odds_b": odds[1] if len(odds) == 2 else None}


def add_text(conn, text):
    """Несколько строк расписания. Возвращает (добавлено, ошибки)."""
    added, errors = [], []
    for line in text.splitlines():
        try:
            d = parse_line(line)
            if d:
                added.append(add(conn, **d))
        except (ValueError, SystemExit) as e:
            errors.append("%s — %s" % (line.strip(), e))
    return added, errors


def update(conn, sid, **kw):
    fields = {k: v for k, v in kw.items() if k in ("bo", "tier", "starts", "odds_a", "odds_b", "note", "done", "ref_a", "ref_b",
                                                   "ref_book")}
    if fields:
        conn.execute("update schedule set %s where id=?" % ", ".join("%s=?" % k for k in fields),
                     list(fields.values()) + [sid])
        conn.commit()


def remove(conn, sid):
    conn.execute("delete from schedule where id=?", (sid,))
    conn.commit()


def series_state(conn, team_a, team_b, since_ts):
    """Счёт идущей (или только что сыгранной) серии пары по базе: dict(series_id, a, b, maps, last_ts) или None."""
    rows = conn.execute(
        "select match_id, series_id, series_type, radiant_team_id, radiant_win, start_time, duration from matches "
        "where radiant_win is not null and ((radiant_team_id=? and dire_team_id=?) or (radiant_team_id=? and dire_team_id=?)) "
        "and start_time >= ? order by start_time desc", (team_a, team_b, team_b, team_a, since_ts)).fetchall()
    if not rows:
        return None
    sid = rows[0]["series_id"]
    rows = [r for r in rows if r["series_id"] == sid] if sid else rows[:1]
    wa = sum(1 for r in rows if (r["radiant_team_id"] == team_a) == bool(r["radiant_win"]))
    return {"series_id": sid, "a": wa, "b": len(rows) - wa, "maps": len(rows),
            "bo": BO_MAPS.get(rows[0]["series_type"], None), "last_ts": rows[0]["start_time"] + (rows[0]["duration"] or 0)}


def decided(state, bo):
    if not state:
        return False
    if bo == 2:
        return state["maps"] >= 2
    need = bo // 2 + 1
    return state["a"] >= need or state["b"] >= need


def forecast_one(conn, pr, team_a, team_b, name_a, name_b, bo, tier, odds_a, odds_b, since_ts, s, ref_a=None,
                 ref_b=None, ref_book=None):
    state = series_state(conn, team_a, team_b, since_ts)
    if state and state["bo"] and state["bo"] != bo:
        bo = state["bo"]
    done = decided(state, bo)
    lead_a, lead_b = (state["a"], state["b"]) if state else (0, 0)
    res = pr.predict(team_a, team_b, tier=tier, bo=bo, series_id=state["series_id"] if state and not done else None)
    dist = series_distribution(res["p"], bo, 0 if done else lead_a, 0 if done else lead_b) if bo > 1 else None
    p_bet = dist["p_a"] if dist else res["p"]
    scores = [("%d-%d" % k, v) for k, v in dist["scores"].items()] if dist else []
    v = policy.verdict(res, name_a, name_b, p_bet, scores, bo, odds_a, odds_b, s, ref_a=ref_a, ref_b=ref_b,
                       ref_book=ref_book)
    from . import markets as mk
    mlist = mk.build(pr, res, name_a, name_b, bo, dist, s, None, None, 0 if done else lead_a, 0 if done else lead_b)
    from . import players, feed
    try:
        pl = players.matchup(pr, conn, res, team_a, team_b, name_a, name_b, int(time.time()))
    except Exception as e:      # разрез по игрокам не должен ронять прогноз
        pl = {"a": {"players": []}, "b": {"players": []}, "lines": [], "duel": None, "caveat": "разрез по игрокам недоступен: %r" % e}
    pn = pr.player_names
    logos = {r["team_id"]: r["logo_url"] for r in conn.execute(
        "select team_id, logo_url from teams where team_id in (?, ?)", (team_a, team_b))}
    out = {
        "team_a": team_a, "team_b": team_b, "name_a": name_a, "name_b": name_b, "bo": bo, "tier": tier,
        "logo_a": logos.get(team_a), "logo_b": logos.get(team_b),
        "odds_a": odds_a, "odds_b": odds_b, "p_map": res["p"], "p_bet": p_bet, "p_draw": dist["p_draw"] if dist else 0.0,
        "scores": [{"score": k, "p": p} for k, p in scores], "mode": res["mode"],
        "lead_a": lead_a, "lead_b": lead_b, "status": "сыграна" if done else ("идёт" if state else "ожидается"),
        "lineup_a": [pn.get(p, str(p)) for p in res["lineup_a"]], "lineup_b": [pn.get(p, str(p)) for p in res["lineup_b"]],
        "form_a": feed.team_form(conn, team_a, 10), "form_b": feed.team_form(conn, team_b, 10),
        "roster_elo_a": round(res["detail"]["a"]["roster_elo"]), "roster_elo_b": round(res["detail"]["b"]["roster_elo"]),
        "warnings": res.get("warnings", []),
        "top": sorted(((k, round(x, 2)) for k, x in res["contributions"].items() if k != "intercept"),
                      key=lambda kv: -abs(kv[1]))[:4],
        "verdict": v,
        "players": pl,
        "markets": [{"key": m["key"], "title": m["title"], "yes": m["yes"], "no": m["no"], "line": m["line"],
                     "p_yes": m["p_yes"], "note": m["note"], "min_odds_yes": m["verdict"]["min_odds_yes"],
                     "min_odds_no": m["verdict"]["min_odds_no"]} for m in mlist],
    }
    if done:
        out["verdict"] = dict(v, recommendation="Серия сыграна: %s %d:%d %s" % (name_a, lead_a, lead_b, name_b), bet=0, flats=0.0)
    return out


def forecasts(conn, pr, include_done=False):
    """Лента: записи расписания + идущие серии из базы, которых нет в расписании."""
    s = policy.settings(conn)
    now = int(time.time())
    out, seen = [], set()
    rows = conn.execute("select * from schedule where done=0 order by coalesce(starts, created), id").fetchall()
    for r in rows:
        try:
            f = forecast_one(conn, pr, r["team_a"], r["team_b"], r["name_a"], r["name_b"], r["bo"] or 3,
                             r["tier"] or "professional", r["odds_a"], r["odds_b"],
                             (r["starts"] or r["created"]) - 3 * 3600, s, r["ref_a"], r["ref_b"], r["ref_book"])
        except Exception as e:
            f = {"name_a": r["name_a"], "name_b": r["name_b"], "error": repr(e)}
        f.update({"id": r["id"], "starts": r["starts"], "note": r["note"], "source": "расписание",
                  "tournament": r["tournament"], "ref_a": r["ref_a"], "ref_b": r["ref_b"], "ref_book": r["ref_book"]})
        seen.add(frozenset((r["team_a"], r["team_b"])))
        if f.get("status") == "сыграна" and not include_done and now - (r["starts"] or r["created"]) > 24 * 3600:
            update(conn, r["id"], done=1)
            continue
        out.append(f)
    # идущие серии из базы
    recent = conn.execute(
        "select radiant_team_id a, dire_team_id b, max(start_time) ts, tier from matches where start_time >= ? "
        "and radiant_win is not null group by series_id order by ts desc", (now - RECENT,)).fetchall()
    names = {r["team_id"]: r["name"] for r in conn.execute("select team_id, name from teams")}
    for r in recent:
        key = frozenset((r["a"], r["b"]))
        if key in seen:
            continue
        seen.add(key)
        state = series_state(conn, r["a"], r["b"], now - RECENT)
        bo = (state or {}).get("bo") or 3
        if decided(state, bo) and not include_done:
            continue
        try:
            f = forecast_one(conn, pr, r["a"], r["b"], names.get(r["a"], str(r["a"])), names.get(r["b"], str(r["b"])),
                             bo, r["tier"] or "professional", None, None, now - RECENT, s)
        except Exception as e:
            f = {"name_a": names.get(r["a"]), "name_b": names.get(r["b"]), "error": repr(e)}
        f.update({"id": None, "starts": None, "note": None, "source": "база (идёт)"})
        out.append(f)
    return {"settings": s, "built": now, "items": out}
