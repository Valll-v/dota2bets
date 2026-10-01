"""Стратегия ставок: смешанная вероятность, правило входа, размер, сегменты.

Смешанная p = w·implied(букмекер, без маржи) + (1 − w)·p_model. Линия несёт информацию, которой нет у модели;
модель добавляет своё. Ставка только там, где смешанная p даёт edge не меньше порога, формат не bo1, состав не
устарел, а вердикт Claude (если есть) не «низкая» уверенность. Размер — Келли на четверть с потолком доли банка.
"""

from .db import kv_get, kv_set
from .odds import implied, kelly

DEFAULTS = {"bankroll": 1000.0, "blend": 0.7, "min_edge": 0.04, "max_stake": 0.02, "min_bo": 2, "kelly": 0.25}


def settings(conn):
    s = dict(DEFAULTS)
    s.update(kv_get(conn, "policy", {}) or {})
    return s


def configure(conn, **kw):
    s = settings(conn)
    for k, v in kw.items():
        if v is not None and k in DEFAULTS:
            s[k] = type(DEFAULTS[k])(v)
    kv_set(conn, "policy", s)
    conn.commit()
    return s


def blend(p_model, odds_a, odds_b, w):
    """Смешанная вероятность A; без кэфов — p_model."""
    if not (odds_a and odds_b):
        return p_model
    ia, ib, _ = implied(odds_a, odds_b)
    return w * ia + (1.0 - w) * p_model


def decide(p_model, odds_a, odds_b, bo, s, roster_stale=False, confidence=None, market="series", ref_a=None,
           ref_b=None):
    """odds_* — цена, по которой можно поставить (ваш букмекер). ref_* — эталонная линия (например Pinnacle):
    если задана, смешивание идёт с ней, а запас считается против вашей цены."""
    """Решение политики: dict(bet, side, p_blend, edge, stake_frac, reason)."""
    out = {"bet": 0, "side": None, "p_blend": None, "edge": None, "stake_frac": 0.0, "reason": ""}
    if not (odds_a and odds_b):
        out["reason"] = "нет кэфов"
        return out
    ia, ib, _ = implied(odds_a, odds_b)
    if ref_a and ref_b:
        pb = blend(p_model, ref_a, ref_b, s["blend"])
    else:
        pb = blend(p_model, odds_a, odds_b, s["blend"])
    out["p_blend"] = pb
    edge_a, edge_b = pb - ia, (1 - pb) - ib
    side = "a" if edge_a >= edge_b else "b"
    edge = max(edge_a, edge_b)
    out["side"], out["edge"] = side, edge
    ev = (pb * odds_a - 1.0) if side == "a" else ((1 - pb) * odds_b - 1.0)
    out["ev"] = ev
    if market != "map" and bo < s["min_bo"]:
        out["reason"] = "bo%d: дисперсия слишком велика" % bo
    elif roster_stale:
        out["reason"] = "состав устарел, модель слепа"
    elif confidence == "низкая":
        out["reason"] = "Claude: уверенность низкая"
    elif edge < s["min_edge"]:
        out["reason"] = "edge %.1f п.п. ниже порога %.0f" % (100 * edge, 100 * s["min_edge"])
    elif ev <= 0:
        out["reason"] = "ожидание отрицательное (%.1f%%): маржа съедает запас" % (100 * ev)
    else:
        p_side = pb if side == "a" else 1 - pb
        odds = odds_a if side == "a" else odds_b
        frac = min(kelly(p_side, odds, s["kelly"]), s["max_stake"])
        out.update({"bet": 1, "stake_frac": frac, "reason": "edge %.1f п.п., ожидание %+.1f%%, Келли/4 %.1f%% банка" % (
            100 * edge, 100 * ev, 100 * frac)})
    return out


def segments(mode, bo, tier, roster_change, edge):
    """Теги сегмента для разреза отчёта."""
    tags = {"mode": mode, "bo": "bo%d" % bo, "tier": tier or "?",
            "roster": "смена состава" if roster_change else "стабильный состав"}
    if edge is None:
        tags["edge"] = "без кэфов"
    else:
        tags["edge"] = "edge<2" if edge < 0.02 else ("edge 2-4" if edge < 0.04 else ("edge 4-7" if edge < 0.07 else "edge 7+"))
    return tags


# ---------------------------------------------------------------- вердикт и флеты

DEFAULTS["flat"] = 0.01        # один флет — доля банка
TYPICAL_MARGIN = 0.06


def flats(frac, s):
    """Размер ставки во флетах: шаг 0.5, от 0.5 до max_stake/flat."""
    if frac <= 0:
        return 0.0
    top = s["max_stake"] / s["flat"]
    return max(0.5, min(top, round(frac / s["flat"] * 2) / 2.0))


