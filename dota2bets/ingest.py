"""Синхронизация базы с OpenDota.

Датасет: про-матчи с 2024-01-01 из лиг premium и professional, где заданы обе команды.
Массово — через SQL-explorer (один запрос = одна единица бюджета, до 100 000 строк), свежее — через REST.
"""

import json
import time

from . import opendota as od
from .db import connect, kv_get, kv_set

SINCE = 1704067200            # 2024-01-01 UTC
TIERS = ("premium", "professional")
MATCH_PAGE = 20000
PICK_CHUNK = 3500             # матчей на запрос picks_bans (≤ 24 строки на матч)
PLAYER_CHUNK = 9000           # матчей на запрос player_matches (10 строк на матч)
LANE_CHUNK = 1500             # срезы массивов тяжелее: explorer отвечает 400 на большие диапазоны
REDRAFT_DAYS = 14

TTL_HEROES = 3 * od.DAY
TTL_HERO_STATS = 6 * od.HOUR
TTL_TEAMS = od.DAY
TTL_TEAM_PLAYERS = od.DAY


def tier_sql():
    return "(%s)" % ", ".join("'%s'" % t for t in TIERS)


def match_filter(alias="m", league_alias="l"):
    return ("%s.start_time >= %d and %s.tier in %s and %s.radiant_team_id is not null "
            "and %s.dire_team_id is not null" % (alias, SINCE, league_alias, tier_sql(), alias, alias))


# ---------------------------------------------------------------- матчи из explorer

MATCH_SQL = """
select m.match_id, m.start_time, m.duration, m.radiant_win, m.leagueid, l.tier, m.radiant_team_id,
       m.dire_team_id, m.series_id, m.series_type, m.radiant_score, m.dire_score, mp.patch,
       m.radiant_gold_adv, (m.picks_bans is not null) as has_draft
from matches m
join leagues l on l.leagueid = m.leagueid
left join match_patch mp on mp.match_id = m.match_id
where %s and m.match_id > %d
order by m.match_id
limit %d
"""


def upsert_matches(conn, rows, source):
    for r in rows:
        gold = r.get("radiant_gold_adv")
        conn.execute(
            """insert into matches(match_id, start_time, duration, radiant_win, leagueid, tier, radiant_team_id,
                   dire_team_id, series_id, series_type, patch, radiant_score, dire_score, gold_adv, has_draft, source)
               values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               on conflict(match_id) do update set
                   duration=excluded.duration, radiant_win=excluded.radiant_win, tier=excluded.tier,
                   patch=coalesce(excluded.patch, matches.patch), gold_adv=coalesce(excluded.gold_adv, matches.gold_adv),
                   has_draft=max(excluded.has_draft, matches.has_draft), radiant_score=excluded.radiant_score,
                   dire_score=excluded.dire_score""",
            (r["match_id"], r["start_time"], r.get("duration"), 1 if r.get("radiant_win") else 0, r.get("leagueid"),
             r.get("tier"), r.get("radiant_team_id"), r.get("dire_team_id"), r.get("series_id"), r.get("series_type"),
             r.get("patch"), r.get("radiant_score"), r.get("dire_score"),
             json.dumps(gold, separators=(",", ":")) if gold else None,
             1 if r.get("has_draft") else 0, source))


def sync_matches(conn, log=od.log):
    """Новые матчи по match_id страницами. Возвращает список новых match_id."""
    last = kv_get(conn, "explorer_last_match_id", 0)
    new_ids = []
    while True:
        rows = od.explorer(MATCH_SQL % (match_filter(), last, MATCH_PAGE))
        if not rows:
            break
        upsert_matches(conn, rows, "explorer")
        new_ids += [r["match_id"] for r in rows]
        last = rows[-1]["match_id"]
        kv_set(conn, "explorer_last_match_id", last)
        conn.commit()
        log("matches: +%d (до %d)" % (len(rows), last))
        if len(rows) < MATCH_PAGE:
            break
    return new_ids


