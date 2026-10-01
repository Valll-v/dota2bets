"""Серии и сетки: распределение счёта при независимых картах, Монте-Карло по сетке турнира."""

import random
from functools import lru_cache


K3_DEFAULT = 0.85   # bo3 доходит до третьей карты реже, чем при независимых картах (бэктест: 39 % против 46 %)


def series_distribution(p, bo, lead_a=0, lead_b=0, momentum=0.0, k3=None):
    """Распределение счёта серии bo{bo} при вероятности p победы A на карте.
    lead_*: уже выигранные карты. momentum: прибавка к p после выигранной карты (по умолчанию 0).
    bo=2 — серия из двух карт (возможна ничья 1-1)."""
    need = bo // 2 + 1 if bo != 2 else 2
    scores = {}

    def walk(wa, wb, prob, pa):
        if bo == 2:
            if wa + wb == 2:
                scores[(wa, wb)] = scores.get((wa, wb), 0.0) + prob
                return
        elif wa == need or wb == need:
            scores[(wa, wb)] = scores.get((wa, wb), 0.0) + prob
            return
        walk(wa + 1, wb, prob * pa, min(0.99, pa + momentum))
        walk(wa, wb + 1, prob * (1 - pa), max(0.01, pa - momentum))

    walk(lead_a, lead_b, 1.0, p)
    # bo3 с нуля: вероятность трёх карт умножается на k3, разница уходит в 2-0 и 0-2 пропорционально
    if bo == 3 and lead_a == 0 and lead_b == 0 and momentum == 0.0:
        k = K3_DEFAULT if k3 is None else k3
        p3 = scores.get((2, 1), 0.0) + scores.get((1, 2), 0.0)
        p2 = scores.get((2, 0), 0.0) + scores.get((0, 2), 0.0)
        if p3 > 0 and p2 > 0 and k != 1.0:
            grow = (1.0 - k * p3) / p2
            for key in list(scores):
                scores[key] *= k if sum(key) == 3 else grow
    p_a = sum(v for (wa, wb), v in scores.items() if wa > wb)
    p_draw = sum(v for (wa, wb), v in scores.items() if wa == wb)
    return {"p_a": p_a, "p_b": 1.0 - p_a - p_draw, "p_draw": p_draw,
            "scores": dict(sorted(scores.items(), key=lambda kv: -kv[1]))}


def simulate_bracket(bracket, p_map, runs=20000, seed=None):
    """Монте-Карло по сетке.

    bracket: {"teams": [id, ...], "rounds": [{"name": "...", "bo": 3, "pairs": [[slot, slot], ...]}, ...]}
    Слоты — индексы команд в "teams" для первого раунда; в следующих раундах — "W<round>-<pair>" (победитель)
    или "L<round>-<pair>" (проигравший). Последний раунд — финал.
    p_map(a, b) → вероятность победы a на карте.

    Возвращает {"champion": {team: p}, "reached": {round_name: {team: p}}}.
    """
    rnd = random.Random(seed)
    teams = bracket["teams"]
    rounds = bracket["rounds"]
    champion = {t: 0 for t in teams}
    reached = {r["name"]: {t: 0 for t in teams} for r in rounds}
    cache = {}

    def prob(a, b):
        if (a, b) not in cache:
            cache[(a, b)] = p_map(a, b)
        return cache[(a, b)]

    def play(a, b, bo):
        need = bo // 2 + 1
        wa = wb = 0
        pa = prob(a, b)
        while wa < need and wb < need:
            if rnd.random() < pa:
                wa += 1
            else:
                wb += 1
        return (a, b) if wa > wb else (b, a)

    for _ in range(runs):
        results = {}
        last_winner = None
        for ri, r in enumerate(rounds):
            for pi, (sa, sb) in enumerate(r["pairs"]):
                a, b = resolve_slot(sa, teams, results), resolve_slot(sb, teams, results)
                reached[r["name"]][a] += 1
                reached[r["name"]][b] += 1
                w, l = play(a, b, r.get("bo", 3))
                results["W%d-%d" % (ri, pi)] = w
                results["L%d-%d" % (ri, pi)] = l
                last_winner = w
        champion[last_winner] += 1
    return {"champion": {t: c / runs for t, c in champion.items()},
            "reached": {k: {t: c / runs for t, c in v.items()} for k, v in reached.items()}}


def resolve_slot(slot, teams, results):
    if isinstance(slot, int):
        return teams[slot]
    return results[slot]
