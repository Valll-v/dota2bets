"""Досье матча для Claude: все факты из базы в компактном markdown (цель — до 25 КБ)."""

import math
import os
import re
import time

from . import DATA, ROOT
from .cli_data import team_matches, player_role, h2h, fmt_ts, departed
from .series import series_distribution
from .odds import evaluate

NOTES_DIR = os.path.join(ROOT, "notes")   # заметки пользователя — в git, не в data/
DOSSIERS_DIR = os.path.join(DATA, "dossiers")
DAY = 86400


def slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", str(text)).strip("-").lower() or "x"


def read_note(kind, key):
    for name in (str(key), slug(key)):
        path = os.path.join(NOTES_DIR, kind, name + ".md")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
    return ""


def player_block(pr, conn, acc, hero=None, now=None):
    """Строка по игроку: Elo, роль, форма, топ героев; если задан hero — статистика на нём."""
    st = pr.state
    name = pr.player_names.get(acc, str(acc))
    elo = st.player_elo.get(acc, now)
    role = player_role(conn, acc) or "-"
    rows = conn.execute(
        "select (mp.is_radiant=m.radiant_win) won, m.start_time from match_players mp join matches m on m.match_id=mp.match_id "
        "where mp.account_id=? order by m.start_time desc limit 20", (acc,)).fetchall()
    w = sum(1 for r in rows if r["won"])
    farm, _ = st.player_farm.mean(acc, now, prior=5.0)
    lane, _ = st.player_lane.mean(acc, now, prior=5.0)
    lead_p, _ = st.lead.rate(("p", acc), now, 5.0, 0.9)
    lines = ["- **%s** (id %d): Elo %.0f, линия %s, последние %d карт %d-%d, фарм %+.0f GPM к базе героев, лейнинг %+.0f к 10 мин, сливы лида %.0f%%" % (
        name, acc, elo, role, len(rows), w, len(rows) - w, farm, lane, 100 * (1 - lead_p))]
    top = conn.execute(
        "select mp.hero_id, count(*) g, sum(case when mp.is_radiant=m.radiant_win then 1 else 0 end) w "
        "from match_players mp join matches m on m.match_id=mp.match_id where mp.account_id=? and m.start_time>=? "
        "group by mp.hero_id order by g desc limit 6", (acc, now - 365 * DAY)).fetchall()
    if top:
        lines.append("  топ героев за год: " + ", ".join("%s %d (%d%%)" % (pr.hero_names.get(r["hero_id"], r["hero_id"]), r["g"],
                                                                        100 * r["w"] // max(r["g"], 1)) for r in top))
    if hero:
        r = conn.execute(
            "select count(*) g, sum(case when mp.is_radiant=m.radiant_win then 1 else 0 end) w, avg(mp.gpm) gpm, "
            "avg((mp.kills+mp.assists)*1.0/max(mp.deaths,1)) kda, max(m.start_time) last "
            "from match_players mp join matches m on m.match_id=mp.match_id where mp.account_id=? and mp.hero_id=?",
            (acc, hero)).fetchone()
        hn = pr.hero_names.get(hero, str(hero))
        if r["g"]:
            lines.append("  на %s: %d карт, %d%% побед, GPM %.0f, KDA %.1f, последняя %s" % (
                hn, r["g"], 100 * r["w"] // r["g"], r["gpm"] or 0, r["kda"] or 0, fmt_ts(r["last"])))
        else:
            lines.append("  на %s: в про-матчах базы не играл" % hn)
    return "\n".join(lines)


def team_block(pr, conn, team_id, lineup, draft, now, label):
    st = pr.state
    raw_id = team_id
    raw_last = team_matches(conn, raw_id, 1)
    last_ts_raw = raw_last[0]["start_time"] if raw_last else 0
    team_id = st.canonical(team_id, frozenset(lineup), now)
    t = conn.execute("select * from teams where team_id=?", (team_id,)).fetchone()
    name = t["name"] if t else str(team_id)
    side = st.team_side(team_id, frozenset(lineup), now, None)
    lines = ["## %s: %s (id %d)" % (label, name, team_id),
             "Elo команды %.0f, roster-Elo %.0f, рейтинг OpenDota %s. Форма (затухающая, с поправкой на состав) %.2f при весе %.1f. "
             "Стабильность пятёрки %.1f, смена состава относительно прошлого матча %.0f%%. Последний матч %s назад; карт за 7 дней %d." % (
                 side["elo"], side["roster_elo"], "%.0f" % t["rating"] if t and t["rating"] else "-", side["form"],
                 side["form_g"], side["stability"], 100 * side["lineup_change"], "%.0f дн" % math.expm1(side["rest"]),
                 side["load7"])]
    lines.append("Устойчивость (с усадкой, команда и пятёрка пополам): при лиде ≥ 10к проигрывает %.0f%% карт, при отставании "
                 "≥ 10к выигрывает %.0f%%; в решающих картах серий остаток к форме %+.2f; на premium-турнирах остаток %+.2f; "
                 "фарм пятёрки относительно базы героев %+.0f GPM, лейнинг (золото+опыт к 10-й минуте относительно базы героев) %+.0f." % (
                     100 * side["throw"], 100 * side["comeback"], side["decider"],
                     st.tier_res(team_id, "premium", now, side["form"]), 100 * side["farm"], 1000 * side["lane"]))
    ms = team_matches(conn, team_id, 12)
    teams = {r["team_id"]: r["name"] for r in conn.execute("select team_id, name from teams")}
    if ms:
        lines.append("Последние карты: " + ", ".join("%s %s %s" % (fmt_ts(m["start_time"])[5:], "W" if m["won"] else "L",
                                                                  teams.get(m["opp"], m["opp"])) for m in ms))
        # состав в последних матчах: сколько матчей у нынешней пятёрки
        same = 0
        for m in ms:
            pl = {r[0] for r in conn.execute("select account_id from match_players where match_id=? and is_radiant=?",
                                             (m["match_id"], 1 if m["is_radiant"] else 0))}
            same += 1 if pl == set(lineup) else 0
        lines.append("Нынешняя пятёрка играла в %d из последних %d карт." % (same, len(ms)))
    lines.append("")
    lines.append("### Игроки")
    hero_of = dict(draft) if draft else {}
    gone = departed(conn, raw_id, lineup, last_ts_raw)
    for acc in lineup:
        lines.append(player_block(pr, conn, acc, hero_of.get(acc), now))
        if acc in gone:
            lines.append("  **УШЁЛ ИЗ КОМАНДЫ:** после последнего матча команды играл за %s (%s). Замена в базе неизвестна." % (
                gone[acc][1], fmt_ts(gone[acc][2])))
    if gone:
        lines.append("**Внимание:** %d из пятёрки с тех пор играли за другие команды — состав в досье устарел, модель этого не видит "
                     "(признак смены состава 0)." % len(gone))
    if draft:
        lines.append("")
        lines.append("### Драфт")
        d = st.draft_side(team_id, draft, now)
        for acc, h in draft:
            p, g = st.hero.rate(h, now, 10.0)
            tp, tg = st.team_hero.rate((team_id, h), now, 5.0)
            r, rg = st.player_residual(acc, h, now)
            lines.append("- %s (%s): про-винрейт в мете %.0f%% (вес %.0f); команда на нём %.0f%% (вес %.1f); "
                         "требовательность героя %.2f, остаток игрока на нём %+.2f (вес %.1f)" % (
                             pr.hero_names.get(h, h), pr.player_names.get(acc, acc), 100 * p, g, 100 * tp, tg,
                             st.demand(h, now), r, rg))
        pairs = []
        heroes = [h for _, h in draft]
        for i in range(len(heroes)):
            for j in range(i + 1, len(heroes)):
                pairs.append((st.synergy(heroes[i], heroes[j], now), heroes[i], heroes[j]))
        pairs.sort()
        lines.append("Синергии (лог-оддс сверх силы героев): худшие %s; лучшие %s" % (
            ", ".join("%s+%s %+.2f" % (pr.hero_names.get(a), pr.hero_names.get(b), s) for s, a, b in pairs[:2]),
            ", ".join("%s+%s %+.2f" % (pr.hero_names.get(a), pr.hero_names.get(b), s) for s, a, b in pairs[-2:])))
        lines.append("Итог по драфту: мета %+.2f, синергия %+.2f, комфорт команды %+.2f, комфорт игроков %+.2f "
                     "(взвешенный требовательностью %+.2f), опыт на героях %.1f; сложность драфта %.2f, исполнение "
                     "команды на сложных драфтах %+.2f" % (
                         d["meta"], d["synergy"], d["team_hero"], d["player_hero"], d["player_hero_w"],
                         d["player_hero_exp"], d["complexity"], d["exec"]))
    note = read_note("teams", name) or read_note("teams", team_id)
    if note:
        lines.append("")
        lines.append("### Заметки пользователя о команде")
        lines.append(note)
    for acc in lineup:
        note = read_note("players", acc) or read_note("players", pr.player_names.get(acc, ""))
        if note:
            lines.append("")
            lines.append("### Заметки пользователя об игроке %s" % pr.player_names.get(acc, acc))
            lines.append(note)
    return "\n".join(lines)


def build(pr, conn, team_a, team_b, res, bo=1, odds=None, now=None):
    """Досье по результату Predictor.predict. Возвращает markdown."""
    now = now or int(time.time())
    ta = conn.execute("select name from teams where team_id=?", (team_a,)).fetchone()
    tb = conn.execute("select name from teams where team_id=?", (team_b,)).fetchone()
    na, nb = ta["name"] if ta else str(team_a), tb["name"] if tb else str(team_b)
    lines = ["# Досье: %s vs %s" % (na, nb),
             "Дата %s. Формат bo%d. Режим модели: %s. Все числа — из базы про-матчей (лиги premium/professional с 2024 года)." % (
                 time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now)), bo, "после драфта" if res["mode"] == "post" else "до драфта"),
             ""]
    for label, cid in (("A", res["detail"].get("canon_a")), ("B", res["detail"].get("canon_b"))):
        if cid:
            t = conn.execute("select name from teams where team_id=?", (cid,)).fetchone()
            lines.append("Команда %s: нынешняя пятёрка последним играла под тегом **%s** (id %d) — Elo команды, форма, h2h и "
                         "история ниже считаются по этому id." % (label, t["name"] if t else cid, cid))
    lines.append("## Оценка модели")
    lines.append("p(%s) на карте = %.1f%% (если %s на Radiant %.1f%%, на Dire %.1f%%)." % (
        na, 100 * res["p"], na, 100 * res["p_radiant"], 100 * res["p_dire"]))
    if bo > 1:
        d = series_distribution(res["p"], bo)
        lines.append("Серия bo%d: p(%s) = %.1f%%; счёт: %s." % (bo, na, 100 * d["p_a"], ", ".join(
            "%d-%d %.0f%%" % (s[0], s[1], 100 * p) for s, p in d["scores"].items())))
    c = res["contributions"]
    top = sorted(((k, v) for k, v in c.items() if k != "intercept"), key=lambda kv: -abs(kv[1]))
    lines.append("Вклад признаков в логит (A − B): " + ", ".join("%s %+.2f" % kv for kv in top) + "; сторона %+.2f." % c["intercept"])
    lines.append("Бэктест модели (walk-forward 2024-06 — 2026-09): log loss %s, точность %s против Elo %s. "
                 "Модель откалибрована, но её горизонт — только цифры базы: она не знает о болезнях, визах, тильте и внутренних конфликтах." % (
                     _bt("logloss", res["mode"]), _bt("acc", res["mode"]), _bt("elo", res["mode"])))
    if odds:
        oa, ob = odds
        p_series = series_distribution(res["p"], bo)["p_a"] if bo > 1 else res["p"]
        ev = evaluate(p_series, oa, ob)
        lines.append("Кэфы на %s %.2f / %.2f: implied %.1f%% / %.1f%% (маржа %.1f%%); наша p на %s %.1f%%. "
                     "Edge %s %+.1f%%, %s %+.1f%%. Келли/4: %s %.1f%%, %s %.1f%%." % (
                         "серию bo%d" % bo if bo > 1 else "карту", oa, ob, 100 * ev["implied_a"], 100 * ev["implied_b"],
                         100 * ev["margin"], "серию" if bo > 1 else "карту", 100 * p_series, na, 100 * ev["edge_a"], nb,
                         100 * ev["edge_b"], na, 100 * ev["kelly_a"], nb, 100 * ev["kelly_b"]))
    from . import policy
    dist_v = series_distribution(res["p"], bo) if bo > 1 else None
    v = policy.verdict(res, na, nb, dist_v["p_a"] if dist_v else res["p"],
                       [("%d-%d" % k, x) for k, x in dist_v["scores"].items()] if dist_v else [], bo,
                       odds[0] if odds else None, odds[1] if odds else None, policy.settings(conn))
    st_ = policy.settings(conn)
    lines.append("Вердикт модели: %s. Надёжность данных: %s%s." % (
        v["text"], v["reliability"], (" (" + "; ".join(v["reliability_reasons"]) + ")") if v["reliability_reasons"] else ""))
    lines.append("Рекомендация политики (флет = %.1f%% банка; смешанная p = %.0f%% линии + %.0f%% модели; порог edge %.0f п.п.): %s" % (
        100 * st_["flat"], 100 * st_["blend"], 100 * (1 - st_["blend"]), 100 * st_["min_edge"], v["recommendation"]))
    g, wa = res["detail"]["h2h"]
    rows, wins_a = h2h(conn, team_a, team_b)
    lines.append("Личные встречи за 2 года: %d-%d по картам (%s), с затуханием %.1f из %.1f за %s." % (
        wins_a, len(rows) - wins_a, ", ".join("%s %s" % (fmt_ts(r["start_time"])[:7], na if (r["radiant_team_id"] == team_a) == bool(r["radiant_win"]) else nb)
                                              for r in rows[:8]) or "нет", wa, g, na))
    from . import draft as draft_mod
    try:
        dr = draft_mod.analyze(pr, conn, res, team_a, team_b, na, nb, now)
        if dr:
            lines.append("")
            lines.append("## Разбор драфта")
            lines.append("```")
            lines.append(draft_mod.describe(dr, na, nb))
            lines.append("```")
    except Exception as e:
        lines.append("Разбор драфта недоступен: %r" % e)
    from . import players
    try:
        pl = players.matchup(pr, conn, res, team_a, team_b, na, nb, now)
        lines.append("")
        lines.append("## Индивидуальный разрез")
        if pl["duel"]:
            lines.append(pl["duel"])
        for ln in pl["lines"]:
            lines.append("- " + ln["text"])
        for label, side in ((na, pl["a"]), (nb, pl["b"])):
            lines.append("")
            lines.append("| %s | роль | рейтинг | фарм к базе | последние карты | форма к обычной | карт с командой |" % label)
            lines.append("|---|---|---|---|---|---|---|")
            for p in side["players"]:
                lines.append("| %s%s | %s | %d | %+d GPM | %d из %d | %s | %d |" % (
                    p["name"], (" (" + ", ".join(p["flags"]) + ")") if p["flags"] else "", "ядро" if p.get("core") else "поддержка",
                    p["elo"], p["farm"], p["recent_wins"], p["recent_n"],
                    ("%+d GPM" % p["farm_dip"]) if p["farm_dip"] is not None else "мало данных", p["tenure"]))
        lines.append("")
        lines.append(pl["caveat"])
    except Exception as e:
        lines.append("Индивидуальный разрез недоступен: %r" % e)
    lines.append("")
    lines.append(team_block(pr, conn, team_a, res["lineup_a"], res["draft_a"], now, "Команда A"))
    lines.append("")
    lines.append(team_block(pr, conn, team_b, res["lineup_b"], res["draft_b"], now, "Команда B"))
    if res["draft_a"] and res["draft_b"]:
        st = pr.state
        ctr = []
        for _, ha in res["draft_a"]:
            for _, hb in res["draft_b"]:
                ctr.append((st.counter_logit(ha, hb, now), ha, hb))
        ctr.sort()
        lines.append("")
        lines.append("## Контры (лог-оддс A против B сверх силы героев)")
        lines.append("Худшие для A: " + ", ".join("%s vs %s %+.2f" % (pr.hero_names.get(a), pr.hero_names.get(b), s) for s, a, b in ctr[:4]))
        lines.append("Лучшие для A: " + ", ".join("%s vs %s %+.2f" % (pr.hero_names.get(a), pr.hero_names.get(b), s) for s, a, b in ctr[-4:]))
        lines.append("Сумма контров: %+.2f." % res["features"]["counter"])
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > 25000:
        text = text[:24000] + "\n\n(досье обрезано до 25 КБ)"
    return text


_BT = {}


def _bt(key, mode):
    if not _BT:
        from .backtest import load_report
        r = load_report()
        if r:
            for name, m in r["models"].items():
                _BT[name] = {"logloss": m["overall"]["logloss"], "acc": m["overall"]["acc"],
                             "elo": m["baseline_elo"]["logloss"]}
    m = _BT.get(mode)
    return "%.3f" % m[key] if m else "?"


def save(text, key):
    os.makedirs(DOSSIERS_DIR, exist_ok=True)
    path = os.path.join(DOSSIERS_DIR, key + ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def key_for(name_a, name_b, now=None):
    return "%s--vs--%s--%s" % (slug(name_a), slug(name_b), time.strftime("%Y%m%d-%H%M", time.gmtime(now or time.time())))
