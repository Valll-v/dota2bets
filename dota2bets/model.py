"""Состояние «на момент времени» и признаки матча.

Один хронологический проход по базе: для каждого матча признаки считаются ДО обновления состояния результатом,
поэтому утечки будущего нет. Все счётчики затухают по времени (полураспад в днях — HALF), командная история
дополнительно взвешивается долей нынешней пятёрки, игравшей в том матче.

Фаза 2b (скилл и исполнение): требовательность героя и остаток игрока на нём, сложность драфта и исполнение
команды, сливы и камбэки по графику золота, решающие карты, LAN против онлайна, фарм относительно базы героя.
"""

import json
import math
import os
import pickle
import time

from . import DATA
from .db import connect
from .learn import logit
from .ratings import Elo, K_TIER, K_DEFAULT, K_PLAYER, expected

DAY = 86400.0
MEAN_ELO = 1500.0
STATE_PATH = os.path.join(DATA, "state.pkl")

HALF = {"form": 30, "meta": 60, "synergy": 120, "counter": 120, "team_hero": 180, "player_hero": 365,
        "stability": 90, "h2h": 365, "demand": 365, "exec": 180, "swing": 365, "decider": 365, "tier": 365,
        "farm": 365, "lane": 365, "pace_all": 45, "pace_team": 120, "pace_hero": 90}   # farm: 120 → 365 по бэктесту
PRIOR = {"form": 5.0, "meta": 10.0, "synergy": 5.0, "counter": 5.0, "team_hero": 5.0, "player_hero": 5.0,
         "swing": 5.0, "decider": 5.0, "tier": 5.0}
HIST_KEEP = 80
SWING = 10000            # преимущество по золоту, которое считаем «большим»
PATCH_KEEP = 1.0         # доля веса героевых счётчиков при смене крупного патча; 0.5 проверено бэктестом 2026-09-29:
                         # log loss 0.6454 против 0.6453 без сброса — затухания по времени достаточно, сброс выключен
BO_MAPS = {0: 1, 1: 3, 2: 5}

PRE_FEATURES = ["elo", "roster_elo", "form", "h2h", "stability", "lineup_change", "rest", "load7", "today",
                "series_lead"]
DRAFT_FEATURES = ["meta", "synergy", "counter", "team_hero", "player_hero", "player_hero_exp"]
PRE_2B = ["throw", "comeback", "decider", "tier_res", "farm", "lane"]
DRAFT_2B = ["player_hero_w", "exec", "complexity"]
ALL_FEATURES = PRE_FEATURES + DRAFT_FEATURES
# тоталы: отклонение от текущего общего уровня (t_base_*), признаки — отклонения команд, героев, уровень игры
TOTAL_FEATURES_PRE = {"kills": ["t_team_k", "t_level", "t_prem", "t_gap"],
                      "dur": ["t_team_d", "t_level", "t_prem", "t_gap"]}
TOTAL_FEATURES_POST = {"kills": TOTAL_FEATURES_PRE["kills"] + ["t_hero_k"],
                       "dur": TOTAL_FEATURES_PRE["dur"] + ["t_hero_d"]}
TOTAL_FEATURES = True
PRE_FEATURES_2B = PRE_FEATURES + PRE_2B
ALL_FEATURES_2B = PRE_FEATURES + PRE_2B + DRAFT_FEATURES + DRAFT_2B
DRAFT_3 = ["ph_perf", "hero_lane"]      # личная игра на герое (GPM против базы героя), сила героев на линии
ALL_FEATURES_3 = ALL_FEATURES_2B + DRAFT_3
# «чисто пик»: только герои — без команд, игроков и их истории на героях
DRAFT_ONLY = ["meta", "synergy", "counter", "hero_lane"]


def decay(v, last, now, half):
    if now <= last:
        return v
    return v * 0.5 ** ((now - last) / (half * DAY))


class Counter:
    """key -> [games, wins, last_ts], всё с экспоненциальным затуханием."""

    def __init__(self, half):
        self.half = half
        self.d = {}

    def read(self, key, now):
        c = self.d.get(key)
        if not c:
            return 0.0, 0.0
        f = 0.5 ** ((now - c[2]) / (self.half * DAY)) if now > c[2] else 1.0
        return c[0] * f, c[1] * f

    def add(self, key, now, won, weight=1.0):
        c = self.d.get(key)
        if c:
            f = 0.5 ** ((now - c[2]) / (self.half * DAY)) if now > c[2] else 1.0
            c[0] = c[0] * f + weight
            c[1] = c[1] * f + weight * won
            c[2] = max(c[2], now)
        else:
            self.d[key] = [weight, weight * won, now]

    def rate(self, key, now, prior, base=0.5):
        g, w = self.read(key, now)
        return (w + prior * base) / (g + prior), g


