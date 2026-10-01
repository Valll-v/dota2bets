"""Кэфы: загрузка расписания и линий, сопоставление с базой матчей, проверка модели и политики на истории.

Источники: OddsPapi (эталонная линия, по умолчанию Pinnacle) и CSV из любого другого места.
Ставите вы у своего букмекера — его цену вводите в карточке матча; эталон нужен, чтобы знать справедливую цену и
чтобы измерить, обыгрывает ли модель рынок.
"""

import difflib
import json
import os
import re
import time

from . import ROOT, oddsapi, policy
from .learn import metrics
from .odds import implied
from .series import series_distribution

DAY = 86400
BO = {0: 1, 1: 3, 2: 5}
STOP = ("team", "esports", "esport", "gaming", "club", "gg", "dota2", "dota")


def norm(name):
    s = re.sub(r"[^a-z0-9а-я ]+", " ", str(name or "").lower())
    words = [w for w in s.split() if w not in STOP]
    return "".join(words) or re.sub(r"\s+", "", s)


ALIASES_PATH = os.path.join(ROOT, "notes", "aliases.csv")


def load_aliases():
    """notes/aliases.csv: строки «название во внешнем источнике,team_id». Для команд, которые не находятся сами."""
    out = {}
    try:
        with open(ALIASES_PATH, encoding="utf-8-sig") as f:
            for line in f:
                parts = [x.strip() for x in line.split(",")]
                if len(parts) >= 2 and parts[1].isdigit():
                    out[norm(parts[0])] = int(parts[1])
    except OSError:
        pass
    return out


class TeamIndex:
    """Сопоставление названий команд из внешних источников с team_id базы (только команды, игравшие за год)."""

    def __init__(self, conn, now=None):
        now = now or int(time.time())
        rows = conn.execute(
            "select t.team_id, t.name, t.tag, (select max(m.start_time) from matches m where m.radiant_team_id = t.team_id "
            "or m.dire_team_id = t.team_id) last from teams t").fetchall()
        self.items = [(r["team_id"], r["name"] or "", r["tag"] or "", r["last"] or 0) for r in rows if (r["last"] or 0) > now - 500 * DAY]
        self.by_norm = {}
        for tid, name, tag, last in sorted(self.items, key=lambda x: x[3]):
            for key in (norm(name), norm(tag)):
                if key:
                    self.by_norm[key] = tid          # при совпадении побеждает игравшая позже
        self.cache = {}
        self.alias = load_aliases()

    def find(self, name):
        key = norm(name)
        if key in self.cache:
            return self.cache[key]
        if key in self.alias:
            self.cache[key] = self.alias[key]
            return self.alias[key]
        tid = self.by_norm.get(key)
        if tid is None and key:
            cands = [k for k in self.by_norm if len(k) >= 4 and len(key) >= 4 and (k in key or key in k)
                     and min(len(k), len(key)) >= 0.6 * max(len(k), len(key))]
            if cands:
                tid = self.by_norm[max(cands, key=len)]
            else:
                close = difflib.get_close_matches(key, list(self.by_norm), n=1, cutoff=0.82)
                tid = self.by_norm[close[0]] if close else None
        self.cache[key] = tid
        return tid


def find_series(conn, team_a, team_b, start_ts):
    """Серия пары, начавшаяся ближе всего ко времени начала из линии (в пределах суток).
    Возвращает счёт по всем картам серии: series_id, bo, карты A и B, первая карта, сторона A в ней."""
    rows = conn.execute(
        "select match_id, series_id, series_type, radiant_team_id, radiant_win, start_time, tier from matches "
        "where radiant_win is not null and ((radiant_team_id = ? and dire_team_id = ?) or (radiant_team_id = ? and dire_team_id = ?)) "
        "and start_time between ? and ? order by start_time", (team_a, team_b, team_b, team_a, start_ts - 12 * 3600, start_ts + 18 * 3600)).fetchall()
    if not rows:
        return None
    groups = {}
    for r in rows:
        groups.setdefault(r["series_id"] or -r["match_id"], []).append(r)
    key = min(groups, key=lambda k: abs(groups[k][0]["start_time"] - start_ts))
    rows = groups[key]
    if key > 0:                       # все карты серии, даже если часть выпала из окна
        rows = conn.execute(
            "select match_id, series_id, series_type, radiant_team_id, radiant_win, start_time, tier from matches "
            "where series_id = ? and radiant_win is not null order by start_time", (key,)).fetchall()
    wa = sum(1 for r in rows if (r["radiant_team_id"] == team_a) == bool(r["radiant_win"]))
    return {"series_id": rows[0]["series_id"], "bo": BO.get(rows[0]["series_type"], 1), "a": wa, "b": len(rows) - wa,
            "first": rows[0]["match_id"], "a_radiant": rows[0]["radiant_team_id"] == team_a, "tier": rows[0]["tier"],
            "ts": rows[0]["start_time"]}


