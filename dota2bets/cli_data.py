"""Подкоманды просмотра данных: teams, team, player, h2h, meta, live."""

import json
import time

from . import opendota as od
from . import ratings
from .db import connect

DAY = 86400


def fmt_ts(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else "-"


# ---------------------------------------------------------------- поиск

def resolve_team(conn, query):
    """team_id по числу, тегу или части имени. При нескольких — та, что играла последней ПО НАШЕЙ БАЗЕ матчей
    (поле last_match_time из OpenDota отстаёт и после решаффлов указывает на старые составы)."""
    q = str(query).strip()
    if q.isdigit():
        row = conn.execute("select * from teams where team_id=?", (int(q),)).fetchone()
        if row:
            return row
    order = (" order by (select max(m.start_time) from matches m where m.radiant_team_id=t.team_id "
             "or m.dire_team_id=t.team_id) desc nulls last, t.last_match_time desc limit 1")
    row = conn.execute("select t.* from teams t where lower(t.name)=lower(?) or lower(t.tag)=lower(?)" + order,
                       (q, q)).fetchone()
    if row:
        return row
    row = conn.execute("select t.* from teams t where t.name like ? or t.tag like ?" + order,
                       ("%" + q + "%", "%" + q + "%")).fetchone()
    if not row:
        raise SystemExit("команда не найдена: %s" % query)
    return row


def resolve_player(conn, query):
    q = str(query).strip()
    if q.isdigit():
        return int(q)
    # однофамильцев в notable_players много — берём того, у кого больше матчей в базе
    sql = ("select p.account_id from players p left join ratings_current r on r.entity='player' and r.id=p.account_id "
           "where %s order by coalesce(r.games, 0) desc, p.updated desc limit 1")
    row = conn.execute(sql % "lower(p.name)=lower(?)", (q,)).fetchone()
    if not row:
        row = conn.execute(sql % "p.name like ?", ("%" + q + "%",)).fetchone()
    if not row:
        raise SystemExit("игрок не найден: %s" % query)
    return row["account_id"]


def departed(conn, team_id, lineup, since_ts=None):
    """Игроки пятёрки, которые после последнего матча команды играли за другую команду.
    Возвращает {account_id: (team_id, team_name, ts)}."""
    if not lineup:
        return {}
    if since_ts is None:
        ms = team_matches(conn, team_id, 1)
        since_ts = ms[0]["start_time"] if ms else 0
    out = {}
    for acc in lineup:
        r = conn.execute(
            "select m.start_time, case when mp.is_radiant=1 then m.radiant_team_id else m.dire_team_id end as tid "
            "from match_players mp join matches m on m.match_id=mp.match_id where mp.account_id=? and m.start_time>? "
            "order by m.start_time desc limit 1", (acc, since_ts)).fetchone()
        if r and r["tid"] != team_id:
            t = conn.execute("select name from teams where team_id=?", (r["tid"],)).fetchone()
            out[acc] = (r["tid"], t["name"] if t else str(r["tid"]), r["start_time"])
    return out


def actual_roster(conn, team_id, matches=5, before_ts=None):
    """Пятёрка по фактическим последним матчам команды (флагу is_current_team_member верить нельзя).
    Возвращает (список account_id, ts последнего матча)."""
    ms = team_matches(conn, team_id, matches, before_ts)
    if not ms:
        return [], None
    # после решаффла старые матчи не должны перевешивать новый состав: берём только матчи в пределах 30 дней
    # от последнего матча команды
    ms = [m for m in ms if ms[0]["start_time"] - m["start_time"] <= 30 * DAY]
    # идёт серия или турнирный день: состав — ровно пятёрка последней карты (стендины видны сразу)
    if before_ts is None and time.time() - ms[0]["start_time"] < 36 * 3600:
        ms = ms[:1]
    weight = {}
    for i, m in enumerate(ms):
        rows = conn.execute("select account_id from match_players where match_id=? and is_radiant=? and account_id is not null",
                            (m["match_id"], 1 if m["is_radiant"] else 0)).fetchall()
        for r in rows:
            weight[r["account_id"]] = weight.get(r["account_id"], 0) + (matches - i)
    top = sorted(weight, key=lambda p: -weight[p])[:5]
    return top, ms[0]["start_time"]


def hero_names(conn):
    return {r["hero_id"]: r["localized_name"] for r in conn.execute("select hero_id, localized_name from heroes")}


def player_names(conn):
    return {r["account_id"]: r["name"] for r in conn.execute("select account_id, name from players")}


# ---------------------------------------------------------------- команды

def team_matches(conn, team_id, limit=20, before_ts=None):
    sql = ("select m.*, (m.radiant_team_id=?) as is_radiant, "
           "case when m.radiant_team_id=? then m.dire_team_id else m.radiant_team_id end as opp, "
           "case when (m.radiant_team_id=?)=(m.radiant_win=1) then 1 else 0 end as won "
           "from matches m where (m.radiant_team_id=? or m.dire_team_id=?) and m.radiant_win is not null")
    params = [team_id] * 5
    if before_ts:
        sql += " and m.start_time < ?"
        params.append(before_ts)
    sql += " order by m.start_time desc limit ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def team_heroes(conn, team_id, days=180, limit=15):
    since = int(time.time()) - days * DAY
    rows = conn.execute(
        "select p.hero_id, count(*) g, sum(case when (m.radiant_team_id=?)=(m.radiant_win=1) then 1 else 0 end) w "
        "from picks p join matches m on m.match_id=p.match_id "
        "where p.is_pick=1 and m.start_time>=? and ((m.radiant_team_id=? and p.team=0) or (m.dire_team_id=? and p.team=1)) "
        "group by p.hero_id order by g desc limit ?", (team_id, since, team_id, team_id, limit)).fetchall()
    return rows


def cmd_teams(args):
    conn = connect()
    q = "%" + (args.q or "") + "%"
    rows = conn.execute("select * from teams where name like ? or tag like ? order by last_match_time desc limit ?",
                        (q, q, args.limit)).fetchall()
    for t in rows:
        elo, games = ratings.current(conn, "team", t["team_id"])
        print("%-10d %-28s %-8s elo %4.0f (%d игр)  od %5s  последний матч %s" % (
            t["team_id"], (t["name"] or "")[:28], (t["tag"] or "")[:8], elo, games,
            "%.0f" % t["rating"] if t["rating"] else "-", fmt_ts(t["last_match_time"])))


def cmd_team(args):
    conn = connect()
    t = resolve_team(conn, args.team)
    tid = t["team_id"]
    names = hero_names(conn)
    teams = {r["team_id"]: r["name"] or r["tag"] or str(r["team_id"]) for r in conn.execute("select team_id, name, tag from teams")}
    elo, games = ratings.current(conn, "team", tid)
    print("%s [%s] id=%d  Elo %.0f (%d матчей)  рейтинг OpenDota %s  W/L %s/%s" % (
        t["name"], t["tag"], tid, elo, games, "%.0f" % t["rating"] if t["rating"] else "-", t["wins"], t["losses"]))

    from .ingest import team_players
    try:
        roster = team_players(conn, tid)
    except Exception as e:
        roster = []
        print("состав: недоступен (%s)" % e)
    pn = player_names(conn)
    actual, last_ts = actual_roster(conn, tid)
    if actual:
        gone = departed(conn, tid, actual, last_ts)
        print("\nФактическая пятёрка по последним 5 матчам (последний %s):" % fmt_ts(last_ts))
        for p in actual:
            pr, pg = ratings.current(conn, "player", p)
            mark = "  УШЁЛ: играл за %s %s" % (gone[p][1], fmt_ts(gone[p][2])) if p in gone else ""
            print("  %-22s %-12d Elo %4.0f (%d матчей в базе)  линия %s%s" % (
                (pn.get(p) or "?")[:22], p, pr, pg, player_role(conn, p) or "-", mark))
        if gone:
            print("Внимание: %d из пятёрки с тех пор играли за другие команды — состав устарел, замены в базе нет." % len(gone))
    if roster:
        listed = {p["account_id"] for p in roster}
        extra = [p for p in roster if p["account_id"] not in actual]
        print("Заявлены в OpenDota (is_current_team_member), но не в фактической пятёрке: %s" % (
            ", ".join("%s (%s/%s)" % (p["name"], p["wins"], p["games_played"]) for p in extra) or "нет"))
        missing = [p for p in actual if p not in listed]
        if missing:
            print("В фактической пятёрке, но не заявлены: %s" % ", ".join(pn.get(p) or str(p) for p in missing))

    ms = team_matches(conn, tid, args.limit)
    wins = sum(m["won"] for m in ms)
    print("\nПоследние %d матчей: %d-%d" % (len(ms), wins, len(ms) - wins))
    for m in ms:
        print("  %s %s %-24s %s %s  %s" % (fmt_ts(m["start_time"]), "W" if m["won"] else "L",
                                            teams.get(m["opp"], m["opp"])[:24], "R" if m["is_radiant"] else "D",
                                            "bo%s" % {0: 1, 1: 3, 2: 5}.get(m["series_type"], "?"),
                                            (m["tier"] or "")[:4]))
    print("\nГерои за 180 дней (игры, винрейт):")
    for h in team_heroes(conn, tid):
        print("  %-20s %3d  %3.0f%%" % (names.get(h["hero_id"], h["hero_id"]), h["g"], 100.0 * h["w"] / h["g"]))


def player_heroes(conn, account_id, days=365, limit=15):
    since = int(time.time()) - days * DAY
    return conn.execute(
        "select mp.hero_id, count(*) g, sum(case when mp.is_radiant=m.radiant_win then 1 else 0 end) w, "
        "avg(mp.gpm) gpm, avg(mp.xpm) xpm, avg((mp.kills+mp.assists)*1.0/max(mp.deaths,1)) kda "
        "from match_players mp join matches m on m.match_id=mp.match_id "
        "where mp.account_id=? and m.start_time>=? group by mp.hero_id order by g desc limit ?",
        (account_id, since, limit)).fetchall()


def player_role(conn, account_id, days=180):
    since = int(time.time()) - days * DAY
    rows = conn.execute(
        "select lane_role, count(*) n from match_players mp join matches m on m.match_id=mp.match_id "
        "where mp.account_id=? and m.start_time>=? and lane_role is not null group by lane_role order by n desc",
        (account_id, since)).fetchall()
    return rows[0]["lane_role"] if rows else None


def cmd_player(args):
    conn = connect()
    pid = resolve_player(conn, args.player)
    names = hero_names(conn)
    p = conn.execute("select * from players where account_id=?", (pid,)).fetchone()
    elo, games = ratings.current(conn, "player", pid)
    team = conn.execute("select name from teams where team_id=?", (p["team_id"],)).fetchone() if p and p["team_id"] else None
    print("%s id=%d  команда: %s  Elo %.0f (%d матчей)  роль (lane_role, 180 дн): %s" % (
        p["name"] if p else "?", pid, team["name"] if team else "-", elo, games, player_role(conn, pid)))
    rows = conn.execute(
        "select m.start_time, (mp.is_radiant=m.radiant_win) won from match_players mp join matches m on m.match_id=mp.match_id "
        "where mp.account_id=? order by m.start_time desc limit ?", (pid, args.limit)).fetchall()
    if rows:
        w = sum(1 for r in rows if r["won"])
        print("Последние %d матчей: %d-%d, последний %s" % (len(rows), w, len(rows) - w, fmt_ts(rows[0]["start_time"])))
    print("\nГерои за 365 дней (игры, винрейт, GPM, XPM, KDA):")
    for h in player_heroes(conn, pid):
        print("  %-20s %3d  %3.0f%%  %4.0f %4.0f  %.1f" % (names.get(h["hero_id"], h["hero_id"]), h["g"],
                                                           100.0 * h["w"] / h["g"], h["gpm"] or 0, h["xpm"] or 0, h["kda"] or 0))


def h2h(conn, a, b, days=730, before_ts=None):
    since = int((before_ts or time.time()) - days * DAY)
    sql = ("select match_id, start_time, radiant_team_id, radiant_win, series_id from matches where radiant_win is not null "
           "and ((radiant_team_id=? and dire_team_id=?) or (radiant_team_id=? and dire_team_id=?)) and start_time>=?")
    params = [a, b, b, a, since]
    if before_ts:
        sql += " and start_time<?"
        params.append(before_ts)
    rows = conn.execute(sql + " order by start_time desc", params).fetchall()
    wins_a = sum(1 for r in rows if (r["radiant_team_id"] == a) == bool(r["radiant_win"]))
    return rows, wins_a


def cmd_h2h(args):
    conn = connect()
    a, b = resolve_team(conn, args.a), resolve_team(conn, args.b)
    rows, wa = h2h(conn, a["team_id"], b["team_id"])
    print("%s vs %s за 2 года: %d-%d (%d карт)" % (a["name"], b["name"], wa, len(rows) - wa, len(rows)))
    for r in rows[:args.limit]:
        won = (r["radiant_team_id"] == a["team_id"]) == bool(r["radiant_win"])
        print("  %s %s  match %d" % (fmt_ts(r["start_time"]), a["tag"] if won else b["tag"], r["match_id"]))


def cmd_meta(args):
    conn = connect()
    names = hero_names(conn)
    since = int(time.time()) - args.days * DAY
    total = conn.execute("select count(*) from matches where start_time>=? and has_draft=1", (since,)).fetchone()[0]
    rows = conn.execute(
        "select p.hero_id, sum(p.is_pick) picks, sum(1-p.is_pick) bans, "
        "sum(case when p.is_pick=1 and ((p.team=0 and m.radiant_win=1) or (p.team=1 and m.radiant_win=0)) then 1 else 0 end) wins "
        "from picks p join matches m on m.match_id=p.match_id where m.start_time>=? group by p.hero_id "
        "order by picks+bans desc", (since,)).fetchall()
    if args.hero:
        rows = [r for r in rows if args.hero.lower() in (names.get(r["hero_id"], "") or "").lower()]
    print("Про-мета за %d дней, %d матчей с драфтом (пики, баны, контест %%, винрейт):" % (args.days, total))
    for r in rows[:args.limit]:
        wr = 100.0 * r["wins"] / r["picks"] if r["picks"] else 0
        print("  %-20s %4d %4d  %3.0f%%  %3.0f%%" % (names.get(r["hero_id"], r["hero_id"]), r["picks"], r["bans"],
                                                   100.0 * (r["picks"] + r["bans"]) / max(total, 1), wr))


def live_games(conn=None):
    """Лиговые игры из /live: команды, герои, игроки."""
    games = od.get("live")
    out = []
    for g in games:
        if not g.get("league_id"):
            continue
        players = g.get("players") or []
        out.append({
            "match_id": g.get("match_id"), "league_id": g.get("league_id"), "series_id": g.get("series_id"),
            "team_id_radiant": g.get("team_id_radiant"), "team_id_dire": g.get("team_id_dire"),
            "team_name_radiant": g.get("team_name_radiant"), "team_name_dire": g.get("team_name_dire"),
            "game_time": g.get("game_time"), "radiant_score": g.get("radiant_score"), "dire_score": g.get("dire_score"),
            "spectators": g.get("spectators"), "radiant_lead": g.get("radiant_lead"),
            "radiant": [(p.get("account_id"), p.get("hero_id")) for p in players if p.get("team") == 0],
            "dire": [(p.get("account_id"), p.get("hero_id")) for p in players if p.get("team") == 1],
        })
    return out


def cmd_live(args):
    from . import live as live_mod
    conn = connect()
    names = hero_names(conn)
    res = live_mod.games(conn)
    games = res["games"]
    print("источник: %s%s" % (res["source"], " (%s)" % res["error"] if res["error"] else ""))
    if not games:
        print("лиговых игр сейчас нет")
    for g in games:
        print("%s vs %s  (%s:%s, %d мин, зрителей %s)  match %s" % (
            g["team_name_radiant"] or g["team_id_radiant"], g["team_name_dire"] or g["team_id_dire"],
            g["radiant_score"], g["dire_score"], (g["game_time"] or 0) // 60, g["spectators"], g["match_id"]))
        for side in ("radiant", "dire"):
            print("   %s: %s" % (side, ", ".join(names.get(h, str(h)) for _, h in g[side] if h)))


def register(sub):
    s = sub.add_parser("teams", help="поиск команд")
    s.add_argument("--q", default="")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(func=cmd_teams)

    s = sub.add_parser("team", help="команда: состав, Elo, форма, герои")
    s.add_argument("team")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_team)

    s = sub.add_parser("player", help="игрок: Elo, роль, герои")
    s.add_argument("player")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(func=cmd_player)

    s = sub.add_parser("h2h", help="личные встречи")
    s.add_argument("a")
    s.add_argument("b")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_h2h)

    s = sub.add_parser("meta", help="про-мета героев")
    s.add_argument("--hero", default="")
    s.add_argument("--days", type=int, default=60)
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(func=cmd_meta)

    s = sub.add_parser("live", help="лиговые игры прямо сейчас")
    s.set_defaults(func=cmd_live)