class Mean:
    """key -> затухающее среднее: [n, sum, sum2, last_ts]."""

    def __init__(self, half):
        self.half = half
        self.d = {}

    def read(self, key, now):
        c = self.d.get(key)
        if not c:
            return 0.0, 0.0, 0.0
        f = 0.5 ** ((now - c[3]) / (self.half * DAY)) if now > c[3] else 1.0
        return c[0] * f, c[1] * f, c[2] * f

    def add(self, key, now, value, weight=1.0):
        c = self.d.get(key)
        if c:
            f = 0.5 ** ((now - c[3]) / (self.half * DAY)) if now > c[3] else 1.0
            c[0] = c[0] * f + weight
            c[1] = c[1] * f + weight * value
            c[2] = c[2] * f + weight * value * value
            c[3] = max(c[3], now)
        else:
            self.d[key] = [weight, weight * value, weight * value * value, now]

    def mean(self, key, now, prior=0.0, base=0.0):
        n, s, _ = self.read(key, now)
        return (s + prior * base) / (n + prior) if n + prior > 0 else base, n

    def std(self, key, now):
        n, s, s2 = self.read(key, now)
        if n < 2:
            return 0.0
        m = s / n
        return math.sqrt(max(s2 / n - m * m, 0.0))


class State:
    def __init__(self):
        self.team_elo = Elo()
        self.player_elo = Elo()
        self.hist = {}            # team -> [(ts, won, lineup frozenset, series_id)]
        self.last_lineup = {}     # team -> frozenset
        self.hero = Counter(HALF["meta"])
        self.pair = Counter(HALF["synergy"])
        self.counter = Counter(HALF["counter"])
        self.team_hero = Counter(HALF["team_hero"])
        self.player_hero = Counter(HALF["player_hero"])
        self.h2h = Counter(HALF["h2h"])
        self.stability = Counter(HALF["stability"])
        # 2b
        self.hero_demand = Mean(HALF["demand"])     # hero -> разброс остатков игроков на нём
        self.team_exec = Mean(HALF["exec"])         # team -> остаток результата на сложных драфтах (вес = сложность)
        self.lead = Counter(HALF["swing"])          # ('t', team) / ('p', acc) -> игры с лидом ≥ SWING, победы
        self.deficit = Counter(HALF["swing"])       # то же при отставании ≥ SWING
        self.decider = Counter(HALF["decider"])     # ('t', team) / ('p', acc) -> решающие карты
        self.tier = Counter(HALF["tier"])           # (team, tier) -> игры, победы
        self.hero_gpm = Mean(HALF["farm"])          # hero -> средний GPM
        self.player_farm = Mean(HALF["farm"])       # acc -> остаток GPM относительно героя
        self.hero_lane = Mean(HALF["lane"])         # hero -> средний (золото + опыт) на 10-й минуте
        self.player_lane = Mean(HALF["lane"])       # acc -> остаток (золото + опыт) на 10-й минуте относительно героя
        self.player_hero_perf = Mean(HALF["player_hero"])   # (acc, hero) -> GPM игрока на герое минус база героя
        self.g_lane = Mean(HALF["lane"])            # 'all' -> общий уровень золота + опыта к 10-й минуте
        self.canon = {}           # team_id -> канонический id (история следует за пятёркой при смене тега)
        self.player_team = {}     # acc -> (канонический team_id, ts) последнего матча
        self.last_patch = None    # крупный патч последнего матча: при смене героевые счётчики режутся
        self.syn_level = Mean(HALF["exec"])         # 'all' -> средняя сумма синергий драфтов (для центрирования сложности)
        # темп: суммарные килы и длительность (минуты)
        self.g_kills = Mean(HALF["pace_all"])       # 'all' -> общий уровень (следит за патчем)
        self.g_dur = Mean(HALF["pace_all"])
        self.team_kills = Mean(HALF["pace_team"])   # канонический team -> килы в картах команды
        self.team_dur = Mean(HALF["pace_team"])
        self.hero_kills = Mean(HALF["pace_hero"])   # hero -> килы в картах с героем
        self.hero_dur = Mean(HALF["pace_hero"])
        self.built_ts = 0
        self.n_matches = 0

    # ---------------------------------------------------------- вспомогательное

    def hero_logit(self, h, now):
        p, _ = self.hero.rate(h, now, PRIOR["meta"])
        return logit(p)

    def synergy(self, h1, h2, now):
        p, g = self.pair.rate((min(h1, h2), max(h1, h2)), now, PRIOR["synergy"])
        return logit(p) - (self.hero_logit(h1, now) + self.hero_logit(h2, now)) / 2.0

    def counter_logit(self, a, b, now):
        p, g = self.counter.rate((a, b), now, PRIOR["counter"])
        return logit(p) - (self.hero_logit(a, now) - self.hero_logit(b, now))

    def player_residual(self, acc, h, now):
        """Остаток игрока на герое (логит) и число игр."""
        base = self.hero_logit(h, now)
        p, g = self.player_hero.rate((acc, h), now, PRIOR["player_hero"])
        return (logit(p) - base if g > 0 else 0.0), g

    def canonical(self, team, lineup, now):
        """Канонический id команды: если четверо и больше из пятёрки последним играли за другой id — история там."""
        if team in self.canon:
            return self.canon[team]
        if lineup and len(lineup) >= 4:
            votes = {}
            for p in lineup:
                last = self.player_team.get(p)
                if last and now - last[1] < 365 * DAY:
                    votes[last[0]] = votes.get(last[0], 0) + 1
            for other, n in votes.items():
                if other != team and n >= 4:
                    root = self.canon.get(other, other)
                    self.canon[team] = root
                    return root
        return team

    def demand(self, h, now):
        return self.hero_demand.std(h, now)

    def complexity(self, syn, now):
        """Сложность драфта: насколько сумма синергий выше среднего уровня драфтов (не меньше 0)."""
        level, n = self.syn_level.mean("all", now)
        return max(syn - level, 0.0) if n >= 20 else 0.0

    def team_form(self, team, lineup, now):
        g = w = 0.0
        for ts, won, lu, _ in self.hist.get(team, ()):
            f = decay(1.0, ts, now, HALF["form"])
            ov = len(lu & lineup) / 5.0 if lineup else 1.0
            g += f * ov
            w += f * ov * won
        return (w + PRIOR["form"] * 0.5) / (g + PRIOR["form"]), g

    def swing_rates(self, team, lineup, now):
        """Доля побед при большом лиде (1 − слив) и при большом отставании (камбэк): среднее команды и пятёрки."""
        def rate(counter, key, base):
            p, g = counter.rate(key, now, PRIOR["swing"], base)
            return p
        base_lead, base_def = 0.9, 0.1
        lead_t = rate(self.lead, ("t", team), base_lead)
        def_t = rate(self.deficit, ("t", team), base_def)
        if lineup:
            lead_p = sum(rate(self.lead, ("p", p), base_lead) for p in lineup) / len(lineup)
            def_p = sum(rate(self.deficit, ("p", p), base_def) for p in lineup) / len(lineup)
        else:
            lead_p, def_p = lead_t, def_t
        return (lead_t + lead_p) / 2.0, (def_t + def_p) / 2.0

    def decider_res(self, team, lineup, now, form):
        p_t, g_t = self.decider.rate(("t", team), now, PRIOR["decider"], form)
        if lineup:
            p_p = sum(self.decider.rate(("p", p), now, PRIOR["decider"], form)[0] for p in lineup) / len(lineup)
        else:
            p_p = p_t
        return (p_t + p_p) / 2.0 - form

    def tier_res(self, team, tier, now, form):
        p, g = self.tier.rate((team, tier), now, PRIOR["tier"], form)
        return p - form

    def lane(self, lineup, now):
        """Средний по пятёрке остаток золота+опыта на 10-й минуте относительно базы героев (/1000)."""
        if not lineup:
            return 0.0
        return sum(self.player_lane.mean(p, now, prior=5.0)[0] for p in lineup) / len(lineup) / 1000.0

    def farm(self, lineup, now):
        if not lineup:
            return 0.0
        return sum(self.player_farm.mean(p, now, prior=5.0)[0] for p in lineup) / len(lineup) / 100.0

    def pace(self, a, b, now, tier, roster_a, roster_b, heroes=None):
        """Признаки тоталов: общий уровень и отклонения команд и героев от него."""
        base_k, nk = self.g_kills.mean("all", now, prior=20.0, base=58.0)
        base_d, nd = self.g_dur.mean("all", now, prior=20.0, base=35.0)
        tk = td = 0.0
        for t in (a, b):
            tk += self.team_kills.mean(t, now, prior=8.0, base=base_k)[0] - base_k
            td += self.team_dur.mean(t, now, prior=8.0, base=base_d)[0] - base_d
        hk = hd = 0.0
        if heroes:
            for h in heroes:
                hk += self.hero_kills.mean(h, now, prior=20.0, base=base_k)[0] - base_k
                hd += self.hero_dur.mean(h, now, prior=20.0, base=base_d)[0] - base_d
            hk /= len(heroes)
            hd /= len(heroes)
        return {"t_base_k": base_k, "t_base_d": base_d, "t_team_k": tk, "t_team_d": td, "t_hero_k": hk,
                "t_hero_d": hd, "t_level": ((roster_a + roster_b) / 2.0 - MEAN_ELO) / 400.0,
                "t_prem": 1.0 if tier == "premium" else 0.0, "t_gap": abs(roster_a - roster_b) / 400.0}

    def team_side(self, team, lineup, now, series_id, tier=None, bo=None):
        """Признаки одной стороны, не зависящие от драфта."""
        h = self.hist.get(team, ())
        last_ts = h[-1][0] if h else None
        days = (now - last_ts) / DAY if last_ts else 60.0
        load7 = sum(1 for ts, _, _, _ in h if now - ts < 7 * DAY)
        day0 = now - (now % DAY)
        today = sum(1 for ts, _, _, _ in h if day0 <= ts < now)
        lead = 0
        played = 0
        if series_id:
            for ts, won, _, sid in reversed(h):
                if sid != series_id or now - ts > 2 * DAY:
                    break
                lead += 1 if won else -1
                played += 1
        form, form_g = self.team_form(team, lineup, now)
        stab, _ = self.stability.read((team, lineup), now)
        prev = self.last_lineup.get(team)
        change = 1.0 - len(prev & lineup) / 5.0 if (prev and lineup) else 0.0
        elo = self.team_elo.get(team, now)
        roster = sum(self.player_elo.get(p, now) for p in lineup) / len(lineup) if lineup else elo
        lead_rate, comeback_rate = self.swing_rates(team, lineup, now)
        is_decider = 1.0 if (bo and bo > 1 and played == bo - 1 and lead == 0) else 0.0
        return {"elo": elo, "roster_elo": roster, "form": form, "form_g": form_g, "stability": stab,
                "lineup_change": change, "rest": min(math.log1p(days), 4.0), "load7": load7, "today": today,
                "series_lead": lead, "played": played, "is_decider": is_decider,
                "throw": 1.0 - lead_rate, "comeback": comeback_rate,
                "decider": self.decider_res(team, lineup, now, form),
                "tier_res": self.tier_res(team, tier, now, form) if tier == "premium" else 0.0,
                "farm": self.farm(lineup, now), "lane": self.lane(lineup, now)}

    def draft_side(self, team, lineup_heroes, now):
        """lineup_heroes: список (account_id, hero_id)."""
        heroes = [h for _, h in lineup_heroes if h]
        meta = sum(self.hero_logit(h, now) for h in heroes)
        syn = 0.0
        for i in range(len(heroes)):
            for j in range(i + 1, len(heroes)):
                syn += self.synergy(heroes[i], heroes[j], now)
        th = ph = ph_exp = ph_w = perf = hlane = 0.0
        demands = []
        lane_all, lane_n = self.g_lane.mean("all", now)
        for acc, h in lineup_heroes:
            if not h:
                continue
            base = self.hero_logit(h, now)
            p, g = self.team_hero.rate((team, h), now, PRIOR["team_hero"])
            th += logit(p) - base if g > 0 else 0.0
            d = self.demand(h, now)
            demands.append(d)
            if acc:
                r, g = self.player_residual(acc, h, now)
                ph += r
                ph_exp += math.log1p(g)
                ph_w += r * d
                perf += self.player_hero_perf.mean((acc, h), now, prior=4.0)[0] / 100.0
            hl, hn = self.hero_lane.mean(h, now)
            if hn >= 5 and lane_n >= 50:
                hlane += (hl - lane_all) / 1000.0
        complexity = self.complexity(syn, now)
        ex, _ = self.team_exec.mean(team, now, prior=3.0)
        return {"meta": meta, "synergy": syn, "team_hero": th, "player_hero": ph, "player_hero_exp": ph_exp,
                "player_hero_w": ph_w, "ph_perf": perf, "hero_lane": hlane, "demand": demands, "complexity": complexity, "exec": ex,
                "exec_x": ex * complexity, "heroes": heroes}

    # ---------------------------------------------------------- признаки матча

    def features(self, a, b, lineup_a, lineup_b, now, series_id=None, draft_a=None, draft_b=None, tier=None, bo=None):
        """Разности A − B (A = Radiant при обучении). draft_*: список (account_id, hero_id) или None."""
        la, lb = frozenset(lineup_a or ()), frozenset(lineup_b or ())
        a_id, b_id = a, b
        a, b = self.canonical(a, la, now), self.canonical(b, lb, now)
        sa = self.team_side(a, la, now, series_id, tier, bo)
        sb = self.team_side(b, lb, now, series_id, tier, bo)
        dec = max(sa["is_decider"], sb["is_decider"])
        f = {"elo": (sa["elo"] - sb["elo"]) / 400.0,
             "roster_elo": (sa["roster_elo"] - sb["roster_elo"]) / 400.0,
             "form": sa["form"] - sb["form"],
             "stability": math.log1p(sa["stability"]) - math.log1p(sb["stability"]),
             "lineup_change": sa["lineup_change"] - sb["lineup_change"],
             "rest": sa["rest"] - sb["rest"],
             "load7": float(sa["load7"] - sb["load7"]),
             "today": float(sa["today"] - sb["today"]),
             "series_lead": float(sa["series_lead"]),
             "throw": sa["throw"] - sb["throw"],
             "comeback": sa["comeback"] - sb["comeback"],
             "decider": dec * (sa["decider"] - sb["decider"]),
             "tier_res": sa["tier_res"] - sb["tier_res"],
             "farm": sa["farm"] - sb["farm"],
             "lane": sa["lane"] - sb["lane"]}
        key = (min(a, b), max(a, b))
        g, w_low = self.h2h.read(key, now)
        w_a = w_low if a == key[0] else g - w_low
        f["h2h"] = (2 * w_a - g) / (g + 2.0)
        detail = {"a": sa, "b": sb, "h2h": (g, w_a), "canon_a": a if a != a_id else None, "canon_b": b if b != b_id else None}
        heroes = ([h for _, h in draft_a if h] + [h for _, h in draft_b if h]) if (draft_a and draft_b) else None
        f.update(self.pace(a, b, now, tier, sa["roster_elo"], sb["roster_elo"], heroes))
        if draft_a and draft_b:
            da, db = self.draft_side(a, draft_a, now), self.draft_side(b, draft_b, now)
            ctr = 0.0
            for ha in da["heroes"]:
                for hb in db["heroes"]:
                    ctr += self.counter_logit(ha, hb, now)
            f.update({"meta": da["meta"] - db["meta"], "synergy": da["synergy"] - db["synergy"], "counter": ctr,
                      "team_hero": da["team_hero"] - db["team_hero"],
                      "player_hero": da["player_hero"] - db["player_hero"],
                      "player_hero_exp": da["player_hero_exp"] - db["player_hero_exp"],
                      "player_hero_w": da["player_hero_w"] - db["player_hero_w"],
                      "exec": da["exec_x"] - db["exec_x"],
                      "complexity": da["complexity"] - db["complexity"],
                      "ph_perf": da["ph_perf"] - db["ph_perf"],
                      "hero_lane": da["hero_lane"] - db["hero_lane"]})
            detail["draft_a"], detail["draft_b"] = da, db
        return f, detail

    # ---------------------------------------------------------- обновление

    def update(self, m, lineup_a, lineup_b, draft_a, draft_b, stats_a=None, stats_b=None):
        """m: dict матча (radiant = A). Вызывать после features(). stats_*: список (account_id, hero_id, gpm)."""
        now = m["start_time"]
        la0, lb0 = frozenset(lineup_a or ()), frozenset(lineup_b or ())
        a = self.canonical(m["radiant_team_id"], la0, now)
        b = self.canonical(m["dire_team_id"], lb0, now)
        won_a = 1.0 if m["radiant_win"] else 0.0
        patch = m.get("patch")
        if patch and self.last_patch and patch != self.last_patch:
            for c in (self.hero, self.pair, self.counter, self.team_hero):
                for v in c.d.values():
                    v[0] *= PATCH_KEEP
                    v[1] *= PATCH_KEEP
        if patch:
            self.last_patch = patch
        k = K_TIER.get(m["tier"], K_DEFAULT)
        ra, rb = self.team_elo.get(a, now), self.team_elo.get(b, now)
        e = expected(ra, rb)
        self.team_elo.update(a, k * (won_a - e), now)
        self.team_elo.update(b, -k * (won_a - e), now)
        la, lb = frozenset(lineup_a or ()), frozenset(lineup_b or ())
        ep = None
        if len(la) == 5 and len(lb) == 5:
            roster_a = sum(self.player_elo.get(p, now) for p in la) / 5.0
            roster_b = sum(self.player_elo.get(p, now) for p in lb) / 5.0
            ep = expected(roster_a, roster_b)
            for p in la:
                self.player_elo.update(p, K_PLAYER * (won_a - ep), now)
            for p in lb:
                self.player_elo.update(p, -K_PLAYER * (won_a - ep), now)
        # решающая карта и тир — до записи матча в историю
        bo = BO_MAPS.get(m.get("series_type"), 1)
        sid = m.get("series_id")
        sides = ((a, la, won_a), (b, lb, 1.0 - won_a))
        for team, lu, won in sides:
            h = self.hist.get(team, ())
            played, lead = 0, 0
            if sid:
                for ts, w, _, s in reversed(h):
                    if s != sid or now - ts > 2 * DAY:
                        break
                    played += 1
                    lead += 1 if w else -1
            if bo > 1 and played == bo - 1 and lead == 0:
                self.decider.add(("t", team), now, won)
                for p in lu:
                    self.decider.add(("p", p), now, won)
            if m.get("tier"):
                self.tier.add((team, m["tier"]), now, won)
        gold = m.get("gold_adv")
        if gold:
            mx, mn = max(gold), min(gold)
            if mx >= SWING:
                self.lead.add(("t", a), now, won_a)
                self.deficit.add(("t", b), now, 1.0 - won_a)
                for p in la:
                    self.lead.add(("p", p), now, won_a)
                for p in lb:
                    self.deficit.add(("p", p), now, 1.0 - won_a)
            if mn <= -SWING:
                self.lead.add(("t", b), now, 1.0 - won_a)
                self.deficit.add(("t", a), now, won_a)
                for p in lb:
                    self.lead.add(("p", p), now, 1.0 - won_a)
                for p in la:
                    self.deficit.add(("p", p), now, won_a)
        for team, lu, won in sides:
            h = self.hist.setdefault(team, [])
            h.append((now, won, lu, sid))
            if len(h) > HIST_KEEP:
                del h[0]
            if lu:
                self.stability.add((team, lu), now, won)
                self.last_lineup[team] = lu
        key = (min(a, b), max(a, b))
        self.h2h.add(key, now, won_a if a == key[0] else 1.0 - won_a)
        if draft_a and draft_b:
            ha = [h for _, h in draft_a if h]
            hb = [h for _, h in draft_b if h]
            for heroes, team, lineup_heroes, won in ((ha, a, draft_a, won_a), (hb, b, draft_b, 1.0 - won_a)):
                # исполнение: остаток результата относительно roster-Elo, вес — сложность драфта
                syn = 0.0
                for i in range(len(heroes)):
                    for j in range(i + 1, len(heroes)):
                        syn += self.synergy(heroes[i], heroes[j], now)
                complexity = self.complexity(syn, now)
                self.syn_level.add("all", now, syn)
                if ep is not None and complexity > 0:
                    exp_t = ep if team == a else 1.0 - ep
                    self.team_exec.add(team, now, won - exp_t, weight=complexity)
                for acc, h in lineup_heroes:
                    if acc and h:
                        r, g = self.player_residual(acc, h, now)
                        if g >= 3:
                            self.hero_demand.add(h, now, r)
                        self.player_hero.add((acc, h), now, won)
                for h in heroes:
                    self.hero.add(h, now, won)
                    self.team_hero.add((team, h), now, won)
                for i in range(len(heroes)):
                    for j in range(i + 1, len(heroes)):
                        self.pair.add((min(heroes[i], heroes[j]), max(heroes[i], heroes[j])), now, won)
            for x in ha:
                for y in hb:
                    self.counter.add((x, y), now, won_a)
                    self.counter.add((y, x), now, 1.0 - won_a)
        for stats in (stats_a, stats_b):
            for acc, h, gpm, lane10 in stats or ():
                if not (acc and h):
                    continue
                if gpm:
                    base, n = self.hero_gpm.mean(h, now, prior=0.0, base=0.0)
                    if n >= 5:
                        self.player_farm.add(acc, now, gpm - base)
                        self.player_hero_perf.add((acc, h), now, gpm - base)
                    self.hero_gpm.add(h, now, gpm)
                if lane10:
                    self.g_lane.add("all", now, lane10)
                    base, n = self.hero_lane.mean(h, now, prior=0.0, base=0.0)
                    if n >= 5:
                        self.player_lane.add(acc, now, lane10 - base)
                    self.hero_lane.add(h, now, lane10)
        kills = (m.get("radiant_score") or 0) + (m.get("dire_score") or 0)
        dur = (m.get("duration") or 0) / 60.0
        if kills > 0 and dur > 10:
            self.g_kills.add("all", now, kills)
            self.g_dur.add("all", now, dur)
            for t in (a, b):
                self.team_kills.add(t, now, kills)
                self.team_dur.add(t, now, dur)
            if draft_a and draft_b:
                for _, h in list(draft_a) + list(draft_b):
                    if h:
                        self.hero_kills.add(h, now, kills)
                        self.hero_dur.add(h, now, dur)
        for p in la:
            self.player_team[p] = (a, now)
        for p in lb:
            self.player_team[p] = (b, now)
        self.n_matches += 1
        self.built_ts = max(self.built_ts, now)


