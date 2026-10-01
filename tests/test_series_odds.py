from dota2bets.series import series_distribution, simulate_bracket
from dota2bets.odds import implied, kelly, evaluate


def test_bo3_matches_formula():
    p = 0.6
    d = series_distribution(p, 3, k3=1.0)
    assert abs(sum(d["scores"].values()) - 1.0) < 1e-12
    # P(A выигрывает bo3) = p² + 2 p² (1−p)
    assert abs(d["p_a"] - (p * p + 2 * p * p * (1 - p))) < 1e-12
    assert abs(d["scores"][(2, 0)] - p * p) < 1e-12
    assert abs(d["scores"][(2, 1)] - 2 * p * p * (1 - p)) < 1e-12
    assert d["p_draw"] == 0.0


def test_bo2_has_draw_and_lead():
    d = series_distribution(0.5, 2)
    assert abs(d["p_draw"] - 0.5) < 1e-12
    d = series_distribution(0.5, 3, lead_a=1)
    assert abs(d["p_a"] - 0.75) < 1e-12


def test_bo5_and_bo1():
    d = series_distribution(0.7, 1)
    assert abs(d["p_a"] - 0.7) < 1e-12
    d = series_distribution(0.5, 5)
    assert abs(d["p_a"] - 0.5) < 1e-12
    assert set(d["scores"]) == {(3, 0), (3, 1), (3, 2), (0, 3), (1, 3), (2, 3)}


def test_bracket():
    bracket = {"teams": ["A", "B", "C", "D"],
               "rounds": [{"name": "semi", "bo": 3, "pairs": [[0, 1], [2, 3]]},
                          {"name": "final", "bo": 5, "pairs": [["W0-0", "W0-1"]]}]}
    strength = {"A": 0.9, "B": 0.5, "C": 0.5, "D": 0.1}

    def p_map(a, b):
        return strength[a] / (strength[a] + strength[b])

    r = simulate_bracket(bracket, p_map, runs=4000, seed=7)
    assert abs(sum(r["champion"].values()) - 1.0) < 1e-9
    assert r["champion"]["A"] > r["champion"]["B"] > r["champion"]["D"]
    assert r["reached"]["semi"]["A"] == 1.0


def test_odds():
    ia, ib, margin = implied(1.5, 2.8)
    assert abs(ia + ib - 1.0) < 1e-12 and margin > 0
    assert kelly(0.6, 2.0) > 0 and kelly(0.4, 2.0) == 0.0
    ev = evaluate(0.6, 2.0, 2.0)
    assert ev["edge_a"] > 0 and ev["ev_a"] > 0 and ev["ev_b"] < 0


def test_bo3_third_map_correction():
    d0 = series_distribution(0.5, 3, k3=1.0)
    d = series_distribution(0.5, 3)            # поправка по умолчанию
    p3_0 = d0["scores"][(2, 1)] + d0["scores"][(1, 2)]
    p3 = d["scores"][(2, 1)] + d["scores"][(1, 2)]
    assert abs(sum(d["scores"].values()) - 1.0) < 1e-12
    assert abs(p3 - 0.85 * p3_0) < 1e-12
    assert abs(d["p_a"] - 0.5) < 1e-12
    # идущая серия не правится
    d1 = series_distribution(0.5, 3, lead_a=1)
    assert abs(d1["p_a"] - 0.75) < 1e-12
