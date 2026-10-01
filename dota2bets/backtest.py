"""Walk-forward обучение и бэктест, финальная модель в model_coefs, состояние в data/state.pkl.

Сравниваются наборы признаков: базовый (M2) и расширенный (2b). В model_coefs под именами pre/post сохраняется
набор с лучшим log loss на тестовых окнах; отчёт показывает все варианты.
"""

import json
import os
import time

from . import DATA
from .db import connect
from .learn import LogReg, LinReg, metrics, calibration, p_over
from .model import (ALL_FEATURES, PRE_FEATURES, ALL_FEATURES_2B, ALL_FEATURES_3, PRE_FEATURES_2B, TOTAL_FEATURES_PRE,
                    TOTAL_FEATURES_POST, DRAFT_ONLY, load_matches, run_pass, save_state)
from .ratings import expected

REPORT_PATH = os.path.join(DATA, "backtest.json")
WARMUP = 1717200000        # 2024-06-01: до этого только копим состояние
STEP_DAYS = 61             # переобучение раз в два месяца
L2 = 2.0

VARIANTS = {"pre": PRE_FEATURES, "pre_2b": PRE_FEATURES_2B, "post": ALL_FEATURES, "post_2b": ALL_FEATURES_2B,
            "post_3": ALL_FEATURES_3}


def vec(f, names):
    return [f.get(n, 0.0) for n in names]


def month_of(ts):
    return time.strftime("%Y-%m", time.gmtime(ts))


def walk_forward(rows, names, log=None):
    """rows: (ts, y, f, match_id). Возвращает (p, y, ts, p_elo, match_id) по тестовым окнам."""
    rows = [r for r in rows if all(n in r[2] for n in names)]
    ps, ys, ts_out, base, ids = [], [], [], [], []
    start = WARMUP
    end = rows[-1][0] + 1
    while start < end:
        stop = start + STEP_DAYS * 86400
        train = [r for r in rows if r[0] < start]
        test = [r for r in rows if start <= r[0] < stop]
        if test and len(train) >= 500:
            model = LogReg(names, l2=L2).fit([vec(r[2], names) for r in train], [r[1] for r in train])
            for r in test:
                ps.append(model.predict(vec(r[2], names)))
                ys.append(r[1])
                ts_out.append(r[0])
                base.append(expected(r[2]["elo"] * 400.0, 0.0))
                ids.append(r[3] if len(r) > 3 else None)
        start = stop
    if log:
        log("walk-forward %s: %s" % ("+".join(names[-3:]), metrics(ps, ys)))
    return ps, ys, ts_out, base, ids


def evaluate(rows, names, log=None):
    ps, ys, tss, base, ids = walk_forward(rows, names, log)
    by_month = {}
    for p, y, ts in zip(ps, ys, tss):
        by_month.setdefault(month_of(ts), ([], []))
        by_month[month_of(ts)][0].append(p)
        by_month[month_of(ts)][1].append(y)
    full = [r for r in rows if all(n in r[2] for n in names)]
    model = LogReg(names, l2=L2).fit([vec(r[2], names) for r in full], [r[1] for r in full])
    return {
        "features": names,
        "overall": metrics(ps, ys),
        "baseline_elo": metrics(base, ys),
        "baseline_half": metrics([0.5] * len(ys), ys),
        "calibration": calibration(ps, ys),
        "by_month": {k: metrics(*v) for k, v in sorted(by_month.items())},
        "coefs": dict(zip(["intercept"] + names, [round(w, 4) for w in model.w])),
    }, model, {i: (p, t) for i, p, t in zip(ids, ps, tss) if i is not None}


