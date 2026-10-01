"""Подбор гиперпараметров модели по walk-forward бэктесту (по одному изменению от базовой конфигурации).

    .venv/Scripts/python scripts/sweep.py [--quick]

Каждая конфигурация — полный проход по базе (~30 с) и два walk-forward (pre_2b, post_2b; ~1,5 мин).
Результат — data/sweep.json и таблица в stdout. Ничего в базе не меняет: применять найденное — руками в model.py.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dota2bets import DATA, model, ratings, backtest  # noqa: E402
from dota2bets.db import connect  # noqa: E402

OUT = os.path.join(DATA, "sweep.json")

CONFIGS = [
    ("base", {}),
    ("form_half_15", {"HALF": {"form": 15}}),
    ("form_half_60", {"HALF": {"form": 60}}),
    ("meta_half_30", {"HALF": {"meta": 30}}),
    ("meta_half_120", {"HALF": {"meta": 120}}),
    ("player_hero_half_180", {"HALF": {"player_hero": 180}}),
    ("farm_half_60", {"HALF": {"farm": 60}}),
    ("farm_half_365", {"HALF": {"farm": 365}}),
    ("k_player_12", {"K_PLAYER": 12.0}),
    ("k_player_30", {"K_PLAYER": 30.0}),
    ("prior_meta_20", {"PRIOR": {"meta": 20.0}}),
    ("l2_0.5", {"L2": 0.5}),
    ("l2_8", {"L2": 8.0}),
    ("k_player_40", {"K_PLAYER": 40.0}),
    ("k_player_30_farm_365", {"K_PLAYER": 30.0, "HALF": {"farm": 365}}),
    ("k_player_40_farm_365", {"K_PLAYER": 40.0, "HALF": {"farm": 365}}),
]


def apply(cfg):
    saved = {"HALF": dict(model.HALF), "PRIOR": dict(model.PRIOR), "K_PLAYER": model.K_PLAYER, "L2": backtest.L2}
    for k, v in cfg.get("HALF", {}).items():
        model.HALF[k] = v
    for k, v in cfg.get("PRIOR", {}).items():
        model.PRIOR[k] = v
    if "K_PLAYER" in cfg:
        model.K_PLAYER = cfg["K_PLAYER"]
    if "L2" in cfg:
        backtest.L2 = cfg["L2"]
    return saved


def restore(saved):
    model.HALF.clear(); model.HALF.update(saved["HALF"])
    model.PRIOR.clear(); model.PRIOR.update(saved["PRIOR"])
    model.K_PLAYER = saved["K_PLAYER"]
    backtest.L2 = saved["L2"]


def main():
    quick = "--quick" in sys.argv
    conn = connect()
    matches = model.load_matches(conn)
    results = {}
    if os.path.exists(OUT):  # возобновление: уже посчитанные конфигурации пропускаем
        with open(OUT, encoding="utf-8") as f:
            results = json.load(f)
    for name, cfg in CONFIGS[: 4 if quick else None]:
        if name in results:
            print("%-24s (из data/sweep.json)" % name, flush=True)
            continue
        t0 = time.time()
        saved = apply(cfg)
        try:
            rows = []
            model.run_pass(matches, lambda m, f: rows.append((m["start_time"], 1 if m["radiant_win"] else 0, f)))
            res = {}
            for variant in ("pre_2b", "post_2b"):
                ps, ys, _, _, _ = backtest.walk_forward(rows, backtest.VARIANTS[variant])
                res[variant] = backtest.metrics(ps, ys)
        finally:
            restore(saved)
        results[name] = res
        print("%-24s pre %.4f  post %.4f  (%.0f с)" % (name, res["pre_2b"]["logloss"], res["post_2b"]["logloss"],
                                                    time.time() - t0), flush=True)
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)
    base = results["base"]
    print("\nЛучше базовой (post_2b):")
    for name, r in sorted(results.items(), key=lambda kv: kv[1]["post_2b"]["logloss"]):
        d = r["post_2b"]["logloss"] - base["post_2b"]["logloss"]
        print("  %-24s %+.4f" % (name, d))


if __name__ == "__main__":
    main()