# ---------------------------------------------------------------- проход по базе

def load_matches(conn):
    """Матчи с составами: список dict, отсортированный по времени."""
    rows = conn.execute(
        "select match_id, start_time, radiant_win, tier, radiant_team_id, dire_team_id, series_id, series_type, gold_adv, patch, "
        "radiant_score, dire_score, duration "
        "from matches where radiant_team_id is not null and dire_team_id is not null and radiant_win is not null "
        "order by start_time, match_id").fetchall()
    players = {}
    for r in conn.execute("select match_id, is_radiant, account_id, hero_id, gpm, gold10, xp10 from match_players order by match_id, player_slot"):
        lane10 = (r[5] or 0) + (r[6] or 0) if (r[5] is not None or r[6] is not None) else None
        players.setdefault((r[0], r[1]), []).append((r[2], r[3], r[4], lane10))
    out = []
    for r in rows:
        m = dict(r)
        m["gold_adv"] = json.loads(m["gold_adv"]) if m["gold_adv"] else None
        pa = players.get((m["match_id"], 1), [])
        pb = players.get((m["match_id"], 0), [])
        m["players_a"] = [(acc, h) for acc, h, _, _ in pa]
        m["players_b"] = [(acc, h) for acc, h, _, _ in pb]
        m["stats_a"], m["stats_b"] = pa, pb
        out.append(m)
    return out


def run_pass(matches, on_features=None, log=None):
    """Проход: для каждого матча признаки → callback → обновление. Возвращает состояние."""
    st = State()
    t0 = time.time()
    for i, m in enumerate(matches):
        pa, pb = m["players_a"], m["players_b"]
        la = [acc for acc, _ in pa if acc]
        lb = [acc for acc, _ in pb if acc]
        full = len(pa) == 5 and len(pb) == 5 and all(h for _, h in pa + pb)
        f, _ = st.features(m["radiant_team_id"], m["dire_team_id"], la, lb, m["start_time"], m.get("series_id"),
                           pa if full else None, pb if full else None, tier=m.get("tier"),
                           bo=BO_MAPS.get(m.get("series_type"), 1))
        if on_features:
            on_features(m, f)
        st.update(m, la, lb, pa if full else None, pb if full else None, m.get("stats_a"), m.get("stats_b"))
        if log and (i + 1) % 10000 == 0:
            log("проход: %d матчей, %.0f с" % (i + 1, time.time() - t0))
    return st


def save_state(st, path=STATE_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(st, f, protocol=pickle.HIGHEST_PROTOCOL)


def load_state(path=STATE_PATH):
    with open(path, "rb") as f:
        return pickle.load(f)
