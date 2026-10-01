"""Глубокий разбор драфта карты: кто на каком герое и как на нём играет, линии, сила по времени, пары.

Источники: база про-матчей (карты игрока на герое, GPM и KDA против базы героя), состояние модели (мета, синергии,
контры, сила героев и игроков на линии) и, по возможности, общая статистика игрока на герое из OpenDota
(`/players/{id}/heroes`, с пабами; кеш на сутки). Пабы идут только в описание, в модель не входят.
"""

import time

from . import opendota as od
from .learn import logit

DAY = 86400
LANES = {1: "лёгкая", 2: "центр", 3: "сложная"}
BUCKETS = (("до 30 мин", 0, 1800), ("30–40 мин", 1800, 2400), ("после 40 мин", 2400, 10 ** 6))


def pct(x):
    return "%.0f%%" % (100 * x)


def hero_base(conn, hero, since):
    r = conn.execute(
        "select count(*) g, avg(mp.gpm) gpm, avg((mp.kills + mp.assists) * 1.0 / max(mp.deaths, 1)) kda "
        "from match_players mp join matches m on m.match_id = mp.match_id where mp.hero_id = ? and m.start_time >= ?",
        (hero, since)).fetchone()
    return {"games": r["g"], "gpm": r["gpm"] or 0.0, "kda": r["kda"] or 0.0}


def player_on_hero(conn, acc, hero, now):
    rows = conn.execute(
        "select (mp.is_radiant = m.radiant_win) won, mp.gpm, mp.kills, mp.deaths, mp.assists, mp.gold10, mp.xp10, m.start_time "
        "from match_players mp join matches m on m.match_id = mp.match_id "
        "where mp.account_id = ? and mp.hero_id = ? and m.start_time <= ? order by m.start_time desc", (acc, hero, now)).fetchall()
    g = len(rows)
    if not g:
        return {"games": 0, "wins": 0}
    recent = [r for r in rows if now - r["start_time"] <= 180 * DAY]
    return {
        "games": g, "wins": sum(1 for r in rows if r["won"]),
        "gpm": sum(r["gpm"] or 0 for r in rows) / g,
        "kda": sum(((r["kills"] or 0) + (r["assists"] or 0)) / max(r["deaths"] or 0, 1) for r in rows) / g,
        "last": rows[0]["start_time"], "days_since": round((now - rows[0]["start_time"]) / DAY),
        "recent_games": len(recent), "recent_wins": sum(1 for r in recent if r["won"]),
        "last5": "".join("W" if r["won"] else "L" for r in rows[:5]),
    }


def pubs(acc, hero):
    """Общая статистика игрока на герое из OpenDota (про и пабы). None, если недоступно."""
    try:
        data = od.get("players/%d/heroes" % acc, ttl=od.DAY)
    except Exception:
        return None
    for h in data:
        if int(h.get("hero_id") or 0) == hero:
            g = h.get("games") or 0
            return {"games": g, "wins": h.get("win") or 0, "last": h.get("last_played") or 0}
    return {"games": 0, "wins": 0, "last": 0}


def label_pick(p, base, core):
    """Ярлык пика и объяснение. Винрейт усаживается к 50 % четырьмя условными картами."""
    g, w = p["games"], p["wins"]
    pb = p.get("pubs")
    if g == 0:
        if pb and pb["games"] >= 40:
            return "впервые в про", "в про-матчах базы не играл, в общей статистике %d игр, %s побед" % (
                pb["games"], pct(pb["wins"] / pb["games"]))
        return "кот в мешке", "в про-матчах базы не играл" + (", в общей статистике %d игр" % pb["games"] if pb else "")
    wr = (w + 2.0) / (g + 4.0)
    perf = p["gpm"] - base["gpm"] if core and base["gpm"] else 0.0
    kda_d = p["kda"] - base["kda"] if base["kda"] else 0.0
    facts = "%d карт, %d побед (%s)" % (g, w, pct(w / g))
    if core and base["gpm"]:
        facts += ", GPM %+.0f к базе героя" % perf
    facts += ", KDA %.1f при базе %.1f" % (p["kda"], base["kda"])
    if p["days_since"] > 150:
        facts += ", последний раз %d дней назад" % p["days_since"]
    if g < 5:
        return "редкий пик", facts
    strong_perf = perf >= 40 or kda_d >= 1.5
    weak_perf = perf <= -40 or kda_d <= -1.5
    if g >= 8 and wr >= 0.60 and not weak_perf:
        return "сигнатурный", facts
    if wr >= 0.55 or (strong_perf and wr >= 0.47):
        return "комфортный", facts
    if g >= 8 and wr <= 0.40:
        return "слабый", facts
    if wr <= 0.43 or (weak_perf and wr <= 0.5):
        return "неудачный", facts
    return "нейтрально", facts