def store_fixture(conn, fixture_id, source, start_ts, name_a, name_b, tournament, idx):
    ta, tb = idx.find(name_a), idx.find(name_b)
    conn.execute(
        "insert into odds_fixtures(fixture_id, source, start_ts, name_a, name_b, tournament, team_a, team_b, fetched) "
        "values (?,?,?,?,?,?,?,?,?) on conflict(fixture_id) do update set start_ts = excluded.start_ts, team_a = excluded.team_a, "
        "team_b = excluded.team_b, tournament = excluded.tournament, fetched = excluded.fetched",
        (fixture_id, source, start_ts, name_a, name_b, tournament, ta, tb, int(time.time())))
    return ta, tb


def store_line(conn, fixture_id, book, open_a, open_b, close_a, close_b, moves=0):
    conn.execute("insert or replace into odds_lines(fixture_id, bookmaker, open_a, open_b, close_a, close_b, moves, fetched) "
                 "values (?,?,?,?,?,?,?,?)", (fixture_id, book, open_a, open_b, close_a, close_b, moves, int(time.time())))


# ---------------------------------------------------------------- загрузка

def windows(ts_from, ts_to, days=9):
    t = ts_from
    while t < ts_to:
        yield time.strftime("%Y-%m-%d", time.gmtime(t)), time.strftime("%Y-%m-%d", time.gmtime(min(t + days * DAY, ts_to)))
        t += days * DAY


def sync_upcoming(conn, days=3, books=oddsapi.DEFAULT_BOOKS, log=print):
    """Ближайшие матчи с текущей линией → расписание (эталонные кэфы). Ваши кэфы остаются пустыми."""
    now = int(time.time())
    idx = TeamIndex(conn, now)
    market = oddsapi.winner_market(oddsapi.markets())
    if not market:
        raise oddsapi.OddsError("в каталоге рынков не найден «победитель матча»")
    added = updated = skipped = 0
    for d1, d2 in windows(now - 6 * 3600, now + days * DAY):
        for fx in oddsapi.fixtures(d1, d2):
            start = oddsapi.parse_ts(fx.get("startTime")) or now
            ta, tb = store_fixture(conn, fx["fixtureId"], "oddspapi", start, fx.get("participant1Name"), fx.get("participant2Name"),
                                   fx.get("tournamentName"), idx)
            if not (ta and tb):
                skipped += 1
                log("не сопоставлено: %s — %s" % (fx.get("participant1Name"), fx.get("participant2Name")))
                continue
            lines = oddsapi.winner_lines(oddsapi.odds(fx["fixtureId"]), market)
            book = next((b for b in books if b in lines), next(iter(lines), None))
            ra = rb = None
            if book:
                ra, rb = lines[book]["a"][-1][1], lines[book]["b"][-1][1]
                store_line(conn, fx["fixtureId"], book, lines[book]["a"][0][1], lines[book]["b"][0][1], ra, rb, len(lines[book]["a"]))
            names = {r["team_id"]: r["name"] for r in conn.execute("select team_id, name from teams where team_id in (?, ?)", (ta, tb))}
            row = conn.execute("select id from schedule where done = 0 and (fixture_id = ? or (team_a = ? and team_b = ? and "
                               "abs(coalesce(starts, created) - ?) < 43200))", (fx["fixtureId"], ta, tb, start)).fetchone()
            if row:
                conn.execute("update schedule set starts = ?, ref_a = coalesce(?, ref_a), ref_b = coalesce(?, ref_b), ref_book = coalesce(?, ref_book), "
                             "fixture_id = ?, tournament = ? where id = ?", (start, ra, rb, book, fx["fixtureId"], fx.get("tournamentName"), row["id"]))
                updated += 1
            else:
                conn.execute("insert into schedule(created, team_a, team_b, name_a, name_b, bo, tier, starts, ref_a, ref_b, ref_book, fixture_id, "
                             "tournament, done) values (?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                             (now, ta, tb, names.get(ta), names.get(tb), 3, "professional", start, ra, rb, book, fx["fixtureId"],
                              fx.get("tournamentName")))
                added += 1
    conn.commit()
    return {"added": added, "updated": updated, "skipped": skipped}


