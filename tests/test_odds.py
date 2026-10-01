import json
import time

from dota2bets import oddsapi, oddsbook, policy, ingame, live
from dota2bets.learn import LogReg


def test_winner_market_and_lines():
    catalog = [{"marketId": 101, "marketName": "Total Maps", "outcomes": [{"outcomeId": 1}, {"outcomeId": 2}]},
               {"marketId": 171, "marketName": "Match Winner", "outcomes": [{"outcomeId": 171}, {"outcomeId": 172}]},
               {"marketId": 300, "marketName": "Map 1 Winner", "outcomes": [{"outcomeId": 301}, {"outcomeId": 302}]}]
    m = oddsapi.winner_market(catalog)
    assert m == ("171", "171", "172")
    hist = {"fixtureId": "x", "bookmakers": {"pinnacle": {"markets": {"171": {"outcomes": {
        "171": {"players": {"0": [{"createdAt": "2026-04-05T10:00:00Z", "price": 1.80}, {"createdAt": "2026-04-05T13:50:00Z", "price": 1.70},
                                  {"createdAt": "2026-04-05T14:30:00Z", "price": 1.20}]}},
        "172": {"players": {"0": [{"createdAt": "2026-04-05T10:00:00Z", "price": 2.05}, {"createdAt": "2026-04-05T13:50:00Z", "price": 2.20}]}}}}}}}}
    lines = oddsapi.winner_lines(hist, m)
    start = oddsapi.parse_ts("2026-04-05T14:00:00Z")
    assert oddsapi.open_close(lines["pinnacle"]["a"], start) == (1.80, 1.70)     # снимок после начала матча не считается закрытием
    assert oddsapi.open_close(lines["pinnacle"]["b"], start) == (2.05, 2.20)
    cur = {"bookmakerOdds": {"bet365": {"markets": {"171": {"outcomes": {"171": {"players": {"0": {"price": 1.5}}},
                                                                         "172": {"players": {"0": {"price": 2.6}}}}}}}}}
    assert oddsapi.winner_lines(cur, m)["bet365"]["a"][0][1] == 1.5
    assert oddsapi.winner_lines(cur, None) == {}


def test_norm_names():
    assert oddsbook.norm("Team Spirit") == "spirit"
    assert oddsbook.norm("BetBoom Team") == oddsbook.norm("BETBOOM TEAM") == "betboom"
    assert oddsbook.norm("Natus Vincere") == "natusvincere"
    assert oddsbook.norm("Level UP esports") == "levelup"


def make_db(tmp_path):
    from dota2bets import db
    conn = db.connect(str(tmp_path / "o.sqlite"))
    now = int(time.time())
    conn.execute("insert into teams(team_id, name, tag) values (1, 'Team Alpha', 'A'), (2, 'Beta Gaming', 'B')")
    mid = 0
    series = []
    for s in range(40):
        start = now - (60 - s) * 86400
        a_wins = s % 3 != 0                      # Alpha выигрывает две серии из трёх
        maps = [(True, a_wins), (False, a_wins)]  # 2-0, в первой карте Alpha на Radiant
        for j, (a_rad, a_won) in enumerate(maps):
            mid += 1
            rad, dire = (1, 2) if a_rad else (2, 1)
            conn.execute("insert into matches(match_id, start_time, duration, radiant_win, tier, radiant_team_id, dire_team_id, series_id, series_type) "
                         "values (?,?,?,?,?,?,?,?,?)", (mid, start + j * 3600, 2000, 1 if a_won == a_rad else 0, "professional", rad, dire, 1000 + s, 1))
            if j == 0:
                conn.execute("insert into wf_pred(match_id, ts, p_pre, p_post) values (?,?,?,?)", (mid, start, 0.64, 0.66))
        series.append((start, a_wins))
    conn.commit()
    return conn, series


def test_csv_import_and_backtest(tmp_path):
    conn, series = make_db(tmp_path)
    path = tmp_path / "odds.csv"
    with open(path, "w", encoding="utf-8") as f:
        f.write("date,team_a,team_b,odds_a,odds_b,close_a,close_b,bookmaker\n")
        for start, _ in series:
            f.write("%s,Team Alpha,Beta,1.95,1.85,1.90,1.90,pinnacle\n" % time.strftime("%Y-%m-%d %H:%M", time.gmtime(start)))
        f.write("2026-01-01,Неизвестные,Beta,1.5,2.5\n")
    n, bad = oddsbook.import_csv(conn, str(path))
    assert n == 40 and len(bad) == 1
    rep = oddsbook.backtest(conn)
    assert rep["n"] == 40 and rep["stats"]["usable"] == 40
    assert set(rep["sources"]) == {"линия на закрытии", "линия на открытии", "модель"}
    # линия ставит 50/50, Alpha выигрывает 2 из 3: модель с p≈0.69 на серию должна быть точнее линии
    assert rep["sources"]["модель"]["logloss"] < rep["sources"]["линия на закрытии"]["logloss"]
    pol = rep["policy"]["по открытию"]
    assert pol["bets"] > 0 and pol["roi"] > 0 and pol["clv"] is not None
    assert "Проверка на кэфах" in oddsbook.describe(rep)
    assert "нет" in oddsbook.describe({"n": 0, "stats": {}})