def picks_side(pr, conn, team, draft, now, cores, with_pubs):
    st = pr.state
    out = []
    since = now - 365 * DAY
    for acc, hero in draft:
        base = hero_base(conn, hero, since)
        p = player_on_hero(conn, acc, hero, now) if acc else {"games": 0, "wins": 0}
        p["pubs"] = pubs(acc, hero) if (with_pubs and acc) else None
        core = acc in cores
        lab, facts = label_pick(p, base, core)
        meta_p, meta_g = st.hero.rate(hero, now, 10.0)
        team_p, team_g = st.team_hero.rate((team, hero), now, 5.0)
        resid, resid_g = st.player_residual(acc, hero, now) if acc else (0.0, 0.0)
        out.append({
            "player": pr.player_names.get(acc, str(acc)) if acc else "?", "account_id": acc, "hero": hero,
            "hero_name": pr.hero_names.get(hero, str(hero)), "core": core, "label": lab, "facts": facts,
            "games": p["games"], "wins": p["wins"], "gpm": round(p.get("gpm") or 0), "kda": round(p.get("kda") or 0, 1),
            "gpm_base": round(base["gpm"]), "kda_base": round(base["kda"], 1), "last5": p.get("last5", ""),
            "days_since": p.get("days_since"), "recent_games": p.get("recent_games", 0), "recent_wins": p.get("recent_wins", 0),
            "pubs": p["pubs"], "meta_wr": meta_p, "meta_games": round(meta_g), "team_wr": team_p, "team_games": round(team_g, 1),
            "demand": round(st.demand(hero, now), 2), "model_resid": round(resid, 2),
        })
    return out


_LANE_CACHE = {"ts": 0, "hero": {}, "role": {}}


def lane_table(conn, now):
    """Средние золото + опыт к 10-й минуте по паре (герой, линия) и по линии в целом, про-матчи за 240 дней.
    Считается раз в час: сила героя зависит от линии, общий средний по герою смешивает роли."""
    if now - _LANE_CACHE["ts"] < 3600 and _LANE_CACHE["hero"]:
        return _LANE_CACHE
    hero, role = {}, {}
    for r in conn.execute(
            "select mp.hero_id h, mp.lane_role l, count(*) n, avg(mp.gold10 + mp.xp10) v from match_players mp "
            "join matches m on m.match_id = mp.match_id where m.start_time >= ? and mp.gold10 is not null "
            "and mp.lane_role in (1, 2, 3) group by 1, 2", (now - 240 * DAY,)):
        hero[(r["h"], r["l"])] = (r["n"], r["v"])
    for l in (1, 2, 3):
        tot = sum(n for (h, x), (n, v) in hero.items() if x == l)
        role[l] = sum(n * v for (h, x), (n, v) in hero.items() if x == l) / tot if tot else 0.0
    _LANE_CACHE.update({"ts": now, "hero": hero, "role": role})
    return _LANE_CACHE