# ---------------------------------------------------------------- драфты и игроки

def chunks(ids, size):
    ids = sorted(ids)
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


def in_range_sql(lo, hi):
    return ("match_id between %d and %d and match_id in (select m.match_id from matches m join leagues l "
            "on l.leagueid = m.leagueid where %s)" % (lo, hi, match_filter()))


def sync_picks(conn, match_ids, log=od.log):
    for chunk in chunks(match_ids, PICK_CHUNK):
        rows = od.explorer("select match_id, is_pick, hero_id, team, ord from picks_bans where %s"
                           % in_range_sql(chunk[0], chunk[-1]))
        conn.executemany("insert or ignore into picks(match_id, team, is_pick, hero_id, ord) values (?,?,?,?,?)",
                         [(r["match_id"], r["team"], 1 if r["is_pick"] else 0, r["hero_id"], r["ord"]) for r in rows])
        got = {r["match_id"] for r in rows}
        conn.executemany("update matches set has_draft=1 where match_id=?", [(m,) for m in got])
        conn.commit()
        log("picks: %d строк для %d матчей" % (len(rows), len(got)))


PLAYER_FIELDS = ("match_id", "account_id", "player_slot", "hero_id", "kills", "deaths", "assists", "gold_per_min",
                 "xp_per_min", "net_worth", "last_hits", "lane_role", "level", "hero_damage", "tower_damage")
LANE_SQL = "gold_t[11] as gold10, xp_t[11] as xp10, lh_t[11] as lh10"   # срез на 10-й минуте (индекс 11: минута 0 первая)
PLAYER_SELECT = ", ".join(PLAYER_FIELDS) + ", " + LANE_SQL