def sync_history(conn, ts_from, ts_to, books=oddsapi.DEFAULT_BOOKS, log=print, limit=None):
    """Сыгранные матчи за период: открывающая и закрывающая линия по рынку «победитель»."""
    idx = TeamIndex(conn)
    market = oddsapi.winner_market(oddsapi.markets())
    if not market:
        raise oddsapi.OddsError("в каталоге рынков не найден «победитель матча»")
    n = 0
    for d1, d2 in windows(ts_from, ts_to):
        for fx in oddsapi.fixtures(d1, d2):
            start = oddsapi.parse_ts(fx.get("startTime")) or 0
            ta, tb = store_fixture(conn, fx["fixtureId"], "oddspapi", start, fx.get("participant1Name"), fx.get("participant2Name"),
                                   fx.get("tournamentName"), idx)
            if not (ta and tb):
                continue
            if conn.execute("select 1 from odds_lines where fixture_id = ?", (fx["fixtureId"],)).fetchone():
                continue
            lines = oddsapi.winner_lines(oddsapi.historical(fx["fixtureId"], books), market)
            for book, ln in lines.items():
                oa, ca = oddsapi.open_close(ln["a"], start)
                ob, cb = oddsapi.open_close(ln["b"], start)
                store_line(conn, fx["fixtureId"], book, oa, ob, ca, cb, len(ln["a"]))
            n += 1
            if limit and n >= limit:
                conn.commit()
                return n
        conn.commit()
        log("%s — %s: матчей с линией %d" % (d1, d2, n))
    return n


def import_csv(conn, path, source="csv", book="csv"):
    """CSV: date,team_a,team_b,odds_a,odds_b[,close_a,close_b][,bookmaker]. Дата — YYYY-MM-DD или с временем (UTC)."""
    idx = TeamIndex(conn)
    n, bad = 0, []
    with open(path, encoding="utf-8-sig") as f:
        for i, line in enumerate(f):
            parts = [x.strip() for x in line.replace(";", ",").split(",")]
            if len(parts) < 5 or not re.match(r"\d{4}-\d{2}-\d{2}", parts[0]):
                continue
            try:
                fmt = "%Y-%m-%d %H:%M" if len(parts[0]) > 10 else "%Y-%m-%d"
                start = int(time.mktime(time.strptime(parts[0][:16].replace("T", " "), fmt)) - time.timezone)
                if len(parts[0]) <= 10:
                    start += 12 * 3600
                oa, ob = float(parts[3]), float(parts[4])
                ca = float(parts[5]) if len(parts) > 6 and parts[5] else oa
                cb = float(parts[6]) if len(parts) > 6 and parts[6] else ob
                bk = parts[7] if len(parts) > 7 and parts[7] else book
            except ValueError as e:
                bad.append("строка %d: %s" % (i + 1, e))
                continue
            fid = "%s:%s:%s:%s" % (source, parts[0][:10], norm(parts[1]), norm(parts[2]))
            ta, tb = store_fixture(conn, fid, source, start, parts[1], parts[2], None, idx)
            if not (ta and tb):
                bad.append("строка %d: команда не найдена (%s — %s)" % (i + 1, parts[1], parts[2]))
                continue
            store_line(conn, fid, bk, oa, ob, ca, cb)
            n += 1
    conn.commit()
    return n, bad


# ---------------------------------------------------------------- проверка на истории