def evaluate_totals(rows, kind, names, log=None):
    """Walk-forward для тотала kind ('kills' | 'dur'). rows: (ts, f, actual_kills, actual_dur).
    Цель регрессии — отклонение от общего уровня t_base_*; прогноз mu = уровень + регрессия."""
    base_key, idx = ("t_base_k", 2) if kind == "kills" else ("t_base_d", 3)
    rows = [r for r in rows if r[idx] and all(n in r[1] for n in names)]
    mus, models_used, ys, bases = [], [], [], []
    start, end = WARMUP, rows[-1][0] + 1
    while start < end:
        stop = start + STEP_DAYS * 86400
        train = [r for r in rows if r[0] < start]
        test = [r for r in rows if start <= r[0] < stop]
        if test and len(train) >= 500:
            m = LinReg(names, l2=5.0).fit([vec(r[1], names) for r in train], [r[idx] - r[1][base_key] for r in train])
            for r in test:
                mus.append(r[1][base_key] + m.predict(vec(r[1], names)))
                models_used.append(m)
                ys.append(r[idx])
                bases.append(r[1][base_key])
        start = stop
    n = len(ys)
    mae = sum(abs(a - b) for a, b in zip(mus, ys)) / n
    mae_base = sum(abs(a - b) for a, b in zip(bases, ys)) / n
    step = 6.0 if kind == "kills" else 5.0
    cal = []
    for off in (-step, 0.0, step):
        pred = [mm.prob_over(mu, mu + off) for mu, mm in zip(mus, models_used)]   # линия относительно прогноза
        fact = [1 if y > mu + off else 0 for mu, y in zip(mus, ys)]
        cal.append({"line": "mu%+.0f" % off, "p_pred": round(sum(pred) / n, 3), "p_fact": round(sum(fact) / n, 3)})
    # калибровка на линии = общий уровень (как ставил бы букмекер без знания команд)
    pred = [mm.prob_over(mu, b) for mu, mm, b in zip(mus, models_used, bases)]
    fact = [1 if y > b else 0 for y, b in zip(ys, bases)]
    bins = calibration(pred, fact, bins=5)
    full = LinReg(names, l2=5.0).fit([vec(r[1], names) for r in rows], [r[idx] - r[1][base_key] for r in rows])
    rep = {"features": names, "n": n, "mae": round(mae, 2), "mae_base": round(mae_base, 2),
           "sigma": round(full.sigma, 2), "offsets": cal, "vs_level_line": bins, "logloss_vs_level": metrics(pred, fact),
           "coefs": dict(zip(["intercept"] + names, [round(w, 3) for w in full.w]))}
    if log:
        log("тотал %s (%s): MAE %.2f против %.2f у общего уровня, sigma %.1f" % (kind, "+".join(names[-2:]), mae, mae_base, full.sigma))
    return rep, full


def evaluate_maps(matches, rows_by_id, model, names, log=None):
    """Счёт bo3: предсказанная P(3 карты) = 2p(1−p) по p первой карты против факта; поправочный множитель."""
    series = {}
    for m in matches:
        if m.get("series_type") == 1 and m.get("series_id"):
            series.setdefault(m["series_id"], []).append(m)
    pred, fact, sweep_pred, sweep_fact = [], [], [], []
    for sid, ms in series.items():
        ms.sort(key=lambda x: x["start_time"])
        if len(ms) not in (2, 3) or ms[0]["start_time"] < WARMUP:
            continue
        f = rows_by_id.get(ms[0]["match_id"])
        if not f or not all(n in f for n in names):
            continue
        p = model.predict(vec(f, names))          # p команды Radiant первой карты
        a = ms[0]["radiant_team_id"]
        wins_a = sum(1 for x in ms if (x["radiant_team_id"] == a) == bool(x["radiant_win"]))
        if max(wins_a, len(ms) - wins_a) != 2:
            continue
        pred.append(2 * p * (1 - p))
        fact.append(1 if len(ms) == 3 else 0)
        sweep_pred.append(p * p)
        sweep_fact.append(1 if (wins_a == 2 and len(ms) == 2) else 0)
    if not pred:
        return {"n": 0, "k3": 1.0}
    k3 = (sum(fact) / len(fact)) / (sum(pred) / len(pred))
    rep = {"n": len(pred), "p3_pred": round(sum(pred) / len(pred), 3), "p3_fact": round(sum(fact) / len(fact), 3),
           "k3": round(k3, 3), "sweep_pred": round(sum(sweep_pred) / len(pred), 3),
           "sweep_fact": round(sum(sweep_fact) / len(pred), 3)}
    if log:
        log("bo3: три карты — прогноз %.1f%%, факт %.1f%%, множитель %.3f (%d серий)" % (
            100 * rep["p3_pred"], 100 * rep["p3_fact"], k3, rep["n"]))
    return rep


