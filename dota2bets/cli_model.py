"""Подкоманды модели: train, backtest, predict."""

import json
import time

from . import opendota as od
from .db import connect


def cmd_train(args):
    from . import ratings, backtest
    conn = connect()
    ratings.compute(conn, od.log)
    report, _ = backtest.run(conn, od.log, save=True)
    print_report(report)


def print_report(report):
    print("Бэктест walk-forward (%d матчей, обучение раз в 2 месяца):" % report["matches"])
    if report.get("chosen"):
        print("выбрано: " + ", ".join("%s ← %s" % kv for kv in report["chosen"].items()))
    for name, m in report["models"].items():
        print("\n[%s] %s" % (name, ", ".join(m["features"])))
        print("  модель:   %s" % m["overall"])
        print("  Elo:      %s" % m["baseline_elo"])
        print("  50/50:    %s" % m["baseline_half"])
        print("  коэффициенты: " + ", ".join("%s %+.3f" % kv for kv in m["coefs"].items()))
        print("  калибровка: " + "  ".join("%s→%.2f/%.2f (%d)" % (c["bin"], c["p_mean"], c["y_mean"], c["n"])
                                           for c in m["calibration"]))
        print("  по месяцам: " + "  ".join("%s %.3f" % (k, v["logloss"]) for k, v in m["by_month"].items()))


def cmd_backtest(args):
    from .backtest import load_report
    r = load_report()
    if not r:
        raise SystemExit("отчёта нет: python -m dota2bets train")
    print_report(r)


def parse_heroes(conn, text):
    """'Pudge, Lion, 14, ...' → список hero_id."""
    if not text:
        return None
    names = {r["localized_name"].lower(): r["hero_id"] for r in conn.execute("select hero_id, localized_name from heroes")}
    short = {r["name"].replace("npc_dota_hero_", ""): r["hero_id"] for r in conn.execute("select hero_id, name from heroes")}
    out = []
    for tok in text.split(","):
        t = tok.strip().lower()
        if not t:
            continue
        if t.isdigit():
            out.append(int(t))
        elif t in names:
            out.append(names[t])
        elif t.replace(" ", "_") in short:
            out.append(short[t.replace(" ", "_")])
        else:
            cand = [h for n, h in names.items() if t in n]
            if len(cand) != 1:
                raise SystemExit("герой не распознан: %s" % tok)
            out.append(cand[0])
    return out


def parse_ids(text):
    return [int(x) for x in text.split(",") if x.strip()] if text else None