def dataset(conn, book=None):
    """Матчи с линией, найденной серией и прогнозом walk-forward. Прогноз — на серию, по первой карте."""
    rows = conn.execute(
        "select f.fixture_id, f.start_ts, f.team_a, f.team_b, f.name_a, f.name_b, l.bookmaker, l.open_a, l.open_b, l.close_a, l.close_b "
        "from odds_fixtures f join odds_lines l on l.fixture_id = f.fixture_id where f.team_a is not null and f.team_b is not null "
        "order by f.start_ts").fetchall()
    prefer = [book] if book else list(oddsapi.DEFAULT_BOOKS)
    best = {}
    for r in rows:
        rank = prefer.index(r["bookmaker"]) if r["bookmaker"] in prefer else 99
        if book and rank == 99:
            continue
        if r["fixture_id"] not in best or rank < best[r["fixture_id"]][0]:
            best[r["fixture_id"]] = (rank, r)
    out, stats = [], {"fixtures": len(best), "no_series": 0, "no_pred": 0, "unfinished": 0}
    for _, r in best.values():
        s = find_series(conn, r["team_a"], r["team_b"], r["start_ts"])
        if not s:
            stats["no_series"] += 1
            continue
        need = s["bo"] // 2 + 1
        if s["bo"] != 2 and max(s["a"], s["b"]) < need:
            stats["unfinished"] += 1
            continue
        if s["a"] == s["b"]:
            continue
        p = conn.execute("select p_pre from wf_pred where match_id = ?", (s["first"],)).fetchone()
        if not p or p["p_pre"] is None:
            stats["no_pred"] += 1
            continue
        p_map = p["p_pre"] if s["a_radiant"] else 1.0 - p["p_pre"]
        p_series = series_distribution(p_map, s["bo"])["p_a"] if s["bo"] > 1 else p_map
        if not (r["close_a"] and r["close_b"]):
            continue
        out.append({"fixture": r["fixture_id"], "ts": s["ts"], "name_a": r["name_a"], "name_b": r["name_b"], "bo": s["bo"],
                    "tier": s["tier"], "book": r["bookmaker"], "open_a": r["open_a"] or r["close_a"], "open_b": r["open_b"] or r["close_b"],
                    "close_a": r["close_a"], "close_b": r["close_b"], "p_model": p_series, "y": 1 if s["a"] > s["b"] else 0})
    stats["usable"] = len(out)
    return out, stats


def simulate(data, s, price="close", ref="close"):
    """Политика на истории: ставка по цене price (open|close), смешивание с линией ref."""
    staked = profit = 0.0
    bets = wins = 0
    clv = []
    for d in data:
        oa, ob = d[price + "_a"], d[price + "_b"]
        dec = policy.decide(d["p_model"], oa, ob, d["bo"], s, ref_a=d[ref + "_a"], ref_b=d[ref + "_b"])
        if not dec["bet"]:
            continue
        stake = dec["stake_frac"]
        odds = oa if dec["side"] == "a" else ob
        won = (d["y"] == 1) == (dec["side"] == "a")
        staked += stake
        profit += stake * (odds - 1) if won else -stake
        bets += 1
        wins += 1 if won else 0
        close = d["close_a"] if dec["side"] == "a" else d["close_b"]
        clv.append(odds / close - 1.0)
    return {"bets": bets, "wins": wins, "staked": round(staked, 4), "profit": round(profit, 4),
            "roi": round(profit / staked, 4) if staked else None,
            "clv": round(sum(clv) / len(clv), 4) if clv else None}