def lanes(pr, conn, draft_a, draft_b, now, side):
    """Линии: лёгкая A против сложной B, центр, сложная A против лёгкой B.
    Сила = отклонение героя от среднего по этой линии + отклонение игрока от среднего по его героям на этой линии."""
    from .cli_data import player_role
    tab = lane_table(conn, now)

    def split(draft):
        by = {1: [], 2: [], 3: []}
        for acc, hero in draft:
            role = player_role(conn, acc) if acc else None
            if role in by:
                by[role].append((acc, hero))
        return by

    def player_dev(acc, role):
        rows = conn.execute(
            "select mp.hero_id h, mp.gold10 + mp.xp10 v from match_players mp join matches m on m.match_id = mp.match_id "
            "where mp.account_id = ? and mp.lane_role = ? and mp.gold10 is not null and m.start_time <= ? "
            "order by m.start_time desc limit 40", (acc, role, now)).fetchall()
        devs = [r["v"] - tab["hero"][(r["h"], role)][1] for r in rows if tab["hero"].get((r["h"], role), (0, 0))[0] >= 15]
        return sum(devs) / (len(devs) + 5.0) if devs else 0.0        # усадка к нулю пятью условными картами

    def strength(items, role):
        if not items:
            return None, []
        tot, parts = 0.0, []
        for acc, hero in items:
            n, v = tab["hero"].get((hero, role), (0, 0.0))
            hdev = (v - tab["role"][role]) if n >= 15 else 0.0
            pdev = player_dev(acc, role) if acc else 0.0
            tot += hdev + pdev
            parts.append({"player": pr.player_names.get(acc, str(acc)), "hero": hero, "hero_name": pr.hero_names.get(hero, str(hero)),
                          "hero_dev": round(hdev), "player_dev": round(pdev), "hero_games": n})
        return tot / len(items), parts

    st = pr.state
    a, b = split(draft_a), split(draft_b)
    out = []
    for title, la, lb in (("Лёгкая A — сложная B", 1, 3), ("Центр", 2, 2), ("Сложная A — лёгкая B", 3, 1)):
        sa, pa = strength(a[la], la)
        sb, pb = strength(b[lb], lb)
        ctr, n = 0.0, 0
        for _, ha in a[la]:
            for _, hb in b[lb]:
                ctr += st.counter_logit(ha, hb, now)
                n += 1
        adv = round(sa - sb) if sa is not None and sb is not None else None
        out.append({"title": title, "a": pa, "b": pb, "adv": adv, "counter": round(ctr, 2) if n else None})
    return out


def timing(conn, heroes_a, heroes_b, now):
    """Сила драфтов по длительности карты: средний усаженный винрейт героев в каждом отрезке."""
    heroes = list(heroes_a) + list(heroes_b)
    if not heroes:
        return []
    rows = conn.execute(
        "select mp.hero_id h, case when m.duration < 1800 then 0 when m.duration < 2400 then 1 else 2 end b, count(*) g, "
        "sum(mp.is_radiant = m.radiant_win) w from match_players mp join matches m on m.match_id = mp.match_id "
        "where mp.hero_id in (%s) and m.start_time >= ? and m.duration > 600 group by 1, 2" % ",".join("?" * len(heroes)),
        heroes + [now - 240 * DAY]).fetchall()
    stat = {(r["h"], r["b"]): (r["g"], r["w"]) for r in rows}
    out = []
    for i, (title, _, _) in enumerate(BUCKETS):
        def side(hs):
            vals = []
            for h in hs:
                g, w = stat.get((h, i), (0, 0))
                vals.append((w + 5.0) / (g + 10.0))
            return sum(vals) / len(vals) if vals else 0.5
        pa, pb = side(heroes_a), side(heroes_b)
        out.append({"bucket": title, "a": pa, "b": pb, "diff": logit(pa) - logit(pb)})
    return out


STAGES = (("до 30 мин", 0, 30), ("30–40 мин", 30, 40), ("40–50 мин", 40, 50), ("после 50 мин", 50, 10 ** 6))
LONG_MIN = 35                      # граница «короткая / длинная карта» для контров по стадиям
STAGE_TAG_GAP = 0.05               # разница винрейтов «после 40» и «до 30», с которой герой считается лейт- или темпо-героем


def shrink(w, g, k=10.0):
    return (w + 0.5 * k) / (g + k)


