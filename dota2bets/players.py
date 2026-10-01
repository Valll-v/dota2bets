"""Индивидуальный разрез: на ком держится команда, слабое звено, спад формы, новички в составе.

Всё считается по базе: рейтинг игрока, фарм относительно базы героев, последние карты против собственного обычного
уровня, стаж в команде. О болезнях, визах и конфликтах база не знает — для этого заметки пользователя (notes/) и
проверка новостей в разборе Claude.
"""

import os
import re

from . import ROOT

NOTES_DIR = os.path.join(ROOT, "notes")
DAY = 86400
RECENT = 8            # карт в «последней форме»
BASELINE = 40         # карт в «обычном уровне»
GAP_KEY = 150         # разрыв лидера со вторым по силе, после которого команда «зависит» от одного игрока
GAP_WEAK = 200        # отставание от медианы команды, после которого игрок — «слабое звено»
DIP_GPM = 50          # просадка фарма ядра относительно своего обычного уровня
DIP_KDA = 1.2         # просадка KDA
NEWCOMER = 10         # карт с командой за 120 дней, меньше — новичок или стендин


def slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", str(text)).strip("-").lower() or "x"


def note(kind, *keys):
    for key in keys:
        if key in (None, ""):
            continue
        for name in (str(key), slug(key)):
            path = os.path.join(NOTES_DIR, kind, name + ".md")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    return f.read().strip()
    return ""


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def player_row(pr, conn, acc, team_ids, now):
    st = pr.state
    rows = conn.execute(
        "select mp.hero_id, mp.gpm, mp.kills, mp.deaths, mp.assists, (mp.is_radiant = m.radiant_win) won, m.start_time, "
        "case when mp.is_radiant = 1 then m.radiant_team_id else m.dire_team_id end tid "
        "from match_players mp join matches m on m.match_id = mp.match_id "
        "where mp.account_id = ? and m.start_time <= ? order by m.start_time desc limit ?",
        (acc, now, RECENT + BASELINE)).fetchall()

    def resid(r):
        if not r["gpm"] or not r["hero_id"]:
            return None
        base, n = st.hero_gpm.mean(r["hero_id"], now)
        return r["gpm"] - base if n >= 5 else None

    def kda(r):
        return ((r["kills"] or 0) + (r["assists"] or 0)) / max(r["deaths"] or 0, 1)

    recent, base = rows[:RECENT], rows[RECENT:]
    f_recent, f_base = mean([resid(r) for r in recent]), mean([resid(r) for r in base])
    k_recent, k_base = mean([kda(r) for r in recent]), mean([kda(r) for r in base])
    enough = len(recent) >= 5 and len(base) >= 12
    tenure = sum(1 for r in rows if r["tid"] in team_ids and now - r["start_time"] <= 120 * DAY)
    last_other = next((r for r in rows if r["tid"] not in team_ids), None)
    return {
        "id": acc, "name": pr.player_names.get(acc, str(acc)),
        "elo": round(st.player_elo.get(acc, now)), "games": st.player_elo.games.get(acc, 0),
        "farm": round(st.player_farm.mean(acc, now, prior=5.0)[0]),
        "lane": round(st.player_lane.mean(acc, now, prior=5.0)[0]),
        "avg_gpm": round(mean([r["gpm"] for r in rows[:20]]) or 0),
        "recent_wins": sum(1 for r in recent if r["won"]), "recent_n": len(recent),
        "farm_dip": round(f_recent - f_base) if enough and f_recent is not None and f_base is not None else None,
        "kda_recent": round(k_recent, 1) if k_recent is not None else None,
        "kda_dip": round(k_recent - k_base, 1) if enough and k_recent is not None and k_base is not None else None,
        "tenure": tenure, "days_idle": round((now - rows[0]["start_time"]) / DAY) if rows else None,
        "note": note("players", acc, pr.player_names.get(acc)),
        "flags": [],
    }


