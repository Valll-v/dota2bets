import random

from dota2bets.model import State, Counter, run_pass, ALL_FEATURES, PRE_FEATURES
from dota2bets.ratings import Elo, expected, shrink, MEAN

DAY = 86400


def synthetic_matches(n=1200, seed=3):
    """Две сильные команды (1, 2) и две слабые (3, 4); составы фиксированы; герои случайные."""
    rnd = random.Random(seed)
    strength = {1: 1.5, 2: 1.0, 3: -1.0, 4: -1.5}
    lineups = {t: [t * 100 + i for i in range(5)] for t in strength}
    out = []
    ts = 1700000000
    a = b = None
    for i in range(n):
        if i % 3 == 0:  # серия из трёх карт между одной парой
            a, b = rnd.sample(list(strength), 2)
        z = strength[a] - strength[b]
        win = rnd.random() < 1 / (1 + 2.718281828 ** (-z))
        heroes = rnd.sample(range(1, 40), 10)
        out.append({"match_id": i, "start_time": ts, "radiant_win": 1 if win else 0, "tier": "premium",
                    "radiant_team_id": a, "dire_team_id": b, "series_id": i // 3,
                    "players_a": list(zip(lineups[a], heroes[:5])), "players_b": list(zip(lineups[b], heroes[5:]))})
        ts += 6 * 3600
    return out


def test_elo_orders_teams():
    matches = synthetic_matches()
    st = run_pass(matches)
    now = matches[-1]["start_time"] + DAY
    r = {t: st.team_elo.get(t, now) for t in (1, 2, 3, 4)}
    assert r[1] > r[2] > r[3] > r[4]
    p = {t * 100: st.player_elo.get(t * 100, now) for t in (1, 2, 3, 4)}
    assert p[100] > p[200] > p[300] > p[400]


def test_features_have_no_future_leak():
    matches = synthetic_matches(300)
    seen = []

    def collect(m, f):
        seen.append((m["match_id"], dict(f)))

    run_pass(matches, collect)
    # первый матч: всё нейтрально
    assert seen[0][1]["elo"] == 0.0 and seen[0][1]["form"] == 0.0 and seen[0][1]["h2h"] == 0.0
    # все признаки присутствуют для полных матчей
    assert all(k in seen[-1][1] for k in ALL_FEATURES)
    # серия: третья карта серии видит счёт после двух первых
    m3 = [f for mid, f in seen if mid % 3 == 2][5]
    assert m3["series_lead"] in (-2.0, 0.0, 2.0)


def test_counter_decay_and_shrink():
    c = Counter(half=10)
    c.add("k", 0, 1.0)
    g, w = c.read("k", 10 * DAY)
    assert abs(g - 0.5) < 1e-9 and abs(w - 0.5) < 1e-9
    p, g = c.rate("k", 0, prior=5.0)
    assert abs(p - (1 + 2.5) / 6.0) < 1e-9
    p0, _ = c.rate("none", 0, prior=5.0)
    assert p0 == 0.5


def test_shrink_idle():
    assert shrink(1800, 0, 30 * DAY) == 1800
    assert shrink(1800, 0, 400 * DAY) < 1800
    assert shrink(1200, 0, 400 * DAY) > 1200
    assert abs(expected(MEAN, MEAN) - 0.5) < 1e-12


def test_lineup_change_and_roster():
    matches = synthetic_matches(200)
    # подмена состава команды 1 в последнем матче: смена состава должна быть видна
    last = matches[-1]
    last["radiant_team_id"], last["dire_team_id"] = 1, 3
    last["players_a"] = [(999, 5), (998, 6), (100, 7), (101, 8), (102, 9)]
    last["players_b"] = [(300 + i, 10 + i) for i in range(5)]
    seen = []
    run_pass(matches, lambda m, f: seen.append(f))
    assert abs(seen[-1]["lineup_change"] - 0.4) < 1e-9
    assert set(PRE_FEATURES) <= set(seen[-1])


def test_canonical_team_follows_roster():
    matches = synthetic_matches(300)
    # с 200-го матча пятёрка команды 1 играет под новым id 99
    for m in matches[200:]:
        for key in ("radiant_team_id", "dire_team_id"):
            if m[key] == 1:
                m[key] = 99
    seen = []
    st = run_pass(matches, lambda m, f: seen.append((m, f)))
    assert st.canon.get(99) == 1
    # история и h2h новой команды не обнулились: elo-признак в первом матче под новым id не нулевой
    first = next(f for m, f in seen if 99 in (m["radiant_team_id"], m["dire_team_id"]))
    assert first["elo"] != 0.0
    assert first["h2h"] != 0.0 or first["form"] != 0.0