def hero_stages(conn, heroes, now):
    """По каждому герою: усаженный винрейт и карты в четырёх отрезках длительности за 240 дней, средняя длительность
    его карт и ярлык: «лейт» (сильнее в затяжной игре), «темп» (сильнее в короткой), «ровный», «мало данных»."""
    heroes = sorted(set(h for h in heroes if h))
    if not heroes:
        return {}
    rows = conn.execute(
        "select mp.hero_id h, case when m.duration < 1800 then 0 when m.duration < 2400 then 1 when m.duration < 3000 then 2 else 3 end b, "
        "count(*) g, sum(mp.is_radiant = m.radiant_win) w, avg(m.duration) d from match_players mp join matches m on m.match_id = mp.match_id "
        "where mp.hero_id in (%s) and m.start_time >= ? and m.duration > 600 and m.radiant_win is not null group by 1, 2"
        % ",".join("?" * len(heroes)), heroes + [now - 240 * DAY]).fetchall()
    stat = {(r["h"], r["b"]): (r["g"], r["w"], r["d"]) for r in rows}
    base_dur = conn.execute("select avg(duration) from matches where start_time >= ? and duration > 600",
                            (now - 240 * DAY,)).fetchone()[0] or 2400.0
    out = {}
    for h in heroes:
        bs = []
        for i in range(len(STAGES)):
            g, w, _ = stat.get((h, i), (0, 0, 0))
            bs.append({"wr": shrink(w, g), "games": g})
        total = sum(b["games"] for b in bs)
        dur = sum(stat.get((h, i), (0, 0, 0))[0] * (stat.get((h, i), (0, 0, 0))[2] or 0) for i in range(4)) / total if total else base_dur
        early = shrink(stat.get((h, 0), (0, 0, 0))[1], stat.get((h, 0), (0, 0, 0))[0])
        lg = stat.get((h, 2), (0, 0, 0))[0] + stat.get((h, 3), (0, 0, 0))[0]
        lw = stat.get((h, 2), (0, 0, 0))[1] + stat.get((h, 3), (0, 0, 0))[1]
        late = shrink(lw, lg)
        if total < 30:
            tag = "мало данных"
        elif late - early >= STAGE_TAG_GAP:
            tag = "лейт"
        elif early - late >= STAGE_TAG_GAP:
            tag = "темп"
        else:
            tag = "ровный"
        out[h] = {"buckets": bs, "games": total, "early": early, "late": late, "tag": tag,
                  "dur_delta": round((dur - base_dur) / 60.0, 1)}
    return out


def counters_by_stage(conn, pr, heroes_a, heroes_b, now):
    """Матчапы «герой A против героя B» отдельно в коротких (< LONG_MIN) и длинных картах: где знак меняется."""
    ha = [h for h in heroes_a if h]
    hb = [h for h in heroes_b if h]
    if not ha or not hb:
        return []
    rows = conn.execute(
        "select p1.hero_id ha, p2.hero_id hb, (m.duration >= ?) lng, count(*) g, "
        "sum(case when (p1.team = 0) = (m.radiant_win = 1) then 1 else 0 end) w "
        "from picks p1 join picks p2 on p2.match_id = p1.match_id and p2.team != p1.team and p2.is_pick = 1 "
        "join matches m on m.match_id = p1.match_id "
        "where p1.is_pick = 1 and p1.hero_id in (%s) and p2.hero_id in (%s) and m.start_time >= ? and m.duration > 600 "
        "and m.radiant_win is not null group by 1, 2, 3" % (",".join("?" * len(ha)), ",".join("?" * len(hb))),
        [LONG_MIN * 60] + ha + hb + [now - 240 * DAY]).fetchall()
    stat = {(r["ha"], r["hb"], r["lng"]): (r["g"], r["w"]) for r in rows}
    hn = pr.hero_names
    out = []
    for a in ha:
        for b in hb:
            gs, ws = stat.get((a, b, 0), (0, 0))
            gl, wl = stat.get((a, b, 1), (0, 0))
            if gs < 6 or gl < 6:
                continue
            s, l = shrink(ws, gs, 4.0), shrink(wl, gl, 4.0)
            out.append({"a": hn.get(a, str(a)), "b": hn.get(b, str(b)), "short": s, "long": l, "games_short": gs,
                        "games_long": gl, "flip": round(l - s, 2)})
    out.sort(key=lambda x: -abs(x["flip"]))
    return out


