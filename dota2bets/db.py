"""SQLite: схема и подключение.

Таблицы, которые заполняет sync (matches, picks, match_players, teams, team_players, players, leagues, heroes,
hero_meta), можно пересоздавать. Таблицы predictions, rating_snapshots, model_coefs, player_hero_cache, notes
хранят результаты работы и переживают пересинхронизацию.
"""

import json
import os
import sqlite3

from . import DATA

DB_PATH = os.environ.get("DOTA2BETS_DB") or os.path.join(DATA, "dota2bets.sqlite")

SCHEMA = """
create table if not exists matches (
    match_id integer primary key,
    start_time integer not null,
    duration integer,
    radiant_win integer,
    leagueid integer,
    tier text,
    radiant_team_id integer,
    dire_team_id integer,
    series_id integer,
    series_type integer,
    patch integer,
    radiant_score integer,
    dire_score integer,
    gold_adv text,
    has_draft integer default 0,
    has_players integer default 0,
    source text
);
create index if not exists matches_time on matches(start_time);
create index if not exists matches_radiant on matches(radiant_team_id, start_time);
create index if not exists matches_dire on matches(dire_team_id, start_time);

create table if not exists picks (
    match_id integer not null,
    team integer not null,
    is_pick integer not null,
    hero_id integer not null,
    ord integer not null,
    primary key (match_id, ord, team, is_pick, hero_id)
);

create table if not exists match_players (
    match_id integer not null,
    player_slot integer not null,
    account_id integer,
    hero_id integer,
    is_radiant integer,
    kills integer, deaths integer, assists integer,
    gpm integer, xpm integer, net_worth integer, last_hits integer,
    lane_role integer, level integer, hero_damage integer, tower_damage integer,
    gold10 integer, xp10 integer, lh10 integer,
    primary key (match_id, player_slot)
);
create index if not exists mp_account on match_players(account_id, match_id);
create index if not exists mp_hero on match_players(hero_id, match_id);

create table if not exists teams (
    team_id integer primary key,
    name text, tag text,
    rating real, wins integer, losses integer, last_match_time integer,
    logo_url text, updated integer
);

create table if not exists team_players (
    team_id integer not null,
    account_id integer not null,
    name text,
    games_played integer, wins integer,
    is_current integer,
    fetched integer,
    primary key (team_id, account_id)
);

create table if not exists players (
    account_id integer primary key,
    name text,
    team_id integer,
    updated integer
);

create table if not exists leagues (
    leagueid integer primary key,
    name text, tier text
);

create table if not exists heroes (
    hero_id integer primary key,
    name text, localized_name text, primary_attr text, roles text, updated integer
);

create table if not exists hero_meta (
    hero_id integer primary key,
    pro_pick integer, pro_win integer, pro_ban integer, updated integer
);

create table if not exists rating_snapshots (
    entity text not null,
    id integer not null,
    match_id integer not null,
    ts integer not null,
    rating real not null,
    primary key (entity, id, match_id)
);
create index if not exists rs_lookup on rating_snapshots(entity, id, ts);

create table if not exists ratings_current (
    entity text not null,
    id integer not null,
    rating real not null,
    games integer not null,
    last_ts integer not null,
    primary key (entity, id)
);

create table if not exists model_coefs (
    name text primary key,
    payload text not null,
    trained integer not null
);

create table if not exists predictions (
    id integer primary key autoincrement,
    created integer not null,
    mode text not null,
    team_a integer, team_b integer, name_a text, name_b text,
    bo integer default 1,
    picks text,
    p_model real, p_claude real,
    odds_a real, odds_b real,
    side text, stake real,
    match_id integer, series_id integer,
    result integer, settled integer default 0,
    note text, dossier text, analysis text
);

create table if not exists schedule (
    id integer primary key autoincrement,
    created integer not null,
    team_a integer, team_b integer, name_a text, name_b text,
    bo integer default 3, tier text,
    starts integer,
    odds_a real, odds_b real,
    note text, done integer default 0
);

create table if not exists wf_pred (
    match_id integer primary key,
    ts integer not null,
    p_pre real, p_post real
);

create table if not exists odds_fixtures (
    fixture_id text primary key,
    source text, start_ts integer, name_a text, name_b text, tournament text,
    team_a integer, team_b integer, series_id integer, swapped integer default 0, fetched integer
);

create table if not exists odds_lines (
    fixture_id text not null,
    bookmaker text not null,
    open_a real, open_b real, close_a real, close_b real, moves integer, fetched integer,
    primary key (fixture_id, bookmaker)
);

create table if not exists player_hero_cache (
    account_id integer primary key,
    payload text not null,
    fetched integer not null
);

create table if not exists live_track (
    match_id integer not null,
    ts integer not null,
    game_time integer,
    gold_lead real,
    p real
);
create index if not exists live_track_match on live_track(match_id, game_time);
create table if not exists kv (
    key text primary key,
    value text
);
"""


def connect(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # check_same_thread=False: Predictor живёт в сервере и читает из разных потоков; sqlite3 сериализует вызовы
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma synchronous=normal")
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("pragma table_info(match_players)")}
    for col in ("gold10", "xp10", "lh10"):  # миграция старых баз
        if col not in cols:
            conn.execute("alter table match_players add column %s integer" % col)
    cols = {r[1] for r in conn.execute("pragma table_info(predictions)")}
    for col, typ in (("closing_a", "real"), ("closing_b", "real"), ("p_blend", "real"), ("segment", "text"),
                     ("paper", "integer default 1"), ("policy_bet", "integer"), ("policy_side", "text"),
                     ("policy_stake", "real"), ("policy_reason", "text"), ("tier", "text"), ("confidence", "text"), ("market", "text"), ("map_no", "integer"), ("line", "real"), ("pick", "text"), ("ref_a", "real"), ("ref_b", "real")):
        if col not in cols:
            conn.execute("alter table predictions add column %s %s" % (col, typ))
    cols = {r[1] for r in conn.execute("pragma table_info(schedule)")}
    for col, typ in (("ref_a", "real"), ("ref_b", "real"), ("ref_book", "text"), ("fixture_id", "text"), ("tournament", "text")):
        if col not in cols:
            conn.execute("alter table schedule add column %s %s" % (col, typ))
    conn.commit()
    return conn


def kv_get(conn, key, default=None):
    row = conn.execute("select value from kv where key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(conn, key, value):
    conn.execute("insert or replace into kv(key, value) values (?, ?)", (key, json.dumps(value)))


def counts(conn):
    out = {}
    for t in ("matches", "picks", "match_players", "teams", "team_players", "players", "leagues", "heroes",
              "hero_meta", "rating_snapshots", "predictions"):
        out[t] = conn.execute("select count(*) from %s" % t).fetchone()[0]
    row = conn.execute("select max(start_time) m, max(match_id) i from matches").fetchone()
    out["last_start_time"] = row["m"]
    out["last_match_id"] = row["i"]
    return out
