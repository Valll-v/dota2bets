from dota2bets.policy import blend, decide, segments, DEFAULTS


def test_blend_and_decide():
    s = dict(DEFAULTS)
    # линия 1.8/2.0 → implied ~0.526/0.474; модель 0.70 → смешанная 0.7*0.526+0.3*0.70 ≈ 0.578, edge ≈ 5.2 п.п.
    pb = blend(0.70, 1.8, 2.0, 0.7)
    assert 0.57 < pb < 0.59
    d = decide(0.70, 1.8, 2.0, 3, s)
    assert d["bet"] == 1 and d["side"] == "a" and 0 < d["stake_frac"] <= s["max_stake"]
    # bo1 — без ставки
    assert decide(0.70, 1.8, 2.0, 1, s)["bet"] == 0
    # устаревший состав — без ставки
    assert decide(0.70, 1.8, 2.0, 3, s, roster_stale=True)["bet"] == 0
    # низкая уверенность Claude — без ставки
    assert decide(0.70, 1.8, 2.0, 3, s, confidence="низкая")["bet"] == 0
    # модель согласна с линией — edge мал
    d = decide(0.53, 1.8, 2.0, 3, s)
    assert d["bet"] == 0 and "ниже порога" in d["reason"]
    # без кэфов
    assert decide(0.7, None, None, 3, s)["reason"] == "нет кэфов"


def test_segments():
    t = segments("post", 3, "premium", True, 0.05)
    assert t["edge"] == "edge 4-7" and t["roster"] == "смена состава" and t["bo"] == "bo3"
    assert segments("pre", 1, None, False, None)["edge"] == "без кэфов"


def test_flats_and_min_odds():
    from dota2bets.policy import flats, min_odds, flat_word
    s = dict(DEFAULTS)
    assert flats(0.013, s) == 1.5 and flats(0.004, s) == 0.5 and flats(0.05, s) == 2.0 and flats(0, s) == 0.0
    assert min_odds(0.6, s) > 2.0          # чтобы ставить, линия должна давать заметно меньше нашей p
    assert min_odds(0.1, s) is None
    assert flat_word(1) == "1 флет" and flat_word(2) == "2 флета" and flat_word(1.5) == "1,5 флета"


def test_schedule_parse():
    from dota2bets.schedule import parse_line
    d = parse_line("Team Liquid vs Yakult Brothers bo3 1.25 3.80 premium")
    assert d == {"team_a": "Team Liquid", "team_b": "Yakult Brothers", "bo": 3, "tier": "premium", "odds_a": 1.25, "odds_b": 3.8}
    d = parse_line("1w - Natus Vincere")
    assert d["team_a"] == "1w" and d["team_b"] == "Natus Vincere" and d["bo"] == 3 and d["odds_a"] is None
    d = parse_line("BetBoom Team против OG bo5")
    assert d["bo"] == 5 and d["team_b"] == "OG"
    assert parse_line("# комментарий") is None and parse_line("") is None


def test_markets_spec_and_verdict():
    from dota2bets.markets import parse_spec, half_line
    from dota2bets.policy import market_verdict
    assert parse_spec("kills:48.5:1.85,1.90") == ("kills", 48.5, (1.85, 1.9))
    assert parse_spec("maps::2.10,1.70") == ("maps", None, (2.1, 1.7))
    assert parse_spec("dur:38.5") == ("dur", 38.5, None)
    assert half_line(50.4) == 50.5 and half_line(51.9) == 51.5
    s = dict(DEFAULTS)
    v = market_verdict(0.70, "больше 48.5", "меньше 48.5", 1.90, 1.90, s, "высокая", [], "Тотал килов")
    assert v["bet"] == 1 and v["side"] == "a" and "больше 48.5" in v["recommendation"]
    v = market_verdict(0.52, "больше 48.5", "меньше 48.5", 1.90, 1.90, s, "высокая", [], "Тотал килов")
    assert v["bet"] == 0
    v = market_verdict(0.70, "да", "нет", 1.90, 1.90, s, "низкая", ["состав устарел"], "Тотал")
    assert v["bet"] == 0 and "надёжность" in v["recommendation"]
    v = market_verdict(0.70, "да", "нет", None, None, s, "высокая", [], "Тотал")
    assert v["min_odds_yes"] and "при кэфе от" in v["recommendation"]


def test_linreg_empirical_over():
    import random
    from dota2bets.learn import LinReg
    rnd = random.Random(2)
    xs = [[rnd.gauss(0, 1)] for _ in range(4000)]
    ys = [10 + 3 * x[0] + rnd.expovariate(0.5) - 2.0 for x in xs]      # скошенные вправо остатки
    m = LinReg(["x"], l2=0.0).fit(xs, ys)
    assert abs(m.w[1] - 3.0) < 0.15
    mu = m.predict([0.0])
    assert m.prob_over(mu, mu) < 0.47            # медиана ниже среднего: «больше среднего» реже половины
    assert m.prob_over(mu, mu - 50) > 0.99 and m.prob_over(mu, mu + 50) < 0.01
    m2 = LinReg.from_dict(m.to_dict())
    assert abs(m2.prob_over(mu, mu + 1) - m.prob_over(mu, mu + 1)) < 0.01