def stages(pr, conn, res, heroes_a, heroes_b, name_a, name_b, now):
    """Стадии игры: профиль каждого драфта по длительности, ожидаемая длительность карты и кому она выгодна."""
    from . import markets
    hs = hero_stages(conn, list(heroes_a) + list(heroes_b), now)
    hn = pr.hero_names

    def side(heroes):
        curve = []
        for i in range(len(STAGES)):
            vals = [hs[h]["buckets"][i]["wr"] for h in heroes if h in hs]
            curve.append(sum(vals) / len(vals) if vals else 0.5)
        early = sum(hs[h]["early"] for h in heroes if h in hs) / max(1, len(heroes))
        late = sum(hs[h]["late"] for h in heroes if h in hs) / max(1, len(heroes))
        slope = late - early
        label = "лейт-драфт" if slope >= 0.04 else ("темподрафт" if slope <= -0.04 else "ровный драфт")
        tags = [{"hero": h, "hero_name": hn.get(h, str(h)), "tag": hs[h]["tag"], "early": hs[h]["early"], "late": hs[h]["late"],
                 "games": hs[h]["games"], "dur_delta": hs[h]["dur_delta"]} for h in heroes if h in hs]
        return {"curve": curve, "early": early, "late": late, "slope": round(slope, 3), "label": label, "heroes": tags}

    a, b = side(heroes_a), side(heroes_b)
    diff = [logit(x) - logit(y) for x, y in zip(a["curve"], b["curve"])]
    tot = markets.totals(pr, res).get("dur")
    probs, mu = None, None
    if tot:
        mu, m = tot["mu"], tot["model"]
        probs = []
        for _, lo, hi in STAGES:
            p_lo = 1.0 if lo <= 0 else m.prob_over(mu, float(lo))
            p_hi = 0.0 if hi >= 10 ** 5 else m.prob_over(mu, float(hi))
            probs.append(max(0.0, p_lo - p_hi))
        s = sum(probs) or 1.0
        probs = [p / s for p in probs]
    weighted = sum(p * d for p, d in zip(probs, diff)) if probs else None
    rows = [{"stage": STAGES[i][0], "a": a["curve"][i], "b": b["curve"][i], "diff": round(diff[i], 3),
             "p": probs[i] if probs else None} for i in range(len(STAGES))]
    flips = [x for x in counters_by_stage(conn, pr, heroes_a, heroes_b, now) if abs(x["flip"]) >= 0.15][:6]
    return {"a": a, "b": b, "rows": rows, "expected_min": round(mu, 1) if mu else None, "weighted": round(weighted, 3) if weighted is not None else None,
            "p_before_40": (probs[0] + probs[1]) if probs else None, "flips": flips,
            "text": stages_text(a, b, rows, mu, probs, weighted, flips, name_a, name_b)}


