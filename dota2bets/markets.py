"""Рынки помимо победителя: тотал килов и длительность карты, тотал карт и фора по картам в серии.

Каждый рынок двухисходный: «да» (больше / фора прошла) и «нет». Вероятность «да» считает модель, рекомендация —
та же политика, что для победителя: смешивание с линией, порог edge, Келли на четверть, флеты.
Кэфы и линии задаются пользователем; без кэфов выдаётся минимальный кэф для ставки.
"""

import math

from . import policy

TITLES = {"kills": "Тотал килов на карте", "dur": "Длительность карты, мин", "maps": "Тотал карт в серии",
          "hcp_a": "Фора по картам", "hcp_b": "Фора по картам"}


def half_line(x):
    """Ближайшая линия вида N.5."""
    return math.floor(x) + 0.5


def totals(pr, res):
    """Прогноз тоталов карты: {'kills': {mu, sigma, model}, 'dur': {...}} или {} без обученных моделей."""
    out = {}
    f = res["features"]
    tag = "post" if res["mode"] == "post" else "pre"
    for kind, base in (("kills", "t_base_k"), ("dur", "t_base_d")):
        m = pr.models.get("tot_%s_%s" % (kind, tag))
        if not m or base not in f:
            continue
        mu = f[base] + m.predict([f.get(n, 0.0) for n in m.names])
        out[kind] = {"mu": mu, "sigma": m.sigma, "level": f[base], "model": m,
                     "median": mu + (m.resid_q[100] if m.resid_q else 0.0)}
    return out


def market(key, title, yes, no, line, p_yes, s, rel, reasons, odds=None, note=None):
    v = policy.market_verdict(p_yes, yes, no, odds[0] if odds else None, odds[1] if odds else None, s, rel, reasons, title)
    return {"key": key, "title": title, "yes": yes, "no": no, "line": line, "p_yes": p_yes, "odds_yes": odds[0] if odds else None,
            "odds_no": odds[1] if odds else None, "note": note, "verdict": v}


def build(pr, res, name_a, name_b, bo, dist, s, lines=None, odds=None, lead_a=0, lead_b=0):
    """Список рынков. lines: {'kills': 48.5, 'dur': 38.5, 'maps': 2.5}; odds: {key: (кэф «да», кэф «нет»)}."""
    lines, odds = lines or {}, odds or {}
    rel, reasons = policy.reliability(res)
    out = []
    tot = totals(pr, res)
    for kind, unit in (("kills", ""), ("dur", " мин")):
        t = tot.get(kind)
        if not t:
            continue
        line = float(lines.get(kind) or half_line(t["median"]))
        p = t["model"].prob_over(t["mu"], line)
        note = "ожидание %.1f%s, медиана %.1f, общий уровень сейчас %.1f, разброс ±%.0f; оценка %s" % (
            t["mu"], unit, t["median"], t["level"], t["sigma"], "после драфта" if res["mode"] == "post" else "до драфта")
        out.append(market(kind, TITLES[kind], "больше %.1f" % line, "меньше %.1f" % line, line, p, s, rel, reasons,
                          odds.get(kind), note))
    if dist and bo > 1:
        scores = dist["scores"]
        played = lead_a + lead_b
        default = {3: 2.5, 5: 4.5, 2: 1.5}.get(bo, bo - 0.5)
        line = float(lines.get("maps") or default)
        if line > played:
            p = sum(v for (a, b), v in scores.items() if a + b > line)
            out.append(market("maps", TITLES["maps"], "больше %.1f" % line, "меньше %.1f" % line, line, p, s, rel, reasons,
                              odds.get("maps"), "с поправкой на то, что bo3 реже доходит до третьей карты"))
        hc = float(lines.get("hcp") or 1.5)
        pa = sum(v for (a, b), v in scores.items() if a - b > hc)
        pb = sum(v for (a, b), v in scores.items() if b - a > hc)
        out.append(market("hcp_a", TITLES["hcp_a"], "%s −%.1f" % (name_a, hc), "%s +%.1f" % (name_b, hc), hc, pa, s, rel,
                          reasons, odds.get("hcp_a"), "%s выигрывает с разницей больше %.1f карты" % (name_a, hc)))
        out.append(market("hcp_b", TITLES["hcp_b"], "%s −%.1f" % (name_b, hc), "%s +%.1f" % (name_a, hc), hc, pb, s, rel,
                          reasons, odds.get("hcp_b"), "%s выигрывает с разницей больше %.1f карты" % (name_b, hc)))
    return out


def parse_spec(spec):
    """'kills:48.5:1.85,1.90' → ('kills', 48.5, (1.85, 1.90)); линию и кэфы можно опускать: 'maps::2.10,1.70'."""
    parts = spec.split(":")
    key = parts[0].strip()
    if key not in TITLES and key != "hcp":
        raise ValueError("рынок %s неизвестен: kills, dur, maps, hcp_a, hcp_b" % key)
    line = float(parts[1].replace(",", ".")) if len(parts) > 1 and parts[1].strip() else None
    od = None
    if len(parts) > 2 and parts[2].strip():
        a, b = parts[2].split(",")
        od = (float(a), float(b))
    return key, line, od


def describe(m):
    v = m["verdict"]
    head = "%s: %s — %.0f%%, %s — %.0f%%" % (m["title"], m["yes"], 100 * m["p_yes"], m["no"], 100 * (1 - m["p_yes"]))
    lines = [head]
    if m["note"]:
        lines.append("    " + m["note"])
    lines.append("    " + v["recommendation"])
    return "\n".join(lines)
