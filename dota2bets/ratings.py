"""Elo команд и игроков. Пересчёт хронологический, снимки «до матча» — в rating_snapshots.

Две независимые системы:
- team: ожидание по рейтингам команд, K по тиру лиги, усадка к среднему при простое.
- player: ожидание по среднему рейтингу фактической пятёрки (roster), каждый игрок получает то же обновление.
  Так стендины и переходы учитываются автоматически; roster-снимок хранится как entity='roster' по team_id.
"""

import math
import time

from .db import connect

MEAN = 1500.0
K_TIER = {"premium": 32.0, "professional": 24.0}
K_DEFAULT = 12.0
K_PLAYER = 40.0   # подобрано по бэктесту (scripts/sweep.py, 2026-09-29): 20 → 40 даёт −0.0008 log loss
IDLE_DAYS = 60            # после такого простоя рейтинг тянется к среднему
IDLE_DECAY = 0.85         # доля отклонения, которая остаётся за каждые 90 дней простоя сверх IDLE_DAYS
DAY = 86400


def expected(ra, rb):
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))


def shrink(rating, last_ts, now_ts):
    if last_ts is None:
        return rating
    idle = (now_ts - last_ts) / DAY - IDLE_DAYS
    if idle <= 0:
        return rating
    return MEAN + (rating - MEAN) * (IDLE_DECAY ** (idle / 90.0))


class Elo:
    def __init__(self):
        self.rating = {}
        self.last = {}
        self.games = {}

    def get(self, key, now_ts):
        """Рейтинг с усадкой за простой. Чтение без побочных эффектов: раньше усаженное значение
        записывалось обратно, и простаивающая команда затухала заново при каждом чтении."""
        return shrink(self.rating.get(key, MEAN), self.last.get(key), now_ts)

    def update(self, key, delta, now_ts):
        self.rating[key] = self.get(key, now_ts) + delta
        self.last[key] = now_ts
        self.games[key] = self.games.get(key, 0) + 1


def compute(conn=None, log=None):
    """Полный пересчёт. Возвращает число обработанных матчей."""
    conn = conn or connect()
    teams, players = Elo(), Elo()
    rows = conn.execute(
        "select match_id, start_time, radiant_win, tier, radiant_team_id, dire_team_id from matches "
        "where radiant_team_id is not null and dire_team_id is not null and radiant_win is not null "
        "order by start_time, match_id").fetchall()
    lineup = {}
    for r in conn.execute("select match_id, is_radiant, account_id from match_players where account_id is not null"):
        lineup.setdefault((r[0], r[1]), []).append(r[2])

    snapshots = []
    n = 0
    for m in rows:
        ts, mid = m["start_time"], m["match_id"]
        a, b = m["radiant_team_id"], m["dire_team_id"]
        k = K_TIER.get(m["tier"], K_DEFAULT)
        s_a = 1.0 if m["radiant_win"] else 0.0

        ra, rb = teams.get(a, ts), teams.get(b, ts)
        snapshots.append(("team", a, mid, ts, ra))
        snapshots.append(("team", b, mid, ts, rb))
        e = expected(ra, rb)
        teams.update(a, k * (s_a - e), ts)
        teams.update(b, -k * (s_a - e), ts)

        pa = lineup.get((mid, 1), [])
        pb = lineup.get((mid, 0), [])
        if len(pa) == 5 and len(pb) == 5:
            ras = [players.get(p, ts) for p in pa]
            rbs = [players.get(p, ts) for p in pb]
            roster_a, roster_b = sum(ras) / 5.0, sum(rbs) / 5.0
            snapshots.append(("roster", a, mid, ts, roster_a))
            snapshots.append(("roster", b, mid, ts, roster_b))
            for p, r in zip(pa, ras):
                snapshots.append(("player", p, mid, ts, r))
            for p, r in zip(pb, rbs):
                snapshots.append(("player", p, mid, ts, r))
            ep = expected(roster_a, roster_b)
            for p in pa:
                players.update(p, K_PLAYER * (s_a - ep), ts)
            for p in pb:
                players.update(p, -K_PLAYER * (s_a - ep), ts)
        n += 1

    conn.execute("delete from rating_snapshots")
    conn.executemany("insert into rating_snapshots(entity, id, match_id, ts, rating) values (?,?,?,?,?)", snapshots)
    conn.execute("delete from ratings_current")
    now = int(time.time())
    conn.executemany(
        "insert into ratings_current(entity, id, rating, games, last_ts) values (?,?,?,?,?)",
        [("team", k, teams.get(k, now), teams.games.get(k, 0), teams.last[k]) for k in teams.last]
        + [("player", k, players.get(k, now), players.games.get(k, 0), players.last[k]) for k in players.last])
    conn.commit()
    if log:
        log("elo: %d матчей, %d команд, %d игроков, %d снимков" % (n, len(teams.last), len(players.last), len(snapshots)))
    return n


def current(conn, entity, key, now_ts=None):
    """Текущий рейтинг с усадкой за простой; (rating, games) или (MEAN, 0)."""
    row = conn.execute("select rating, games, last_ts from ratings_current where entity=? and id=?",
                       (entity, key)).fetchone()
    if not row:
        return MEAN, 0
    return shrink(row["rating"], row["last_ts"], now_ts or int(time.time())), row["games"]


def before(conn, entity, key, match_id):
    """Рейтинг до конкретного матча (из снимка); None, если снимка нет."""
    row = conn.execute("select rating from rating_snapshots where entity=? and id=? and match_id=?",
                       (entity, key, match_id)).fetchone()
    return row["rating"] if row else None


def roster_rating(conn, account_ids, now_ts=None):
    """Средний текущий рейтинг пятёрки и список (account_id, rating, games)."""
    detail = []
    for p in account_ids:
        r, g = current(conn, "player", p, now_ts)
        detail.append((p, r, g))
    mean = sum(d[1] for d in detail) / len(detail) if detail else MEAN
    return mean, detail


def win_prob_from_elo(ra, rb):
    return expected(ra, rb)


def elo_to_logit(ra, rb):
    return (ra - rb) * math.log(10) / 400.0
