"""Вероятность победы по ходу карты: предматчевая оценка + преимущество по золоту на текущей минуте.

Для каждой минуты из MINUTES своя логистическая регрессия на двух признаках: логит предматчевой вероятности
(прогноз walk-forward, то есть «как в тот день») и перевес Radiant по золоту в тысячах. Между минутами —
линейная интерполяция. Обучение на картах до CUT_DAYS дней назад, проверка на более свежих.
"""

import json
import time

from .db import connect
from .learn import LogReg, logit, metrics, sigmoid

MINUTES = (5, 10, 15, 20, 25, 30, 35, 40, 45, 50)
CUT_DAYS = 150
DAY = 86400


def rows_for(conn, minute):
    out = []
    for r in conn.execute(
            "select m.start_time, m.radiant_win, m.gold_adv, m.duration, w.p_post, w.p_pre from matches m "
            "join wf_pred w on w.match_id = m.match_id where m.gold_adv is not null and m.duration > ?", (minute * 60 + 60,)):
        g = json.loads(r["gold_adv"])
        if len(g) <= minute:
            continue
        p = r["p_post"] if r["p_post"] is not None else r["p_pre"]
        if p is None:
            continue
        out.append((r["start_time"], [logit(p), g[minute] / 1000.0], 1 if r["radiant_win"] else 0, p))
    return out


def train(conn=None, log=None):
    conn = conn or connect()
    cut = int(time.time()) - CUT_DAYS * DAY
    models, report = {}, {}
    for m in MINUTES:
        rows = rows_for(conn, m)
        tr = [r for r in rows if r[0] < cut]
        te = [r for r in rows if r[0] >= cut]
        if len(tr) < 1000:
            continue
        model = LogReg(["pregame", "gold_k"], l2=1.0).fit([r[1] for r in tr], [r[2] for r in tr])
        if te:
            ps = [model.predict(r[1]) for r in te]
            report[m] = {"n": len(te), "model": metrics(ps, [r[2] for r in te]),
                         "pregame_only": metrics([r[3] for r in te], [r[2] for r in te])}
        full = LogReg(["pregame", "gold_k"], l2=1.0).fit([r[1] for r in rows], [r[2] for r in rows])
        models[m] = full.to_dict()
        if log and m in report:
            log("минута %d: log loss %.4f против %.4f без золота (%d карт)" % (
                m, report[m]["model"]["logloss"], report[m]["pregame_only"]["logloss"], report[m]["n"]))
    conn.execute("insert or replace into model_coefs(name, payload, trained) values (?,?,?)",
                 ("ingame", json.dumps({"models": {str(k): v for k, v in models.items()}, "report": {str(k): v for k, v in report.items()}}),
                  int(time.time())))
    conn.commit()
    return report


def load(conn):
    r = conn.execute("select payload from model_coefs where name = 'ingame'").fetchone()
    if not r:
        return None
    d = json.loads(r["payload"])
    return {"models": {int(k): LogReg.from_dict(v) for k, v in d["models"].items()}, "report": d.get("report", {})}


def predict(bundle, p_pregame, minute, gold_lead):
    """p победы команды, для которой задан gold_lead (её перевес по золоту, может быть отрицательным),
    при её предматчевой вероятности p_pregame."""
    ms = sorted(bundle["models"])
    if not ms:
        return p_pregame
    x = [logit(p_pregame), gold_lead / 1000.0]
    if minute <= 1:
        return p_pregame
    if minute < ms[0]:
        w = minute / float(ms[0])                      # до первой модели — от предматчевой оценки к модели 5-й минуты
        return sigmoid((1 - w) * logit(p_pregame) + w * logit(bundle["models"][ms[0]].predict(x)))
    if minute >= ms[-1]:
        return bundle["models"][ms[-1]].predict(x)
    lo = max(m for m in ms if m <= minute)
    hi = min(m for m in ms if m >= minute)
    if lo == hi:
        return bundle["models"][lo].predict(x)
    w = (minute - lo) / float(hi - lo)
    return sigmoid((1 - w) * logit(bundle["models"][lo].predict(x)) + w * logit(bundle["models"][hi].predict(x)))


def table(bundle, p_pregame, minute):
    """Вероятность при разных перевесах на заданной минуте — для быстрой оценки live-кэфа."""
    return [{"lead": g, "p": predict(bundle, p_pregame, minute, g)} for g in (-15000, -10000, -5000, -2000, 0, 2000, 5000, 10000, 15000)]