def stages_text(a, b, rows, mu, probs, weighted, flips, name_a, name_b):
    """Абзац про стадии: кто чего ждёт от карты и что говорит ожидаемая длительность."""
    def tagged(side, tag):
        return [h["hero_name"] for h in side["heroes"] if h["tag"] == tag]

    out = []
    for name, side in ((name_a, a), (name_b, b)):
        late, tempo = tagged(side, "лейт"), tagged(side, "темп")
        parts = []
        if late:
            parts.append("лейт-герои %s" % ", ".join(late))
        if tempo:
            parts.append("темпо-герои %s" % ", ".join(tempo))
        out.append("%s — %s (до 30 минут герои выигрывают %s, после 40 — %s%s)." % (
            name, side["label"], pct(side["early"]), pct(side["late"]), "; " + ", ".join(parts) if parts else ""))
    lead = [(r["stage"], name_a if r["diff"] > 0 else name_b, abs(r["diff"])) for r in rows]
    turns = [i for i in range(1, len(rows)) if (rows[i]["diff"] > 0) != (rows[i - 1]["diff"] > 0) and abs(rows[i]["diff"]) >= 0.03]
    if turns:
        i = turns[0]
        out.append("Перелом по героям — на %s: до этого перевес у %s, дальше у %s." % (
            rows[i]["stage"].replace("после ", "").replace(" мин", "-й минуте") if "после" in rows[i]["stage"] else rows[i]["stage"].split("–")[0] + "-й минуте",
            lead[i - 1][1], lead[i][1]))
    elif max(x[2] for x in lead) >= 0.03:
        best = max(lead, key=lambda x: x[2])
        out.append("По героям перевес у %s на всех стадиях, заметнее всего %s." % (best[1], best[0]))
    else:
        out.append("По героям стадии примерно равны.")
    if mu and probs:
        out.append("Модель ждёт карту около %.0f минут: до 40-й закончится с вероятностью %s, уйдёт за 50-ю — %s." % (
            mu, pct(probs[0] + probs[1]), pct(probs[3])))
        if weighted is not None and abs(weighted) >= 0.03:
            out.append("С учётом ожидаемой длительности герои дают перевес %s." % (name_a if weighted > 0 else name_b))
    for f in flips[:3]:
        out.append("%s против %s: до %d-й минуты %s (%d карт), после — %s (%d)." % (
            f["a"], f["b"], LONG_MIN, pct(f["short"]), f["games_short"], pct(f["long"]), f["games_long"]))
    return " ".join(out)


def pairs(pr, draft_a, draft_b, now):
    st = pr.state
    hn = pr.hero_names

    def synergy(draft):
        hs = [h for _, h in draft]
        res = []
        for i in range(len(hs)):
            for j in range(i + 1, len(hs)):
                key = (min(hs[i], hs[j]), max(hs[i], hs[j]))
                g, w = st.pair.read(key, now)
                res.append({"a": hn.get(hs[i]), "b": hn.get(hs[j]), "games": round(g), "wr": (w / g) if g >= 1 else None,
                            "score": round(st.synergy(hs[i], hs[j], now), 2)})
        return sorted(res, key=lambda x: -x["score"])

    ctr = []
    for _, ha in draft_a:
        for _, hb in draft_b:
            g, w = st.counter.read((ha, hb), now)
            ctr.append({"a": hn.get(ha), "b": hn.get(hb), "games": round(g), "wr": (w / g) if g >= 1 else None,
                        "score": round(st.counter_logit(ha, hb, now), 2)})
    ctr.sort(key=lambda x: -x["score"])
    return {"synergy_a": synergy(draft_a), "synergy_b": synergy(draft_b), "counters": ctr}