def test_reference_line_policy():
    s = dict(policy.DEFAULTS)
    # эталон 1.90/1.90 (50 %), модель 0.70 → смешанная 0.56; ваш букмекер даёт 2.10 на A — ставка
    d = policy.decide(0.70, 2.10, 1.72, 3, s, ref_a=1.90, ref_b=1.90)
    assert abs(d["p_blend"] - 0.56) < 1e-9 and d["bet"] == 1 and d["side"] == "a" and d["ev"] > 0
    # та же линия у вашего букмекера занижена — ожидание отрицательное
    d = policy.decide(0.70, 1.60, 2.30, 3, s, ref_a=1.90, ref_b=1.90)
    assert d["bet"] == 0
    assert policy.price_needed(0.56, 0.5, s) > 1.0 / 0.56


def test_ingame_interpolation():
    m = LogReg(["pregame", "gold_k"], l2=1.0)
    xs, ys = [], []
    import random
    rnd = random.Random(5)
    for _ in range(3000):
        g = rnd.gauss(0, 6)
        z = 0.3 * g
        xs.append([0.0, g])
        ys.append(1 if rnd.random() < 1 / (1 + 2.718281828 ** (-z)) else 0)
    m.fit(xs, ys)
    b = {"models": {10: m, 20: m}, "report": {}}
    assert ingame.predict(b, 0.5, 0, 5000) == 0.5
    p10 = ingame.predict(b, 0.5, 10, 5000)
    assert p10 > 0.7
    assert abs(ingame.predict(b, 0.5, 15, 5000) - p10) < 1e-9
    assert 0.5 < ingame.predict(b, 0.5, 5, 5000) < p10            # до первой модели — между предматчевой и моделью
    assert ingame.predict(b, 0.5, 60, -5000) < 0.3
    assert len(ingame.table(b, 0.5, 20)) == 9


def test_steam_parse():
    g = {"match_id": 7, "league_id": 1, "series_type": 1, "radiant_series_wins": 1, "dire_series_wins": 0, "spectators": 100,
         "radiant_team": {"team_id": 11, "team_name": "A"}, "dire_team": {"team_id": 22, "team_name": "B"},
         "players": [{"account_id": 1, "team": 0, "hero_id": 0}, {"account_id": 2, "team": 1, "hero_id": 0}, {"account_id": 3, "team": 2}],
         "scoreboard": {"duration": 600.5, "radiant": {"score": 5, "picks": [{"hero_id": 8}], "bans": [{"hero_id": 9}],
                                                       "players": [{"account_id": 1, "hero_id": 8, "net_worth": 5000}]},
                        "dire": {"score": 3, "picks": [{"hero_id": 17}], "players": [{"account_id": 2, "hero_id": 17, "net_worth": 4000}]}}}
    r = live.parse_steam_game(g)
    assert r["radiant"] == [(1, 8)] and r["dire"] == [(2, 17)] and r["gold_lead"] == 1000
    assert r["radiant_series_wins"] == 1 and r["bans_radiant"] == [9] and not r["drafting"]
    assert live.parse_steam_game({"radiant_team": {}, "dire_team": {}}) is None


def test_load_models_ignores_foreign_payloads(tmp_path):
    """Запись ingame в model_coefs не должна ломать загрузку моделей прогноза."""
    from dota2bets import db
    from dota2bets.backtest import load_models
    conn = db.connect(str(tmp_path / "m.sqlite"))
    m = LogReg(["a"], l2=1.0).fit([[0.0], [1.0], [2.0], [3.0]], [0, 0, 1, 1])
    conn.execute("insert into model_coefs(name, payload, trained) values ('pre', ?, 0)", (json.dumps(m.to_dict()),))
    conn.execute("insert into model_coefs(name, payload, trained) values ('ingame', ?, 0)", (json.dumps({"models": {}, "report": {}}),))
    conn.execute("insert into model_coefs(name, payload, trained) values ('maps', ?, 0)", (json.dumps({"k3": 0.85}),))
    out = load_models(conn)
    assert "pre" in out and "ingame" not in out and out["maps"]["k3"] == 0.85