def cmd_predict(args):
    from .cli_data import resolve_team
    from .predict import Predictor
    from .series import series_distribution
    from .odds import evaluate
    conn = connect()
    a, b = resolve_team(conn, args.a), resolve_team(conn, args.b)
    pr = Predictor(conn)
    from .schedule import series_state, decided
    import time as _t
    state = series_state(conn, a["team_id"], b["team_id"], int(_t.time()) - 14 * 3600)
    if state and decided(state, state["bo"] or args.bo):
        state = None
    lead_a, lead_b = (state["a"], state["b"]) if state else (args.lead_a, args.lead_b)
    if args.lead_a or args.lead_b:
        lead_a, lead_b = args.lead_a, args.lead_b
    res = pr.predict(a["team_id"], b["team_id"], parse_ids(args.lineup_a), parse_ids(args.lineup_b),
                     parse_heroes(conn, args.picks_a), parse_heroes(conn, args.picks_b), side=args.side,
                     tier=args.tier, bo=args.bo, series_id=state["series_id"] if state else None)
    if state:
        print("идёт серия: счёт %d:%d, следующая карта %d" % (lead_a, lead_b, lead_a + lead_b + 1))
    print(pr.describe(res, a["name"], b["name"]))
    pn = pr.player_names
    for label, lu, dr in (("A", res["lineup_a"], res["draft_a"]), ("B", res["lineup_b"], res["draft_b"])):
        if dr:
            print("  состав %s: %s" % (label, ", ".join("%s=%s" % (pn.get(acc, acc), pr.hero_names.get(h, h)) for acc, h in dr)))
        else:
            print("  состав %s: %s" % (label, ", ".join(str(pn.get(acc, acc)) for acc in lu)))
    if args.bo and args.bo > 1:
        dist = series_distribution(res["p"], args.bo, lead_a, lead_b)
        print("  серия bo%d: p(A) = %.1f%%; счёт: %s" % (args.bo, 100 * dist["p_a"], ", ".join(
            "%d-%d %.1f%%" % (s[0], s[1], 100 * p) for s, p in dist["scores"].items())))
    if args.odds:
        oa, ob = [float(x) for x in args.odds.split(",")]
        p_series = (series_distribution(res["p"], args.bo, lead_a, lead_b)["p_a"]
                    if args.bo and args.bo > 1 and args.market != "map" else res["p"])
        ev = evaluate(p_series, oa, ob)
        print("  кэфы %.2f/%.2f: implied %.1f%%/%.1f%% (маржа %.1f%%), edge A %+.1f%%, B %+.1f%%, EV A %+.3f, B %+.3f, "
              "Келли/4: A %.1f%%, B %.1f%%" % (oa, ob, 100 * ev["implied_a"], 100 * ev["implied_b"], 100 * ev["margin"],
                                              100 * ev["edge_a"], 100 * ev["edge_b"], ev["ev_a"], ev["ev_b"],
                                              100 * ev["kelly_a"], 100 * ev["kelly_b"]))
    from . import policy
    oa, ob = ([float(x) for x in args.odds.split(",")] if args.odds else (None, None))
    dist = series_distribution(res["p"], args.bo, lead_a, lead_b) if args.bo and args.bo > 1 else None
    is_map = args.market == "map"
    v = policy.verdict(res, a["name"], b["name"], res["p"] if (is_map or not dist) else dist["p_a"],
                       [("%d-%d" % k, x) for k, x in dist["scores"].items()] if dist else [], args.bo or 1, oa, ob,
                       policy.settings(conn), market=args.market, map_no=lead_a + lead_b + 1)
    print("\nВЕРДИКТ: %s" % v["text"])
    print("Надёжность данных: %s%s" % (v["reliability"], (" (" + "; ".join(v["reliability_reasons"]) + ")") if v["reliability_reasons"] else ""))
    print(v["recommendation"])
    for w in res.get("warnings", []):
        print("! " + w)
    from . import draft as draft_mod
    dr = draft_mod.analyze(pr, conn, res, a["team_id"], b["team_id"], a["name"], b["name"], with_pubs=not args.no_pubs)
    if dr:
        print("\nРазбор драфта:")
        print(draft_mod.describe(dr, a["name"], b["name"]))
    from . import players
    pl = players.matchup(pr, conn, res, a["team_id"], b["team_id"], a["name"], b["name"], int(_t.time()))
    print("\nИгроки:")
    if pl["duel"]:
        print("  " + pl["duel"])
    for ln in pl["lines"]:
        print("  - " + ln["text"])
    print("  " + pl["caveat"])
    from . import markets as mk
    lines, modds = {}, {}
    for spec in args.mkt or []:
        key, line, od = mk.parse_spec(spec)
        if line is not None:
            lines["hcp" if key.startswith("hcp") else key] = line
        if od:
            modds[key] = od
    print("\nДругие рынки:")
    for m in mk.build(pr, res, a["name"], b["name"], args.bo or 1, dist, policy.settings(conn), lines, modds, lead_a, lead_b):
        print("  " + mk.describe(m))
    if args.json:
        out = {k: v for k, v in res.items() if k not in ("detail",)}
        print(json.dumps(out, ensure_ascii=False, default=str))


def register(sub):
    s = sub.add_parser("train", help="пересчёт рейтингов, бэктест, обучение моделей")
    s.set_defaults(func=cmd_train)

    s = sub.add_parser("backtest", help="показать последний отчёт бэктеста")
    s.set_defaults(func=cmd_backtest)

    s = sub.add_parser("predict", help="прогноз на матч")
    s.add_argument("a")
    s.add_argument("b")
    s.add_argument("--picks-a", default="", help="герои A через запятую (имена или id)")
    s.add_argument("--picks-b", default="")
    s.add_argument("--lineup-a", default="", help="account_id через запятую (иначе фактическая пятёрка)")
    s.add_argument("--lineup-b", default="")
    s.add_argument("--side", choices=("unknown", "radiant", "dire"), default="unknown", help="сторона A")
    s.add_argument("--tier", choices=("professional", "premium"), default="professional", help="тир турнира")
    s.add_argument("--bo", type=int, default=1)
    s.add_argument("--market", choices=("series", "map"), default="series",
                   help="рынок ставки: серия или отдельная карта (кэфы тогда — на карту)")
    s.add_argument("--lead-a", type=int, default=0, help="счёт серии: карт у A (иначе берётся из базы)")
    s.add_argument("--lead-b", type=int, default=0)
    s.add_argument("--odds", default="", help="десятичные кэфы A,B на выбранный рынок")
    s.add_argument("--no-pubs", action="store_true", help="не запрашивать общую статистику игроков на героях (10 запросов OpenDota)")
    s.add_argument("--mkt", action="append", default=[],
                   help="рынок:линия:кэф_да,кэф_нет — kills:48.5:1.85,1.90 | dur:38.5:1.8,1.95 | maps:2.5:2.1,1.7 | "
                        "hcp_a:1.5:2.6,1.45 | hcp_b:1.5:4.0,1.2; можно несколько раз")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_predict)