def backtest(conn, book=None):
    data, stats = dataset(conn, book)
    rep = {"stats": stats, "n": len(data)}
    if not data:
        return rep
    ys = [d["y"] for d in data]
    p_book = [implied(d["close_a"], d["close_b"])[0] for d in data]
    p_open = [implied(d["open_a"], d["open_b"])[0] for d in data]
    p_model = [d["p_model"] for d in data]
    rep["margin"] = round(sum(implied(d["close_a"], d["close_b"])[2] for d in data) / len(data), 4)
    rep["sources"] = {"линия на закрытии": metrics(p_book, ys), "линия на открытии": metrics(p_open, ys), "модель": metrics(p_model, ys)}
    rep["blend"] = {}
    for w in (0.0, 0.3, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
        rep["blend"]["%.1f" % w] = metrics([w * b + (1 - w) * m for b, m in zip(p_book, p_model)], ys)
    rep["best_blend"] = min(rep["blend"], key=lambda k: rep["blend"][k]["logloss"])
    s = policy.settings(conn)
    rep["settings"] = s
    rep["policy"] = {"по открытию": simulate(data, s, "open", "open"), "по закрытию": simulate(data, s, "close", "close")}
    rep["grid"] = {}
    for w in (0.5, 0.7, 0.85):
        for e in (0.02, 0.04, 0.06):
            rep["grid"]["линия %.0f%%, порог %.0f п.п." % (100 * w, 100 * e)] = simulate(data, dict(s, blend=w, min_edge=e), "open", "open")
    seg = {}
    for d in data:
        for k in ("bo%d" % d["bo"], d["tier"] or "?", "фаворит модели" if max(d["p_model"], 1 - d["p_model"]) >= 0.7 else "равные"):
            seg.setdefault(k, []).append(d)
    rep["segments"] = {k: {"n": len(v), "модель": metrics([x["p_model"] for x in v], [x["y"] for x in v]),
                           "линия": metrics([implied(x["close_a"], x["close_b"])[0] for x in v], [x["y"] for x in v]),
                           "политика": simulate(v, s, "open", "open")} for k, v in seg.items()}
    rep["period"] = [min(d["ts"] for d in data), max(d["ts"] for d in data)]
    return rep


def describe(rep):
    if not rep.get("n"):
        st = rep.get("stats", {})
        return ("Матчей с линией, серией в базе и прогнозом нет. Загружено матчей с линией: %s, без серии в базе: %s, "
                "серия не доиграна по базе: %s, без прогноза: %s.\n"
                "Загрузите историю: book history --days 120 (нужен ODDSPAPI_KEY) или book import file.csv" % (
                    st.get("fixtures", 0), st.get("no_series", 0), st.get("unfinished", 0), st.get("no_pred", 0)))
    out = ["Проверка на кэфах: %d серий, %s — %s, средняя маржа %.1f%%" % (
        rep["n"], time.strftime("%Y-%m-%d", time.gmtime(rep["period"][0])), time.strftime("%Y-%m-%d", time.gmtime(rep["period"][1])),
        100 * rep["margin"])]
    out.append("Точность прогноза (log loss ниже — лучше):")
    for k, v in rep["sources"].items():
        out.append("  %-20s log loss %.4f, точность %.1f%%" % (k, v["logloss"], 100 * v["acc"]))
    out.append("Смешивание линии с моделью (доля линии → log loss): " + ", ".join(
        "%s → %.4f" % (k, v["logloss"]) for k, v in rep["blend"].items()) + "; лучшее: %s" % rep["best_blend"])
    out.append("Политика с текущими настройками (ставка в долях банка):")
    for k, v in rep["policy"].items():
        out.append("  %-12s %s" % (k, fmt(v)))
    out.append("Другие настройки, ставка по открытию:")
    for k, v in rep["grid"].items():
        out.append("  %-28s %s" % (k, fmt(v)))
    out.append("По сегментам:")
    for k, v in sorted(rep["segments"].items(), key=lambda kv: -kv[1]["n"]):
        out.append("  %-16s n %4d | модель %.4f | линия %.4f | %s" % (k, v["n"], v["модель"]["logloss"], v["линия"]["logloss"], fmt(v["политика"])))
    verdict = rep["sources"]["модель"]["logloss"] < rep["sources"]["линия на закрытии"]["logloss"]
    out.append("Вывод: модель %s линии на закрытии; %s." % (
        "точнее" if verdict else "уступает",
        "смешивание с весом линии %s даёт лучший прогноз" % rep["best_blend"]))
    if rep["n"] < 300:
        out.append("Серий меньше 300: ROI политики статистически ненадёжен, смотрите на log loss.")
    return "\n".join(out)


def fmt(v):
    if not v["bets"]:
        return "ставок 0"
    return "ставок %d, выиграно %d, ROI %+.1f%%, прибыль %+.2f банка, CLV %s" % (
        v["bets"], v["wins"], 100 * (v["roi"] or 0), v["profit"], ("%+.1f%%" % (100 * v["clv"])) if v["clv"] is not None else "—")
