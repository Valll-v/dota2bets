"""Журнал прогнозов и ставок: запись с решением политики, закрывающие кэфы, результаты, отчёт по сегментам.

По умолчанию всё — бумажная торговля (`paper=1`): деньги не ставятся, копятся линии и результаты. Отчёт показывает
точность модели, смешанной p, Claude и букмекера, а также ROI политики по сегментам — где ставить, а где нет.
"""

import json
import time

from . import policy
from .learn import metrics
from .odds import evaluate, implied


MARKET_NAMES = {"series": "серия", "map": "карта", "kills": "тотал килов", "dur": "длительность",
                "maps": "тотал карт", "hcp_a": "фора", "hcp_b": "фора"}


def record(conn, mode, team_a, team_b, name_a, name_b, p_model, bo=1, picks=None, odds_a=None, odds_b=None,
           side=None, stake=None, p_claude=None, series_id=None, match_id=None, note=None, dossier=None,
           analysis=None, tier=None, roster_change=False, confidence=None, paper=True, market="series",
           map_no=None, line=None, pick=None):
    """market: series | map | kills | dur | maps | hcp_a | hcp_b. Для рынков-тоталов p_model и odds_a — исход «да»
    (больше / фора прошла), odds_b — «нет»; pick — подписи исходов «да|нет»."""
    s = policy.settings(conn)
    d = policy.decide(p_model, odds_a, odds_b, bo, s, roster_stale=roster_change, confidence=confidence,
                      market="series" if market == "series" else "map")
    seg = policy.segments(mode, bo, tier, roster_change, d["edge"])
    seg["market"] = MARKET_NAMES.get(market, market)
    cur = conn.execute(
        """insert into predictions(created, mode, team_a, team_b, name_a, name_b, bo, picks, p_model, p_claude,
               odds_a, odds_b, side, stake, match_id, series_id, note, dossier, analysis, p_blend, segment, paper,
               policy_bet, policy_side, policy_stake, policy_reason, tier, confidence, market, map_no, line, pick)
           values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (int(time.time()), mode, team_a, team_b, name_a, name_b, bo, json.dumps(picks) if picks else None,
         p_model, p_claude, odds_a, odds_b, side, stake, match_id, series_id, note, dossier, analysis,
         d["p_blend"], json.dumps(seg, ensure_ascii=False), 1 if paper else 0, d["bet"], d["side"],
         round(d["stake_frac"] * s["bankroll"], 2) if d["bet"] else None, d["reason"], tier, confidence,
         market, map_no, line, pick))
    conn.commit()
    return cur.lastrowid


def set_claude(conn, pred_id, p_claude, analysis=None, confidence=None):
    conn.execute("update predictions set p_claude=?, analysis=coalesce(?, analysis), confidence=coalesce(?, confidence) where id=?",
                 (p_claude, analysis, confidence, pred_id))
    conn.commit()


def close(conn, pred_id, closing_a, closing_b):
    """Закрывающие кэфы: пересчёт смешанной p и решения политики по закрытию (как если бы ставили перед матчем)."""
    r = conn.execute("select * from predictions where id=?", (pred_id,)).fetchone()
    if not r:
        raise SystemExit("нет записи #%s" % pred_id)
    s = policy.settings(conn)
    seg = json.loads(r["segment"]) if r["segment"] else {}
    d = policy.decide(r["p_model"], closing_a, closing_b, r["bo"] or 1, s,
                      roster_stale=seg.get("roster") == "смена состава", confidence=r["confidence"],
                      market="series" if (r["market"] or "series") == "series" else "map")
    seg = policy.segments(r["mode"], r["bo"] or 1, r["tier"], seg.get("roster") == "смена состава", d["edge"])
    seg["market"] = MARKET_NAMES.get(r["market"] or "series", r["market"])
    conn.execute("update predictions set closing_a=?, closing_b=?, p_blend=?, segment=?, policy_bet=?, policy_side=?, "
                 "policy_stake=?, policy_reason=? where id=?",
                 (closing_a, closing_b, d["p_blend"], json.dumps(seg, ensure_ascii=False), d["bet"], d["side"],
                  round(d["stake_frac"] * s["bankroll"], 2) if d["bet"] else None, d["reason"], pred_id))
    conn.commit()
    return d


def import_closing(conn, path):
    """CSV `id,closing_a,closing_b` (заголовок необязателен)."""
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = [x.strip() for x in line.replace(";", ",").split(",")]
            if len(parts) < 3 or not parts[0].isdigit():
                continue
            close(conn, int(parts[0]), float(parts[1]), float(parts[2]))
            n += 1
    return n


def settle(conn, log=None):
    """Результат по базе: серия — по матчам пары после created (первая серия), карта — по match_id."""
    rows = conn.execute("select * from predictions where settled=0").fetchall()
    n = 0
    for r in rows:
        result = None
        if r["match_id"]:
            m = conn.execute("select radiant_team_id, radiant_win from matches where match_id=?", (r["match_id"],)).fetchone()
            if m and m["radiant_win"] is not None:
                result = 1 if (m["radiant_team_id"] == r["team_a"]) == bool(m["radiant_win"]) else 0
        elif r["market"] in ("kills", "dur"):
            m = conn.execute(
                "select radiant_score, dire_score, duration from matches where radiant_win is not null "
                "and ((radiant_team_id=? and dire_team_id=?) or (radiant_team_id=? and dire_team_id=?)) "
                "and start_time >= ? - 1200 order by start_time limit 1",
                (r["team_a"], r["team_b"], r["team_b"], r["team_a"], r["created"])).fetchone()
            if m and r["line"] is not None:
                value = ((m["radiant_score"] or 0) + (m["dire_score"] or 0)) if r["market"] == "kills" \
                    else (m["duration"] or 0) / 60.0
                result = 1 if value > r["line"] else 0
        elif r["market"] in ("maps", "hcp_a", "hcp_b"):
            ms = conn.execute(
                "select radiant_team_id, radiant_win, series_id from matches where radiant_win is not null "
                "and ((radiant_team_id=? and dire_team_id=?) or (radiant_team_id=? and dire_team_id=?)) "
                "and start_time >= ? - 6*3600 order by start_time",
                (r["team_a"], r["team_b"], r["team_b"], r["team_a"], r["created"])).fetchall()
            if ms:
                first = r["series_id"] or ms[0]["series_id"]
                ms = [m for m in ms if m["series_id"] == first] if first else ms[:1]
                wa = sum(1 for m in ms if (m["radiant_team_id"] == r["team_a"]) == bool(m["radiant_win"]))
                wb = len(ms) - wa
                need = (r["bo"] or 3) // 2 + 1
                if (wa >= need or wb >= need) and r["line"] is not None:
                    if r["market"] == "maps":
                        result = 1 if len(ms) > r["line"] else 0
                    elif r["market"] == "hcp_a":
                        result = 1 if wa - wb > r["line"] else 0
                    else:
                        result = 1 if wb - wa > r["line"] else 0
        elif r["market"] == "map":
            m = conn.execute(
                "select radiant_team_id, radiant_win from matches where radiant_win is not null "
                "and ((radiant_team_id=? and dire_team_id=?) or (radiant_team_id=? and dire_team_id=?)) "
                "and start_time >= ? - 1200 order by start_time limit 1",
                (r["team_a"], r["team_b"], r["team_b"], r["team_a"], r["created"])).fetchone()
            if m:
                result = 1 if (m["radiant_team_id"] == r["team_a"]) == bool(m["radiant_win"]) else 0
        else:
            ms = conn.execute(
                "select radiant_team_id, radiant_win, series_id, start_time from matches where radiant_win is not null "
                "and ((radiant_team_id=? and dire_team_id=?) or (radiant_team_id=? and dire_team_id=?)) "
                "and start_time >= ? - 6*3600 order by start_time", (r["team_a"], r["team_b"], r["team_b"], r["team_a"],
                                                                     r["created"])).fetchall()
            if r["series_id"]:
                ms = [m for m in ms if m["series_id"] == r["series_id"]]
            elif ms:
                first = ms[0]["series_id"]
                ms = [m for m in ms if m["series_id"] == first] if first else ms[:1]
            if ms:
                wa = sum(1 for m in ms if (m["radiant_team_id"] == r["team_a"]) == bool(m["radiant_win"]))
                wb = len(ms) - wa
                need = (r["bo"] or 1) // 2 + 1
                if wa >= need or wb >= need or (r["bo"] == 2 and len(ms) == 2):
                    result = 1 if wa > wb else (0 if wb > wa else None)
                    if r["bo"] == 2 and wa == wb:
                        result = 2  # ничья в bo2
        if result is not None:
            conn.execute("update predictions set result=?, settled=1 where id=?", (result, r["id"]))
            n += 1
            if log:
                log("#%d %s vs %s → %s" % (r["id"], r["name_a"], r["name_b"], result))
    conn.commit()
    return n


def _odds(r):
    """Кэфы для оценки: закрывающие, если есть, иначе открывающие."""
    if r["closing_a"] and r["closing_b"]:
        return r["closing_a"], r["closing_b"]
    if r["odds_a"] and r["odds_b"]:
        return r["odds_a"], r["odds_b"]
    return None, None


def _policy_pnl(rows):
    """Итог политики: число ставок, выигрыши, оборот, прибыль, ROI (в единицах ставки политики)."""
    staked = profit = 0.0
    bets = wins = 0
    for r in rows:
        if not r["policy_bet"] or not r["policy_stake"]:
            continue
        oa, ob = _odds(r)
        if not oa:
            continue
        odds = oa if r["policy_side"] == "a" else ob
        won = (r["result"] == 1) == (r["policy_side"] == "a")
        staked += r["policy_stake"]
        profit += r["policy_stake"] * (odds - 1) if won else -r["policy_stake"]
        bets += 1
        wins += 1 if won else 0
    return {"n": bets, "wins": wins, "staked": round(staked, 2), "profit": round(profit, 2),
            "roi": round(profit / staked, 4) if staked else None}


def report(conn):
    rows = conn.execute("select * from predictions where settled=1 and result in (0,1)").fetchall()
    s = policy.settings(conn)
    out = {"n": len(rows), "settings": s, "sources": {}, "segments": {}}
    for src in ("p_model", "p_blend", "p_claude", "book"):
        ps, ys = [], []
        for r in rows:
            if src == "book":
                oa, ob = _odds(r)
                if not oa:
                    continue
                p = implied(oa, ob)[0]
            else:
                p = r[src]
                if p is None:
                    continue
            ps.append(p)
            ys.append(r["result"])
        if ps:
            out["sources"][src] = metrics(ps, ys)
    out["policy"] = _policy_pnl(rows)
    # разрез политики по сегментам
    by = {}
    for r in rows:
        seg = json.loads(r["segment"]) if r["segment"] else {}
        for k, v in seg.items():
            by.setdefault(k, {}).setdefault(v, []).append(r)
    for k, groups in by.items():
        out["segments"][k] = {}
        for v, rs in groups.items():
            ps = [r["p_model"] for r in rs if r["p_model"] is not None]
            ys = [r["result"] for r in rs if r["p_model"] is not None]
            books = [(implied(*_odds(r))[0], r["result"]) for r in rs if _odds(r)[0]]
            out["segments"][k][v] = {
                "n": len(rs), "model": metrics(ps, ys) if ps else None,
                "book": metrics([b for b, _ in books], [y for _, y in books]) if books else None,
                "policy": _policy_pnl(rs),
            }
    # ставки реальными деньгами (paper=0, side/stake заданы)
    staked = profit = 0.0
    bets = wins = 0
    for r in rows:
        if r["paper"] or not r["stake"] or r["side"] not in ("a", "b"):
            continue
        oa, ob = _odds(r)
        if not oa:
            continue
        odds = oa if r["side"] == "a" else ob
        won = (r["result"] == 1) == (r["side"] == "a")
        staked += r["stake"]
        profit += r["stake"] * (odds - 1) if won else -r["stake"]
        bets += 1
        wins += 1 if won else 0
    out["money"] = {"n": bets, "wins": wins, "staked": staked, "profit": round(profit, 2),
                    "roi": round(profit / staked, 4) if staked else None}
    out["pending"] = conn.execute("select count(*) from predictions where settled=0").fetchone()[0]
    out["without_closing"] = conn.execute("select count(*) from predictions where closing_a is null").fetchone()[0]
    return out


def list_rows(conn, limit=50):
    return [dict(r) for r in conn.execute("select * from predictions order by id desc limit ?", (limit,))]
