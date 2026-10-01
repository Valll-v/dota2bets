"""Коэффициенты: десятичные кэфы → вероятности без маржи, edge, EV, дробный Келли."""

KELLY_FRACTION = 0.25


def implied(odds_a, odds_b):
    """Вероятности с пропорциональным снятием маржи."""
    ia, ib = 1.0 / odds_a, 1.0 / odds_b
    s = ia + ib
    return ia / s, ib / s, s - 1.0


def kelly(p, odds, fraction=KELLY_FRACTION):
    b = odds - 1.0
    if b <= 0:
        return 0.0
    f = (p * b - (1 - p)) / b
    return max(0.0, f * fraction)


def evaluate(p_a, odds_a, odds_b):
    ia, ib, margin = implied(odds_a, odds_b)
    p_b = 1.0 - p_a
    return {
        "implied_a": ia, "implied_b": ib, "margin": margin,
        "edge_a": p_a - ia, "edge_b": p_b - ib,
        "ev_a": p_a * odds_a - 1.0, "ev_b": p_b * odds_b - 1.0,
        "kelly_a": kelly(p_a, odds_a), "kelly_b": kelly(p_b, odds_b),
    }