def min_odds(p, s):
    """Минимальный кэф букмекера, при котором политика ставит на исход с нашей вероятностью p.
    edge смешанной p = (1 − w)·(p − implied) ≥ min_edge → implied ≤ p − min_edge/(1 − w)."""
    w = s["blend"]
    if w >= 1:
        return None
    imp = p - s["min_edge"] / (1.0 - w)
    if imp <= 0.02:
        return None
    return round(1.0 / (imp * (1.0 + TYPICAL_MARGIN)), 2)


def price_needed(p_blend, p_ref, s):
    """Минимальный кэф вашего букмекера при известной эталонной линии: запас min_edge к цене и положительное ожидание."""
    need = p_blend - s["min_edge"]
    if need <= 0.03:
        return None
    return round(max(1.0 / (need * (1.0 + TYPICAL_MARGIN / 2.0)), 1.0 / p_blend + 0.01), 2)


def reliability(res):
    """Надёжность данных под прогнозом: (уровень, причины)."""
    reasons = []
    level = 2
    for label, side, lu in (("A", res["detail"]["a"], res["lineup_a"]), ("B", res["detail"]["b"], res["lineup_b"])):
        import math
        days = math.expm1(side["rest"])
        if days > 30:
            reasons.append("%s не играла %.0f дн" % (label, days))
            level = min(level, 1)
        if side["stability"] < 3:
            reasons.append("у %s пятёрка почти не играла вместе" % label)
            level = min(level, 1)
        if len(lu) < 5:
            reasons.append("у %s неполный состав в базе" % label)
            level = 0
    for w in res.get("warnings", []):
        if "играли за другие команды" in w:
            reasons.append("состав %s устарел" % w[:1])
            level = 0
    return ("низкая", "средняя", "высокая")[level], reasons


def strength_word(p):
    if p >= 0.85:
        return "явный фаворит"
    if p >= 0.70:
        return "уверенный фаворит"
    if p >= 0.58:
        return "фаворит"
    return "небольшой перевес"


def verdict(res, name_a, name_b, p_bet, scores, bo, odds_a, odds_b, s, confidence=None, market="series",
            map_no=None, ref_a=None, ref_b=None, ref_book=None):
    """Текстовый вердикт и рекомендация во флетах.
    p_bet — вероятность A на рынке ставки (серия при bo>1), scores — [(счёт, p)] распределения серии."""
    rel, reasons = reliability(res)
    stale = any("играли за другие команды" in w for w in res.get("warnings", []))
    fav, p_fav = (name_a, p_bet) if p_bet >= 0.5 else (name_b, 1 - p_bet)
    is_map = market == "map"
    market = ("карта %d" % map_no if map_no else "карта") if is_map else ("серия bo%d" % bo if bo > 1 else "карта")
    if is_map:
        scores = []
        draft = "после драфта" if res.get("mode") == "post" else "до драфта"
        side = {"radiant": ", %s на Radiant" % name_a, "dire": ", %s на Dire" % name_a}.get(res.get("side"), ", сторона неизвестна")
    head = "%s — %s, %.0f%% (%s)" % (fav, strength_word(p_fav), 100 * p_fav, market)
    if abs(p_bet - 0.5) < 0.04:
        head = "Равный матч: %s %.0f%% / %s %.0f%% (%s)" % (name_a, 100 * p_bet, name_b, 100 * (1 - p_bet), market)
    if scores:
        top = scores[0]
        head += "; вероятнейший счёт %s (%.0f%%)" % (top[0], 100 * top[1])
    if is_map:
        head += " — оценка %s%s" % (draft, side)
    d = decide(p_bet, odds_a, odds_b, bo, s, roster_stale=stale, confidence=confidence,
               market="map" if is_map else "series", ref_a=ref_a, ref_b=ref_b)
    fair = None
    if ref_a and ref_b:
        ra, rb, rm = implied(ref_a, ref_b)
        fair = {"book": ref_book or "эталон", "odds_a": ref_a, "odds_b": ref_b, "p_a": ra, "p_b": rb, "margin": rm,
                "fair_a": round(1.0 / ra, 2), "fair_b": round(1.0 / rb, 2)}
    out = {"text": head, "reliability": rel, "reliability_reasons": reasons, "favorite": fav, "fair": fair,
           "ev": d.get("ev"),
           "p_blend": d["p_blend"], "edge": d["edge"], "bet": d["bet"], "side": d["side"], "flats": 0.0,
           "stake": None, "min_odds_a": min_odds(p_bet, s), "min_odds_b": min_odds(1 - p_bet, s)}
    if odds_a and odds_b:
        if d["bet"] and rel != "низкая":
            n = flats(d["stake_frac"], s)
            if rel == "средняя":
                n = max(0.5, n - 0.5)     # данные неполные — на полфлета меньше
            side_name = name_a if d["side"] == "a" else name_b
            odds = odds_a if d["side"] == "a" else odds_b
            out.update({"flats": n, "stake": round(n * s["flat"] * s["bankroll"], 2)})
            out["recommendation"] = "СТАВКА: %s на %s (%s) по %.2f — edge %+.1f п.п., ожидание %+.1f%%, смешанная p %.0f%%" % (
                flat_word(n), side_name, market, odds, 100 * d["edge"], 100 * d["ev"],
                100 * (d["p_blend"] if d["side"] == "a" else 1 - d["p_blend"]))
        else:
            out["bet"] = 0
            why = d["reason"] if not d["bet"] else "надёжность данных низкая: " + "; ".join(reasons)
            out["recommendation"] = "ПРОПУСК: %s" % why
    else:
        if not is_map and bo < s["min_bo"]:
            out["recommendation"] = "ПРОПУСК: bo%d, дисперсия слишком велика" % bo
        elif rel == "низкая":
            out["recommendation"] = "ПРОПУСК: надёжность данных низкая (%s)" % "; ".join(reasons)
        else:
            if fair:
                pb = blend(p_bet, ref_a, ref_b, s["blend"])
                out["p_blend"] = pb
                out["min_odds_a"] = price_needed(pb, fair["p_a"], s)
                out["min_odds_b"] = price_needed(1 - pb, fair["p_b"], s)
            parts = []
            if out["min_odds_a"]:
                parts.append("на %s при кэфе от %.2f" % (name_a, out["min_odds_a"]))
            if out["min_odds_b"]:
                parts.append("на %s при кэфе от %.2f" % (name_b, out["min_odds_b"]))
            out["recommendation"] = ("СТАВИТЬ " + ", ".join(parts) + "; иначе пропуск") if parts else "ПРОПУСК: нет исхода с запасом"
    out["market"] = "map" if is_map else "series"
    if is_map and res.get("mode") != "post":
        out["recommendation"] += " | до драфта: после пиков пересчитайте с героями и стороной"
    return out