def run(conn=None, log=None, save=True):
    conn = conn or connect()
    t0 = time.time()
    matches = load_matches(conn)
    rows, trows, by_id = [], [], {}

    def collect(m, f):
        rows.append((m["start_time"], 1 if m["radiant_win"] else 0, f, m["match_id"]))
        by_id[m["match_id"]] = f
        kills = (m.get("radiant_score") or 0) + (m.get("dire_score") or 0)
        dur = (m.get("duration") or 0) / 60.0
        trows.append((m["start_time"], f, kills if kills > 0 and dur > 10 else None, dur if dur > 10 else None))

    st = run_pass(matches, collect, log)
    if log:
        log("признаки: %d матчей, %.0f с" % (len(rows), time.time() - t0))

    report = {"built": int(time.time()), "matches": len(rows), "models": {}, "chosen": {}}
    models, wf = {}, {}
    for name, names in VARIANTS.items():
        report["models"][name], models[name], wf[name] = evaluate(rows, names, log)
    for base in ("pre", "post"):
        cands = [k for k in VARIANTS if k == base or k.startswith(base + "_")]
        best = min(cands, key=lambda k: report["models"][k]["overall"]["logloss"])
        report["chosen"][base] = best
        if save:
            conn.execute("insert or replace into model_coefs(name, payload, trained) values (?,?,?)",
                         (base, json.dumps(models[best].to_dict()), int(time.time())))
        if log:
            log("модель %s ← %s: %s | базовый набор %s | elo %s" % (
                base, best, report["models"][best]["overall"], report["models"][base]["overall"],
                report["models"][base]["baseline_elo"]))
    # оценка одного драфта: те же окна, признаки только от героев; в выбор pre/post не входит
    report["models"]["draft"], draft_model, _ = evaluate(rows, DRAFT_ONLY, log)
    if save:
        conn.execute("insert or replace into model_coefs(name, payload, trained) values (?,?,?)",
                     ("draft", json.dumps(draft_model.to_dict()), int(time.time())))
    if save:      # прогнозы «как в тот день» по каждому матчу: для проверки на кэфах и модели по ходу карты
        pre, post = wf[report["chosen"]["pre"]], wf[report["chosen"]["post"]]
        conn.execute("delete from wf_pred")
        conn.executemany("insert into wf_pred(match_id, ts, p_pre, p_post) values (?,?,?,?)",
                         [(i, pre[i][1], pre[i][0], post.get(i, (None,))[0]) for i in pre])
    report["totals"] = {}
    for kind in ("kills", "dur"):
        for tag, names in (("pre", TOTAL_FEATURES_PRE[kind]), ("post", TOTAL_FEATURES_POST[kind])):
            rep, lin = evaluate_totals(trows, kind, names, log)
            report["totals"]["%s_%s" % (kind, tag)] = rep
            if save:
                conn.execute("insert or replace into model_coefs(name, payload, trained) values (?,?,?)",
                             ("tot_%s_%s" % (kind, tag), json.dumps(lin.to_dict()), int(time.time())))
    report["maps"] = evaluate_maps(matches, by_id, models[report["chosen"]["pre"]],
                                  VARIANTS[report["chosen"]["pre"]], log)
    if save:
        conn.execute("insert or replace into model_coefs(name, payload, trained) values (?,?,?)",
                     ("maps", json.dumps(report["maps"]), int(time.time())))
    if save:
        conn.commit()
        save_state(st)
        try:
            from . import ingame
            report["ingame"] = {str(k): v for k, v in ingame.train(conn, log).items()}
        except Exception as e:
            report["ingame"] = {"error": repr(e)}
        os.makedirs(DATA, exist_ok=True)
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    return report, st


def load_models(conn):
    out = {}
    for r in conn.execute("select name, payload from model_coefs"):
        d = json.loads(r["payload"])
        if r["name"].startswith("tot_"):
            out[r["name"]] = LinReg.from_dict(d)
        elif r["name"] == "maps":
            out["maps"] = d
        elif "names" in d and "w" in d:          # прочие записи (ingame и будущие) грузят свои модули
            out[r["name"]] = LogReg.from_dict(d)
    return out


def load_report():
    try:
        with open(REPORT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return None