def summary(name_a, name_b, pa, pb, ln, tm, pr_, stg=None):
    """Главные выводы по драфту, от самого важного."""
    out = []
    rank = {"сигнатурный": 0, "слабый": 1, "неудачный": 2, "кот в мешке": 3, "впервые в про": 4, "редкий пик": 5, "комфортный": 6}
    notable = sorted([(rank[p["label"]], name, p) for name, side in ((name_a, pa), (name_b, pb)) for p in side if p["label"] in rank],
                     key=lambda x: x[0])
    for _, name, p in notable[:6]:
        kind = "good" if p["label"] in ("сигнатурный", "комфортный") else ("bad" if p["label"] in ("слабый", "неудачный") else "warn")
        out.append({"kind": kind, "label": p["label"], "text": "%s на %s (%s): %s." % (p["player"], p["hero_name"], name, p["facts"])})
    for l in ln:
        if l["adv"] is not None and abs(l["adv"]) >= 120:
            lead = name_a if l["adv"] > 0 else name_b
            out.append({"kind": "lane", "label": "линия", "text": "%s: перевес у %s, около %d золота и опыта на героя к 10-й минуте относительно среднего по линии (%s против %s)." % (
                l["title"], lead, abs(l["adv"]), ", ".join(x["hero_name"] for x in l["a"]) or "—", ", ".join(x["hero_name"] for x in l["b"]) or "—")})
    if stg:
        out.append({"kind": "time", "label": "стадии", "text": stg["text"]})
    elif tm:
        early, late = tm[0]["diff"], tm[-1]["diff"]
        if abs(early - late) >= 0.12:
            fast, slow = (name_a, name_b) if early > late else (name_b, name_a)
            out.append({"kind": "time", "label": "время", "text": "Драфт %s сильнее в короткой игре, драфт %s — в затяжной: до 30 минут герои %s выигрывают %s против %s, после 40 минут — %s против %s." % (
                fast, slow, name_a, pct(tm[0]["a"]), pct(tm[0]["b"]), pct(tm[-1]["a"]), pct(tm[-1]["b"]))})
    c = pr_["counters"]
    if c and c[0]["score"] >= 0.25 and c[0]["games"] >= 8:
        out.append({"kind": "good", "label": "контр", "text": "Лучший матчап %s: %s против %s, %d карт, %s побед." % (
            name_a, c[0]["a"], c[0]["b"], c[0]["games"], pct(c[0]["wr"]))})
    if c and c[-1]["score"] <= -0.25 and c[-1]["games"] >= 8:
        out.append({"kind": "bad", "label": "контр", "text": "Худший матчап %s: %s против %s, %d карт, %s побед." % (
            name_a, c[-1]["a"], c[-1]["b"], c[-1]["games"], pct(c[-1]["wr"]))})
    return out


def analyze(pr, conn, res, team_a, team_b, name_a, name_b, now=None, with_pubs=True):
    """Полный разбор драфта для результата Predictor.predict с известными пиками. None, если драфта нет."""
    da, db = res.get("draft_a"), res.get("draft_b")
    if not (da and db):
        return None
    now = now or int(time.time())
    st = pr.state
    ca = st.canonical(team_a, frozenset(res["lineup_a"]), now)
    cb = st.canonical(team_b, frozenset(res["lineup_b"]), now)

    def cores(lineup):
        gp = []
        for acc in lineup:
            r = conn.execute("select avg(gpm) g from (select mp.gpm from match_players mp join matches m on m.match_id = mp.match_id "
                             "where mp.account_id = ? order by m.start_time desc limit 20)", (acc,)).fetchone()
            gp.append((r["g"] or 0, acc))
        return {acc for _, acc in sorted(gp, reverse=True)[:3]}

    pa = picks_side(pr, conn, ca, da, now, cores(res["lineup_a"]), with_pubs)
    pb = picks_side(pr, conn, cb, db, now, cores(res["lineup_b"]), with_pubs)
    ln = lanes(pr, conn, da, db, now, res.get("side"))
    tm = timing(conn, [h for _, h in da], [h for _, h in db], now)
    prs = pairs(pr, da, db, now)
    stg = stages(pr, conn, res, [h for _, h in da], [h for _, h in db], name_a, name_b, now)
    for side_picks, side_stg in ((pa, stg["a"]), (pb, stg["b"])):
        tags = {h["hero"]: h for h in side_stg["heroes"]}
        for p in side_picks:
            t = tags.get(p["hero"])
            p["stage"] = t["tag"] if t else None
            p["stage_early"], p["stage_late"] = (t["early"], t["late"]) if t else (None, None)
    score = {"сигнатурный": 2, "комфортный": 1, "нейтрально": 0, "редкий пик": -0.5, "впервые в про": -0.5, "кот в мешке": -1,
             "неудачный": -1, "слабый": -2}
    return {
        "picks_a": pa, "picks_b": pb, "lanes": ln, "timing": tm, "pairs": prs, "stages": stg,
        "comfort_a": sum(score[p["label"]] for p in pa), "comfort_b": sum(score[p["label"]] for p in pb),
        "summary": summary(name_a, name_b, pa, pb, ln, tm, prs, stg),
        "note": "Ярлыки пиков считаются по про-матчам базы с 2024 года; выборки по паре «игрок — герой» малы, поэтому рядом "
                "всегда указано число карт. Линии определены по обычной линии игрока и могут не совпасть с фактическими.",
    }