def team_insights(pr, conn, team_id, lineup, now, name):
    """Игроки команды с ролями и флагами плюс готовые фразы."""
    st = pr.state
    canon = st.canonical(team_id, frozenset(lineup), now)
    team_ids = {team_id, canon}
    ps = [player_row(pr, conn, acc, team_ids, now) for acc in lineup]
    if not ps:
        return {"players": [], "lines": [], "note": note("teams", name, team_id)}
    # ядра — трое с наибольшим средним GPM: для саппортов фарм не показатель
    order = sorted(ps, key=lambda p: -p["avg_gpm"])
    for i, p in enumerate(order):
        p["core"] = i < 3
    team_mean = sum(p["elo"] for p in ps) / len(ps)
    by_elo = sorted(ps, key=lambda p: -p["elo"])
    median = by_elo[len(by_elo) // 2]["elo"]
    lines = []

    # лидер: сравнение со вторым по силе, а не со средним — среднее тянет вниз слабое звено
    key = by_elo[0]
    second = by_elo[1]["elo"] if len(by_elo) > 1 else key["elo"]
    key["flags"].append("лидер")
    extra = ", фарм %+d GPM к базе героев" % key["farm"] if key["core"] and abs(key["farm"]) >= 20 else ""
    role = "ядро" if key["core"] else "поддержка"
    if key["elo"] - second >= GAP_KEY:
        lines.append({"kind": "key", "text": "%s зависит от %s (%s): рейтинг %d, у следующего по силе %d%s. Если он сыграет плохо, заменить его некем." % (
            name, key["name"], role, key["elo"], second, extra)})
    else:
        lines.append({"kind": "key", "text": "Сильнейший у %s — %s (%s, рейтинг %d%s). Состав ровный: у второго по силе %d, от одного игрока команда не зависит." % (
            name, key["name"], role, key["elo"], extra, second)})

    # слабое звено: заметно ниже медианы команды
    weak = by_elo[-1]
    if weak is not key and median - weak["elo"] >= GAP_WEAK:
        weak["flags"].append("слабое звено")
        why = ["рейтинг %d при медиане команды %d" % (weak["elo"], median)]
        if weak["core"] and weak["farm"] <= -30:
            why.append("фарм %+d GPM к базе героев" % weak["farm"])
        if weak["games"] < 60:
            why.append("всего %d карт в базе, оценка неточная" % weak["games"])
        lines.append({"kind": "weak", "text": "Слабое звено %s — %s (%s): %s." % (
            name, weak["name"], "ядро" if weak["core"] else "поддержка", ", ".join(why))})

    for p in ps:
        if p["tenure"] < NEWCOMER:
            p["flags"].append("новичок")
            lines.append({"kind": "new", "text": "%s в составе %s недавно: %s за 120 дней. Сыгранность не набрана." % (
                p["name"], name, "%d карт" % p["tenure"] if p["tenure"] else "ни одной карты")})

    # спад: только индивидуальный — относительно партнёров по тем же картам.
    # Когда просели все, это уровень соперников или форма команды, а не игрока.
    kdas = [p["kda_dip"] for p in ps if p["kda_dip"] is not None]
    team_kda = sum(kdas) / len(kdas) if kdas else 0.0
    farms = [p["farm_dip"] for p in ps if p["core"] and p["farm_dip"] is not None]
    team_farm = sum(farms) / len(farms) if farms else 0.0
    down = sum(1 for k in kdas if k <= -DIP_KDA)
    if len(kdas) >= 4 and down >= 3:
        lines.append({"kind": "team", "text": "%s в целом играет хуже обычного: у %d из %d показатели на последних картах ниже привычных. Чаще всего это сильные соперники или общая форма, а не один игрок." % (
            name, down, len(kdas))})
    # личный спад: не больше одного игрока на команду — тот, кто просел сильнее всех относительно партнёров
    worst, worst_gap, worst_text = None, 0.0, ""
    for p in ps:
        if p["core"] and p["farm_dip"] is not None and p["farm_dip"] <= -DIP_GPM:
            rel = (p["farm_dip"] - team_farm) / DIP_GPM
            if rel <= -1.0 and rel < worst_gap:
                worst, worst_gap = p, rel
                worst_text = "фарм на %d GPM ниже своего обычного, у партнёров в среднем %+d" % (-p["farm_dip"], round(team_farm))
        if p["kda_dip"] is not None and p["kda_dip"] <= -DIP_KDA:
            rel = (p["kda_dip"] - team_kda) / DIP_KDA
            if rel <= -1.0 and rel < worst_gap:
                worst, worst_gap = p, rel
                worst_text = "KDA %.1f против обычных %.1f, у партнёров просадка в среднем %.1f" % (
                    p["kda_recent"], p["kda_recent"] - p["kda_dip"], -team_kda)
    if worst:
        worst["flags"].append("спад")
        lines.append({"kind": "dip", "text": "%s (%s) в личном спаде: за последние %d карт %s. Выборка мала, причину база не знает: возможны болезнь, смена роли или тильт." % (
            worst["name"], name, worst["recent_n"], worst_text)})
    for p in ps:
        if p is not worst and p["core"] and p["farm_dip"] is not None and p["farm_dip"] >= DIP_GPM and p["farm_dip"] - team_farm >= DIP_GPM:
            p["flags"].append("на подъёме")
            lines.append({"kind": "up", "text": "%s (%s) на подъёме: фарм на %d GPM выше своего обычного за последние %d карт." % (
                p["name"], name, p["farm_dip"], p["recent_n"])})
        if p["note"]:
            lines.append({"kind": "note", "text": "Заметка о %s: %s" % (p["name"], p["note"].splitlines()[0][:220])})
    tnote = note("teams", name, team_id)
    if tnote:
        lines.append({"kind": "note", "text": "Заметка о %s: %s" % (name, tnote.splitlines()[0][:220])})
    return {"players": ps, "lines": lines, "mean_elo": round(team_mean), "note": tnote}


def matchup(pr, conn, res, team_a, team_b, name_a, name_b, now):
    """Разрез для пары: обе команды и общий вывод, от кого зависит игра."""
    a = team_insights(pr, conn, team_a, res["lineup_a"], now, name_a)
    b = team_insights(pr, conn, team_b, res["lineup_b"], now, name_b)
    for side, block in (("a", a), ("b", b)):
        for l in block["lines"]:
            l["team"] = side
        for p in block["players"]:
            p["team"] = side
    lines = a["lines"] + b["lines"]
    rank = {"key": 0, "weak": 1, "new": 2, "dip": 3, "team": 4, "up": 5, "note": 6}
    lines.sort(key=lambda x: rank.get(x["kind"], 9))
    # дуэль ядер: сильнейшие по рейтингу ядра обеих команд
    ca = max((p for p in a["players"] if p.get("core")), key=lambda p: p["elo"], default=None)
    cb = max((p for p in b["players"] if p.get("core")), key=lambda p: p["elo"], default=None)
    duel = None
    if ca and cb:
        d = ca["elo"] - cb["elo"]
        lead = ca if d >= 0 else cb
        duel = "Сильнейшие ядра: %s (%d) против %s (%d)%s." % (
            ca["name"], ca["elo"], cb["name"], cb["elo"],
            " — перевес у %s, %d пунктов" % (lead["name"], abs(d)) if abs(d) >= 60 else " — равны")
    return {"a": a, "b": b, "lines": lines, "duel": duel,
            "caveat": "О болезнях, заменах в день матча и конфликтах база не знает: это проверяет разбор Claude по новостям и ваши заметки."}