def test_elo_get_is_pure():
    e = Elo()
    e.update("t", 300.0, 0)                       # рейтинг 1800, последний матч в момент 0
    now = 400 * DAY
    first = e.get("t", now)
    assert first < 1800
    assert e.get("t", now) == first                # повторное чтение не меняет рейтинг
    assert e.rating["t"] == 1800.0
    e.update("t", 10.0, now)                       # усадка применяется один раз, при обновлении
    assert abs(e.rating["t"] - (first + 10.0)) < 1e-9


def test_player_insights(tmp_path, monkeypatch):
    """Лидер, слабое звено и новичок определяются по базе; разрез не падает на пустых данных."""
    import time
    from dota2bets import db, players
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    conn = db.connect(str(tmp_path / "t.sqlite"))
    matches = synthetic_matches(240)
    now = matches[-1]["start_time"] + 3600
    for m in matches:
        conn.execute("insert into matches(match_id, start_time, radiant_win, tier, radiant_team_id, dire_team_id, series_id, series_type, duration, radiant_score, dire_score) "
                     "values (?,?,?,?,?,?,?,?,?,?,?)", (m["match_id"], m["start_time"], m["radiant_win"], "premium", m["radiant_team_id"],
                                                         m["dire_team_id"], m["series_id"], 1, 2100, 30, 25))
        for side, key in ((1, "players_a"), (0, "players_b")):
            for i, (acc, h) in enumerate(m[key]):
                conn.execute("insert into match_players(match_id, player_slot, account_id, hero_id, is_radiant, kills, deaths, assists, gpm) values (?,?,?,?,?,?,?,?,?)",
                             (m["match_id"], i + (0 if side else 128), acc, h, side, 5, 4, 8, 650 - 90 * i))
    conn.commit()
    st = run_pass(matches)

    class P:
        state = st
        player_names = {}
    res = {"lineup_a": [100, 101, 102, 103, 999], "lineup_b": [300, 301, 302, 303, 304]}
    out = players.matchup(P(), conn, res, 1, 3, "Alpha", "Gamma", now)
    a = out["a"]["players"]
    assert len(a) == 5 and sum(1 for p in a if p["core"]) == 3
    newcomer = next(p for p in a if p["id"] == 999)
    assert "новичок" in newcomer["flags"] and newcomer["tenure"] == 0
    assert any(l["kind"] == "key" for l in out["lines"])
    assert any(l["kind"] == "new" for l in out["lines"])
    assert out["duel"] and out["caveat"]
    empty = players.team_insights(P(), conn, 1, [], now, "Alpha")
    assert empty["players"] == [] and empty["lines"] == []


def test_draft_labels():
    from dota2bets.draft import label_pick
    base = {"games": 500, "gpm": 600.0, "kda": 5.0}
    strong = {"games": 20, "wins": 15, "gpm": 690.0, "kda": 8.0, "days_since": 10, "pubs": None}
    assert label_pick(strong, base, True)[0] == "сигнатурный"
    weak = {"games": 12, "wins": 3, "gpm": 560.0, "kda": 3.0, "days_since": 10, "pubs": None}
    assert label_pick(weak, base, True)[0] == "слабый"
    rare = {"games": 2, "wins": 2, "gpm": 700.0, "kda": 9.0, "days_since": 10, "pubs": None}
    assert label_pick(rare, base, True)[0] == "редкий пик"
    # 9 карт, 5 побед, но личные показатели высокие — комфортный, а не нейтральный
    mixed = {"games": 9, "wins": 5, "gpm": 778.0, "kda": 12.9, "days_since": 200, "pubs": None}
    lab, facts = label_pick(mixed, {"games": 1500, "gpm": 663.0, "kda": 6.0}, True)
    assert lab == "комфортный" and "9 карт" in facts and "200 дней" in facts
    none = {"games": 0, "wins": 0, "pubs": {"games": 300, "wins": 180, "last": 0}}
    assert label_pick(none, base, True)[0] == "впервые в про"
    assert label_pick({"games": 0, "wins": 0, "pubs": None}, base, True)[0] == "кот в мешке"
