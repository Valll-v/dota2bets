"""Прогноз на матч «сейчас»: состояние + модели → вероятность с разбивкой."""

import math
import threading
import time

from .backtest import load_models
from .cli_data import actual_roster, hero_names, player_names
from .model import load_state


class Predictor:
    def __init__(self, conn):
        self._local = threading.local()      # SQLite-соединение своё на каждый поток сервера
        self._local.conn = conn
        self.state = load_state()
        self.models = load_models(conn)
        if "pre" not in self.models or "post" not in self.models:
            raise RuntimeError("модели не обучены: python -m dota2bets train")
        self.hero_names = hero_names(conn)
        self.player_names = player_names(conn)

    @property
    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            from .db import connect
            c = self._local.conn = connect()
        return c

    def lineup(self, team_id, override=None):
        if override:
            return list(override)
        roster, _ = actual_roster(self.conn, team_id)
        return roster

    def assign_heroes(self, lineup, heroes, now):
        """Назначить героев игрокам по опыту (жадно). heroes — список hero_id (может быть короче 5)."""
        if not heroes:
            return []
        pairs = []
        for acc in lineup:
            for h in heroes:
                _, g = self.state.player_hero.read((acc, h), now)
                pairs.append((g, acc, h))
        pairs.sort(reverse=True)
        used_acc, used_h, out = set(), set(), []
        for g, acc, h in pairs:
            if acc in used_acc or h in used_h:
                continue
            out.append((acc, h))
            used_acc.add(acc)
            used_h.add(h)
        for h in heroes:
            if h not in used_h:
                out.append((None, h))
        return out

    def predict(self, team_a, team_b, lineup_a=None, lineup_b=None, heroes_a=None, heroes_b=None,
                side="unknown", now=None, series_id=None, assign_a=None, assign_b=None, tier="professional", bo=1):
        """side: 'radiant' (A на Radiant), 'dire' или 'unknown' (среднее по сторонам).
        assign_*: явный список (account_id, hero_id); иначе герои назначаются по опыту."""
        now = now or int(time.time())
        la, lb = self.lineup(team_a, lineup_a), self.lineup(team_b, lineup_b)
        da = assign_a or (self.assign_heroes(la, heroes_a, now) if heroes_a else None)
        db = assign_b or (self.assign_heroes(lb, heroes_b, now) if heroes_b else None)
        full_draft = bool(da and db and len(da) == 5 and len(db) == 5)
        kw = dict(tier=tier, bo=bo)
        f_ab, det_ab = self.state.features(team_a, team_b, la, lb, now, series_id, da if full_draft else None,
                                           db if full_draft else None, **kw)
        model = self.models["post" if full_draft else "pre"]
        names = model.names
        x_ab = [f_ab.get(n, 0.0) for n in names]
        p_rad = model.predict(x_ab)
        # A на Dire: признаки B−A, вероятность победы B (Radiant) → 1 − p
        f_ba, _ = self.state.features(team_b, team_a, lb, la, now, series_id, db if full_draft else None,
                                      da if full_draft else None, **kw)
        p_dire = 1.0 - model.predict([f_ba.get(n, 0.0) for n in names])
        if side == "radiant":
            p = p_rad
        elif side == "dire":
            p = p_dire
        else:
            p = (p_rad + p_dire) / 2.0
        contrib = model.contributions(x_ab)
        draft_only = None
        dm = self.models.get("draft")
        if full_draft and dm is not None:        # чисто пик: те же герои, любые команды и игроки
            d_rad = dm.predict([f_ab.get(n, 0.0) for n in dm.names])
            d_dire = 1.0 - dm.predict([f_ba.get(n, 0.0) for n in dm.names])
            d_p = d_rad if side == "radiant" else (d_dire if side == "dire" else (d_rad + d_dire) / 2.0)
            draft_only = {"p": d_p, "p_radiant": d_rad, "p_dire": d_dire,
                          "contributions": dm.contributions([f_ab.get(n, 0.0) for n in dm.names])}
        warnings = []
        from .cli_data import departed
        for label, tid, lu in (("A", team_a, la), ("B", team_b, lb)):
            gone = departed(self.conn, tid, lu)
            if gone:
                warnings.append("%s: %d из пятёрки с тех пор играли за другие команды (%s) — состав устарел, замены в базе нет" % (
                    label, len(gone), ", ".join("%s → %s" % (self.player_names.get(p, p), g[1]) for p, g in gone.items())))
            if len(lu) < 5:
                warnings.append("%s: в базе меньше пяти игроков состава" % label)
        return {
            "warnings": warnings,
            "p": p, "p_radiant": p_rad, "p_dire": p_dire, "side": side, "mode": "post" if full_draft else "pre",
            "features": f_ab, "contributions": contrib, "detail": det_ab, "draft_only": draft_only,
            "lineup_a": la, "lineup_b": lb, "draft_a": da, "draft_b": db, "tier": tier, "bo": bo,
        }

    def describe(self, res, name_a, name_b):
        """Текстовая разбивка для консоли и досье."""
        lines = ["%s vs %s: p(%s) = %.1f%%  (Radiant %.1f%% / Dire %.1f%%), модель %s" % (
            name_a, name_b, name_a, 100 * res["p"], 100 * res["p_radiant"], 100 * res["p_dire"], res["mode"])]
        d = res["detail"]
        for label, cid in (("A", d.get("canon_a")), ("B", d.get("canon_b"))):
            if cid:
                lines.append("  %s: пятёрка последним играла под id %d — история, форма и h2h считаются по нему" % (label, cid))
        for label, s in (("A", d["a"]), ("B", d["b"])):
            lines.append("  %s: Elo %.0f, roster-Elo %.0f, форма %.2f (вес %.1f), стабильность %.1f, смена состава %.0f%%, "
                         "отдых %.0f дн, карт за 7 дн %d, сегодня %d, счёт в серии %+d" % (
                             label, s["elo"], s["roster_elo"], s["form"], s["form_g"], s["stability"],
                             100 * s["lineup_change"], math.expm1(s["rest"]), s["load7"], s["today"], s["series_lead"]))
            lines.append("     сливы лида %.0f%%, камбэки %.0f%%, решающие карты %+.2f, LAN-остаток %+.2f, фарм %+.0f GPM, лейнинг %+.0f к 10 мин" % (
                100 * s["throw"], 100 * s["comeback"], s["decider"], s["tier_res"], 100 * s["farm"], 1000 * s["lane"]))
        g, wa = d["h2h"]
        lines.append("  h2h (вес): %.1f из %.1f за A" % (wa, g))
        if "draft_a" in d:
            for label, s in (("A", d["draft_a"]), ("B", d["draft_b"])):
                lines.append("  драфт %s: %s | мета %+.2f, синергия %+.2f, комфорт команды %+.2f, комфорт игроков %+.2f "
                             "(с учётом требовательности %+.2f), опыт %.1f, сложность %.2f, исполнение %+.2f" % (
                                 label, ", ".join(self.hero_names.get(h, str(h)) for h in s["heroes"]), s["meta"],
                                 s["synergy"], s["team_hero"], s["player_hero"], s["player_hero_w"], s["player_hero_exp"],
                                 s["complexity"], s["exec"]))
            lines.append("  контры A против B: %+.2f" % res["features"]["counter"])
        do = res.get("draft_only")
        if do:
            c = do["contributions"]
            lines.append("  чисто пик, без команд и игроков: p(%s) = %.1f%% (Radiant %.1f%% / Dire %.1f%%); "
                         "мета %+.2f, синергии %+.2f, контры %+.2f, линии %+.2f" % (
                             name_a, 100 * do["p"], 100 * do["p_radiant"], 100 * do["p_dire"], c.get("meta", 0.0),
                             c.get("synergy", 0.0), c.get("counter", 0.0), c.get("hero_lane", 0.0)))
        c = res["contributions"]
        top = sorted(((k, v) for k, v in c.items() if k != "intercept"), key=lambda kv: -abs(kv[1]))
        lines.append("  вклад в логит: " + ", ".join("%s %+.2f" % (k, v) for k, v in top[:8]) + "; сторона %+.2f" % c["intercept"])
        return "\n".join(lines)