def describe(d, name_a, name_b):
    """Текст для консоли и досье."""
    if not d:
        return ""
    lines = []
    for name, side, comfort in ((name_a, d["picks_a"], d["comfort_a"]), (name_b, d["picks_b"], d["comfort_b"])):
        lines.append("%s — комфорт драфта %+.1f:" % (name, comfort))
        for p in side:
            pub = ""
            if p["pubs"] and p["pubs"]["games"]:
                pub = "; всего с пабами %d игр, %s" % (p["pubs"]["games"], pct(p["pubs"]["wins"] / p["pubs"]["games"]))
            lines.append("  %-18s %-14s [%s] %s%s; мета героя %s, требовательность %.2f" % (
                p["hero_name"], p["player"], p["label"], p["facts"], pub, pct(p["meta_wr"]), p["demand"]))
    lines.append("Линии (перевес по золоту и опыту к 10-й минуте, на героя):")
    for l in d["lanes"]:
        lines.append("  %-22s %s против %s: %s%s" % (
            l["title"], ", ".join(x["hero_name"] for x in l["a"]) or "—", ", ".join(x["hero_name"] for x in l["b"]) or "—",
            ("%+d в пользу %s" % (abs(l["adv"]), name_a if l["adv"] >= 0 else name_b)) if l["adv"] is not None else "нет данных",
            (", контры %+.2f" % l["counter"]) if l["counter"] is not None else ""))
    lines.append("Сила героев по длительности карты (%s / %s):" % (name_a, name_b))
    lines.append("  " + "; ".join("%s: %s / %s" % (t["bucket"], pct(t["a"]), pct(t["b"])) for t in d["timing"]))
    stg = d.get("stages")
    if stg:
        lines.append("Стадии игры: %s" % stg["text"])
        for name, side in ((name_a, stg["a"]), (name_b, stg["b"])):
            lines.append("  %s (%s): %s" % (name, side["label"], "; ".join("%s — %s, до 30 мин %s, после 40 %s, %d карт, длительность %+.1f мин к средней" % (
                h["hero_name"], h["tag"], pct(h["early"]), pct(h["late"]), h["games"], h["dur_delta"]) for h in side["heroes"])))
        if stg["expected_min"]:
            lines.append("  ожидаемая длительность %.0f мин; по отрезкам: %s" % (stg["expected_min"], "; ".join(
                "%s — %s карт заканчивается здесь, герои %s / %s" % (r["stage"], pct(r["p"]) if r["p"] is not None else "?", pct(r["a"]), pct(r["b"])) for r in stg["rows"])))
        for f in stg["flips"]:
            lines.append("  матчап по стадиям: %s против %s — до %d мин %s (%d карт), после %s (%d карт)" % (
                f["a"], f["b"], LONG_MIN, pct(f["short"]), f["games_short"], pct(f["long"]), f["games_long"]))
    pr_ = d["pairs"]
    for name, key in ((name_a, "synergy_a"), (name_b, "synergy_b")):
        s = pr_[key]
        if s:
            lines.append("Синергии %s: лучшая %s + %s (%+.2f, %d карт), худшая %s + %s (%+.2f, %d карт)" % (
                name, s[0]["a"], s[0]["b"], s[0]["score"], s[0]["games"], s[-1]["a"], s[-1]["b"], s[-1]["score"], s[-1]["games"]))
    c = pr_["counters"]
    if c:
        lines.append("Контры: лучший для %s — %s против %s (%+.2f, %d карт); худший — %s против %s (%+.2f, %d карт)" % (
            name_a, c[0]["a"], c[0]["b"], c[0]["score"], c[0]["games"], c[-1]["a"], c[-1]["b"], c[-1]["score"], c[-1]["games"]))
    lines.append("Главное:")
    for x in d["summary"]:
        lines.append("  - [%s] %s" % (x["label"], x["text"]))
    lines.append(d["note"])
    return "\n".join(lines)