def upsert_players(conn, rows):
    conn.executemany(
        """insert or replace into match_players(match_id, player_slot, account_id, hero_id, is_radiant, kills, deaths,
               assists, gpm, xpm, net_worth, last_hits, lane_role, level, hero_damage, tower_damage, gold10, xp10, lh10)
           values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(r["match_id"], r["player_slot"], r.get("account_id"), r.get("hero_id"),
          1 if (r["player_slot"] or 0) < 128 else 0, r.get("kills"), r.get("deaths"), r.get("assists"),
          r.get("gold_per_min"), r.get("xp_per_min"), r.get("net_worth"), r.get("last_hits"), r.get("lane_role"),
          r.get("level"), r.get("hero_damage"), r.get("tower_damage"), r.get("gold10"), r.get("xp10"), r.get("lh10"))
         for r in rows])
    got = {r["match_id"] for r in rows}
    conn.executemany("update matches set has_players=1 where match_id=?", [(m,) for m in got])
    return got


def sync_players(conn, match_ids, log=od.log):
    for chunk in chunks(match_ids, PLAYER_CHUNK):
        rows = od.explorer("select %s from player_matches where %s"
                           % (PLAYER_SELECT, in_range_sql(chunk[0], chunk[-1])))
        got = upsert_players(conn, rows)
        conn.commit()
        log("players: %d строк для %d матчей" % (len(rows), len(got)))


def redraft(conn, log=od.log):
    """Свежие матчи, у которых при первой выгрузке ещё не было драфта."""
    since = int(time.time()) - REDRAFT_DAYS * od.DAY
    ids = [r[0] for r in conn.execute(
        "select match_id from matches where has_draft=0 and start_time >= ? and source='explorer'", (since,))]
    if not ids:
        return
    log("повторно: %d матчей без драфта" % len(ids))
    sync_picks(conn, ids, log)
    missing_players = [r[0] for r in conn.execute(
        "select match_id from matches where has_players=0 and start_time >= ? and source='explorer'", (since,))]
    if missing_players:
        sync_players(conn, missing_players, log)


def backfill_lanes(conn, log=od.log, days=None):
    """Досыпать срезы 10-й минуты. По умолчанию — только матчи за последние 30 дней (у старых матчей без среза
    массивы короче 11 минут, перезапрашивать их бессмысленно); days=0 — вся база (первичная догрузка).
    Ошибка explorer на диапазоне не роняет sync: диапазон делится пополам, в худшем случае пропускается."""
    days = 30 if days is None else days
    sql = ("select distinct mp.match_id from match_players mp join matches m on m.match_id=mp.match_id "
           "where mp.gold10 is null and m.source='explorer' and m.duration >= 660")
    params = ()
    if days:
        sql += " and m.start_time >= ?"
        params = (int(time.time()) - days * od.DAY,)
    ids = sorted(r[0] for r in conn.execute(sql, params))
    if not ids:
        return 0
    log("лейнинг: %d матчей без среза 10-й минуты" % len(ids))
    n = 0
    queue = list(chunks(ids, LANE_CHUNK))
    while queue:
        chunk = queue.pop(0)
        try:
            rows = od.explorer("select match_id, player_slot, %s from player_matches where match_id in (%s)"
                               % (LANE_SQL, ",".join(str(i) for i in chunk)) if len(chunk) <= 300 else
                               "select match_id, player_slot, %s from player_matches where %s"
                               % (LANE_SQL, in_range_sql(chunk[0], chunk[-1])))
        except od.BudgetError:
            raise
        except Exception as e:
            if len(chunk) > 50:
                half = len(chunk) // 2
                queue[:0] = [chunk[:half], chunk[half:]]
                continue
            log("лейнинг: пропуск %d матчей (%s)" % (len(chunk), str(e)[-60:]))
            continue
        conn.executemany("update match_players set gold10=?, xp10=?, lh10=? where match_id=? and player_slot=?",
                         [(r["gold10"], r["xp10"], r["lh10"], r["match_id"], r["player_slot"]) for r in rows])
        conn.commit()
        n += len(rows)
    log("лейнинг: обновлено %d строк" % n)
    return n


# ---------------------------------------------------------------- справочники

def sync_leagues(conn):
    rows = od.explorer("select leagueid, name, tier from leagues where tier in %s" % tier_sql())
    conn.executemany("insert or replace into leagues(leagueid, name, tier) values (?,?,?)",
                     [(r["leagueid"], r["name"], r["tier"]) for r in rows])
    conn.commit()
    return len(rows)


def sync_teams(conn):
    """Команды из нашего датасета (explorer) плюс рейтинг OpenDota из /teams."""
    now = int(time.time())
    rows = od.explorer(
        "select t.team_id, t.name, t.tag, t.logo_url, r.rating, r.wins, r.losses, r.last_match_time "
        "from teams t left join team_rating r on r.team_id = t.team_id where t.team_id in "
        "(select m.radiant_team_id from matches m join leagues l on l.leagueid = m.leagueid where %s "
        " union select m.dire_team_id from matches m join leagues l on l.leagueid = m.leagueid where %s)"
        % (match_filter(), match_filter()))
    conn.executemany(
        """insert into teams(team_id, name, tag, logo_url, rating, wins, losses, last_match_time, updated)
           values (?,?,?,?,?,?,?,?,?)
           on conflict(team_id) do update set name=excluded.name, tag=excluded.tag, logo_url=excluded.logo_url,
               rating=coalesce(excluded.rating, teams.rating), wins=excluded.wins, losses=excluded.losses,
               last_match_time=excluded.last_match_time, updated=excluded.updated""",
        [(r["team_id"], r["name"], r["tag"], r["logo_url"], r["rating"], r["wins"], r["losses"],
          r["last_match_time"], now) for r in rows])
    for t in od.get("teams", ttl=TTL_TEAMS):
        conn.execute(
            """insert into teams(team_id, name, tag, logo_url, rating, wins, losses, last_match_time, updated)
               values (?,?,?,?,?,?,?,?,?)
               on conflict(team_id) do update set name=coalesce(excluded.name, teams.name), tag=excluded.tag,
                   logo_url=excluded.logo_url, rating=excluded.rating, wins=excluded.wins, losses=excluded.losses,
                   last_match_time=excluded.last_match_time, updated=excluded.updated""",
            (t["team_id"], t.get("name"), t.get("tag"), t.get("logo_url"), t.get("rating"), t.get("wins"),
             t.get("losses"), t.get("last_match_time"), now))
    conn.commit()
    return len(rows)


def sync_notable_players(conn):
    rows = od.explorer("select account_id, name, team_id from notable_players")
    now = int(time.time())
    conn.executemany(
        """insert into players(account_id, name, team_id, updated) values (?,?,?,?)
           on conflict(account_id) do update set name=excluded.name, team_id=excluded.team_id, updated=excluded.updated""",
        [(r["account_id"], r["name"], r["team_id"], now) for r in rows])
    conn.commit()
    return len(rows)


def sync_heroes(conn):
    now = int(time.time())
    heroes = od.get("constants/heroes", ttl=TTL_HEROES)
    conn.executemany(
        "insert or replace into heroes(hero_id, name, localized_name, primary_attr, roles, updated) values (?,?,?,?,?,?)",
        [(h["id"], h["name"], h["localized_name"], h.get("primary_attr"), json.dumps(h.get("roles") or []), now)
         for h in heroes.values()])
    stats = od.get("heroStats", ttl=TTL_HERO_STATS)
    conn.executemany(
        "insert or replace into hero_meta(hero_id, pro_pick, pro_win, pro_ban, updated) values (?,?,?,?,?)",
        [(h["id"], h.get("pro_pick") or 0, h.get("pro_win") or 0, h.get("pro_ban") or 0, now) for h in stats])
    conn.commit()
    return len(heroes)


def team_players(conn, team_id, max_age=TTL_TEAM_PLAYERS):
    """Состав команды по /teams/{id}/players (кеш в базе на сутки). Возвращает текущих участников."""
    now = int(time.time())
    row = conn.execute("select max(fetched) f from team_players where team_id=?", (team_id,)).fetchone()
    if not row["f"] or now - row["f"] > max_age:
        data = od.get("teams/%d/players" % team_id, ttl=max_age)
        conn.execute("delete from team_players where team_id=?", (team_id,))
        conn.executemany(
            "insert or replace into team_players(team_id, account_id, name, games_played, wins, is_current, fetched) "
            "values (?,?,?,?,?,?,?)",
            [(team_id, p["account_id"], p.get("name"), p.get("games_played"), p.get("wins"),
              1 if p.get("is_current_team_member") else 0, now) for p in data if p.get("account_id")])
        conn.executemany(
            "insert into players(account_id, name, updated) values (?,?,?) on conflict(account_id) do update set "
            "name=coalesce(players.name, excluded.name)",
            [(p["account_id"], p.get("name"), now) for p in data if p.get("account_id") and p.get("name")])
        conn.commit()
    return conn.execute(
        "select account_id, name, games_played, wins from team_players where team_id=? and is_current=1 "
        "order by games_played desc", (team_id,)).fetchall()


# ---------------------------------------------------------------- свежее через REST

def parse_match_detail(m):
    """Полный ответ /matches/{id} → строки для matches, picks, match_players."""
    league = m.get("league") or {}
    row = {
        "match_id": m["match_id"], "start_time": m["start_time"], "duration": m.get("duration"),
        "radiant_win": m.get("radiant_win"), "leagueid": m.get("leagueid"), "tier": league.get("tier"),
        "radiant_team_id": m.get("radiant_team_id"), "dire_team_id": m.get("dire_team_id"),
        "series_id": m.get("series_id"), "series_type": m.get("series_type"), "patch": m.get("patch"),
        "radiant_score": m.get("radiant_score"), "dire_score": m.get("dire_score"),
        "radiant_gold_adv": m.get("radiant_gold_adv"), "has_draft": bool(m.get("picks_bans")),
    }
    picks = [(m["match_id"], p["team"], 1 if p["is_pick"] else 0, p["hero_id"], p.get("order", p.get("ord", i)))
             for i, p in enumerate(m.get("picks_bans") or [])]
    players = []
    for p in m.get("players") or []:
        d = {k: p.get(k) for k in PLAYER_FIELDS if k != "match_id"}
        d["match_id"] = m["match_id"]
        for src, dst in (("gold_t", "gold10"), ("xp_t", "xp10"), ("lh_t", "lh10")):
            arr = p.get(src)
            d[dst] = arr[10] if isinstance(arr, list) and len(arr) > 10 else None
        players.append(d)
    return row, picks, players


def sync_fresh(conn, limit=100, log=od.log):
    """Матчи из /proMatches, которых ещё нет в базе; детали через /matches/{id} (limit запросов)."""
    known_leagues = {r[0]: r[1] for r in conn.execute("select leagueid, tier from leagues")}
    have = kv_get(conn, "explorer_last_match_id", 0)
    fresh = []
    less_than = None
    for _ in range(10):
        path = "proMatches" + ("?less_than_match_id=%d" % less_than if less_than else "")
        page = od.get(path)
        if not page:
            break
        for m in page:
            if m["match_id"] <= have or m["start_time"] < SINCE:
                page = None
                break
            if (m.get("leagueid") in known_leagues and m.get("radiant_team_id") and m.get("dire_team_id")
                    and m.get("radiant_win") is not None):
                fresh.append(m)
        if page is None:
            break
        less_than = page[-1]["match_id"]
    fresh_ids = [m["match_id"] for m in fresh]
    exist = set()
    for chunk in chunks(fresh_ids, 900):
        exist |= {r[0] for r in conn.execute(
            "select match_id from matches where has_draft=1 and has_players=1 and match_id in (%s)"
            % ",".join("?" * len(chunk)), chunk)}
    todo = [m for m in fresh if m["match_id"] not in exist]
    log("свежих матчей в лигах белого списка: %d, без деталей: %d, лимит: %d" % (len(fresh), len(todo), limit))
    done = 0
    for m in sorted(todo, key=lambda x: -x["match_id"])[:limit]:
        try:
            detail = od.get("matches/%d" % m["match_id"], ttl=30 * od.DAY)
        except Exception as e:
            log("matches/%d: %s" % (m["match_id"], e))
            continue
        row, picks, players = parse_match_detail(detail)
        row["tier"] = row["tier"] or known_leagues.get(row["leagueid"])
        upsert_matches(conn, [row], "rest")
        conn.executemany("insert or ignore into picks(match_id, team, is_pick, hero_id, ord) values (?,?,?,?,?)", picks)
        if players:
            upsert_players(conn, players)
        conn.commit()
        done += 1
    return done


# ---------------------------------------------------------------- всё вместе

def sync(conn=None, fresh_limit=100, log=od.log):
    conn = conn or connect()
    log("лиги: %d" % sync_leagues(conn))
    new_ids = sync_matches(conn, log)
    if new_ids:
        sync_picks(conn, new_ids, log)
        sync_players(conn, new_ids, log)
    for step in (redraft, backfill_lanes):
        try:
            step(conn, log)
        except od.BudgetError:
            raise
        except Exception as e:  # необязательные шаги не должны ронять синхронизацию
            log("%s: пропущено (%s)" % (step.__name__, str(e)[-80:]))
    log("команды: %d" % sync_teams(conn))
    log("игроки (notable): %d" % sync_notable_players(conn))
    log("герои: %d" % sync_heroes(conn))
    if fresh_limit:
        log("свежие детали: %d" % sync_fresh(conn, fresh_limit, log))
    kv_set(conn, "last_sync", int(time.time()))
    conn.commit()
    return conn