def market_verdict(p_yes, yes, no, odds_yes, odds_no, s, rel, reasons, title):
    """Рекомендация по двухисходному рынку (тотал, фора): те же смешивание, порог и флеты."""
    out = {"p_blend": None, "edge": None, "bet": 0, "side": None, "flats": 0.0, "stake": None,
           "min_odds_yes": min_odds(p_yes, s), "min_odds_no": min_odds(1 - p_yes, s), "reliability": rel}
    if rel == "низкая":
        out["recommendation"] = "ПРОПУСК: надёжность данных низкая (%s)" % "; ".join(reasons)
        return out
    if odds_yes and odds_no:
        d = decide(p_yes, odds_yes, odds_no, 3, s, market="map")
        out.update({"p_blend": d["p_blend"], "edge": d["edge"], "side": d["side"]})
        if d["bet"]:
            n = flats(d["stake_frac"], s)
            if rel == "средняя":
                n = max(0.5, n - 0.5)
            pick, odds = (yes, odds_yes) if d["side"] == "a" else (no, odds_no)
            out.update({"bet": 1, "flats": n, "stake": round(n * s["flat"] * s["bankroll"], 2)})
            out["recommendation"] = "СТАВКА: %s на «%s» по %.2f — edge %+.1f п.п. к линии, смешанная p %.0f%%" % (
                flat_word(n), pick, odds, 100 * d["edge"], 100 * (d["p_blend"] if d["side"] == "a" else 1 - d["p_blend"]))
        else:
            out["recommendation"] = "ПРОПУСК: %s" % d["reason"]
    else:
        parts = []
        if out["min_odds_yes"]:
            parts.append("«%s» при кэфе от %.2f" % (yes, out["min_odds_yes"]))
        if out["min_odds_no"]:
            parts.append("«%s» при кэфе от %.2f" % (no, out["min_odds_no"]))
        out["recommendation"] = ("СТАВИТЬ " + ", ".join(parts) + "; иначе пропуск") if parts else "ПРОПУСК: нет исхода с запасом"
    return out


def flat_word(n):
    if n == int(n):
        n = int(n)
        return "%d флет%s" % (n, "" if n == 1 else "а")
    return ("%.1f флета" % n).replace(".", ",")
