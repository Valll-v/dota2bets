"""Подкоманды: series, bracket, odds, bets."""

import json
import time

from . import opendota as od
from .db import connect
from .odds import evaluate
from .series import series_distribution, simulate_bracket


def cmd_series(args):
    d = series_distribution(args.p, args.bo, args.lead_a, args.lead_b, args.momentum)
    print("bo%d при p=%.3f за карту: A %.1f%%, B %.1f%%%s" % (
        args.bo, args.p, 100 * d["p_a"], 100 * d["p_b"], (", ничья %.1f%%" % (100 * d["p_draw"])) if d["p_draw"] else ""))
    for s, p in d["scores"].items():
        print("  %d-%d  %5.1f%%" % (s[0], s[1], 100 * p))


def cmd_bracket(args):
    from .cli_data import resolve_team
    from .predict import Predictor
    with open(args.file, encoding="utf-8") as f:
        bracket = json.load(f)
    conn = connect()
    pr = Predictor(conn)
    ids = [resolve_team(conn, t)["team_id"] for t in bracket["teams"]]
    names = dict(zip(ids, bracket["teams"]))
    bracket = dict(bracket, teams=ids)
    res = simulate_bracket(bracket, lambda a, b: pr.predict(a, b)["p"], runs=args.runs)
    print("Чемпион (%d прогонов):" % args.runs)
    for t, p in sorted(res["champion"].items(), key=lambda kv: -kv[1]):
        print("  %-24s %5.1f%%" % (names[t], 100 * p))
    for rname, d in res["reached"].items():
        print("Дошли до «%s»: %s" % (rname, ", ".join("%s %.0f%%" % (names[t], 100 * p)
                                                     for t, p in sorted(d.items(), key=lambda kv: -kv[1]) if p > 0)))


def cmd_odds(args):
    ev = evaluate(args.p, args.a, args.b)
    print("implied A %.1f%%, B %.1f%% (маржа %.1f%%)" % (100 * ev["implied_a"], 100 * ev["implied_b"], 100 * ev["margin"]))
    print("edge A %+.1f%%, B %+.1f%%; EV A %+.3f, B %+.3f; Келли/4 A %.1f%%, B %.1f%%" % (
        100 * ev["edge_a"], 100 * ev["edge_b"], ev["ev_a"], ev["ev_b"], 100 * ev["kelly_a"], 100 * ev["kelly_b"]))


def fmt_metrics(m):
    return "log loss %.3f, точность %.0f%% (n %d)" % (m["logloss"], 100 * m["acc"], m["n"]) if m else "-"


def fmt_pnl(p):
    if not p or not p["n"]:
        return "ставок 0"
    return "ставок %d, выиграно %d, оборот %.0f, прибыль %+.0f, ROI %+.1f%%" % (
        p["n"], p["wins"], p["staked"], p["profit"], 100 * (p["roi"] or 0))


def print_report(rep):
    s = rep["settings"]
    print("Политика: смешивание %.0f%% линии + %.0f%% модели, порог edge %.0f п.п., минимум bo%d, Келли ×%.2f, "
          "потолок %.0f%% банка, банк %.0f" % (100 * s["blend"], 100 * (1 - s["blend"]), 100 * s["min_edge"],
                                                s["min_bo"], s["kelly"], 100 * s["max_stake"], s["bankroll"]))
    print("Рассчитано %d, ждут результата %d, без закрывающих кэфов %d." % (rep["n"], rep["pending"], rep["without_closing"]))
    names = {"p_model": "модель", "p_blend": "смешанная", "p_claude": "Claude", "book": "букмекер"}
    for k, v in rep["sources"].items():
        print("  %-10s %s" % (names[k], fmt_metrics(v)))
    print("Политика (бумажная): %s" % fmt_pnl(rep["policy"]))
    print("Реальные ставки:     %s" % fmt_pnl(rep["money"]))
    if rep["segments"]:
        print("\nПо сегментам (модель / букмекер / политика):")
        for k, groups in rep["segments"].items():
            for v, g in sorted(groups.items(), key=lambda kv: -kv[1]["n"]):
                print("  %-8s %-18s n %3d | %s | %s | %s" % (k, v, g["n"], fmt_metrics(g["model"]), fmt_metrics(g["book"]),
                                                          fmt_pnl(g["policy"])))


def cmd_bets(args):
    from . import bets, policy
    conn = connect()
    if args.action == "record":
        from .cli_data import resolve_team
        from .predict import Predictor
        a, b = resolve_team(conn, args.a), resolve_team(conn, args.b)
        from .cli_model import parse_heroes
        from .schedule import series_state, decided
        state = series_state(conn, a["team_id"], b["team_id"], int(time.time()) - 14 * 3600)
        if state and decided(state, state["bo"] or args.bo):
            state = None
        la, lb = (state["a"], state["b"]) if state else (0, 0)
        res = Predictor(conn).predict(a["team_id"], b["team_id"], None, None, parse_heroes(conn, args.picks_a),
                                      parse_heroes(conn, args.picks_b), side=args.map_side, tier=args.tier,
                                      bo=args.bo, series_id=state["series_id"] if state else None)
        p = (series_distribution(res["p"], args.bo, la, lb)["p_a"]
             if args.bo > 1 and args.market != "map" else res["p"])
        oa, ob = ([float(x) for x in args.odds.split(",")] if args.odds else (None, None))
        stale = any("играли за другие команды" in w for w in res.get("warnings", []))
        change = stale or res["detail"]["a"]["lineup_change"] > 0 or res["detail"]["b"]["lineup_change"] > 0
        pid = bets.record(conn, res["mode"], a["team_id"], b["team_id"], a["name"], b["name"], p, bo=args.bo,
                          odds_a=oa, odds_b=ob, side=args.side, stake=args.stake, note=args.note, tier=args.tier,
                          roster_change=change, paper=not args.money, market=args.market,
                          map_no=la + lb + 1 if args.market == "map" else None,
                          series_id=state["series_id"] if state else None)
        r = conn.execute("select * from predictions where id=?", (pid,)).fetchone()
        print("записано #%d: %s vs %s, p(A)=%.3f, %s, %s" % (
            pid, a["name"], b["name"], p, "карта %d" % (la + lb + 1) if args.market == "map" else "серия bo%d" % args.bo,
            "деньги" if args.money else "бумага"))
        if r["p_blend"] is not None:
            print("  смешанная p(A)=%.3f; политика: %s — %s" % (r["p_blend"], "ставка на %s %.0f" % (
                r["policy_side"].upper(), r["policy_stake"]) if r["policy_bet"] else "без ставки", r["policy_reason"]))
    elif args.action == "close":
        oa, ob = [float(x) for x in args.odds.split(",")]
        d = bets.close(conn, int(args.a), oa, ob)
        print("#%s: закрытие %.2f/%.2f, смешанная p(A)=%.3f, политика: %s — %s" % (
            args.a, oa, ob, d["p_blend"], "ставка на %s" % d["side"].upper() if d["bet"] else "без ставки", d["reason"]))
    elif args.action == "import":
        print("закрывающих кэфов импортировано: %d" % bets.import_closing(conn, args.a))
    elif args.action == "config":
        s = policy.configure(conn, bankroll=args.bankroll, blend=args.blend, min_edge=args.min_edge,
                             max_stake=args.max_stake, min_bo=args.min_bo, flat=args.flat)
        print(json.dumps(s, ensure_ascii=False))
    elif args.action == "settle":
        print("подтянуто результатов: %d" % bets.settle(conn, od.log))
    elif args.action == "report":
        print_report(bets.report(conn))
    else:
        for r in bets.list_rows(conn, args.limit):
            print("#%-4d %s %-16s vs %-16s bo%d p %.2f blend %s Claude %s кэфы %s/%s закр %s/%s политика %s итог %s%s" % (
                r["id"], time.strftime("%m-%d", time.gmtime(r["created"])), (r["name_a"] or "")[:16],
                (r["name_b"] or "")[:16], r["bo"] or 1, r["p_model"] or 0,
                "%.2f" % r["p_blend"] if r["p_blend"] is not None else "-",
                "%.2f" % r["p_claude"] if r["p_claude"] is not None else "-", r["odds_a"] or "-", r["odds_b"] or "-",
                r["closing_a"] or "-", r["closing_b"] or "-",
                ("%s %.0f" % (r["policy_side"].upper(), r["policy_stake"])) if r["policy_bet"] else "нет",
                {1: "A", 0: "B", 2: "ничья"}.get(r["result"], "?") if r["settled"] else "?",
                "" if r["paper"] else " [деньги %s %s]" % (r["side"], r["stake"])))


def print_forecast(f):
    if f.get("error"):
        print("%s vs %s: ошибка %s" % (f.get("name_a"), f.get("name_b"), f["error"]))
        return
    v = f["verdict"]
    head = "%s vs %s  bo%d  [%s%s]" % (f["name_a"], f["name_b"], f["bo"], f["status"],
                                        ", счёт %d:%d" % (f["lead_a"], f["lead_b"]) if f["status"] != "ожидается" else "")
    print("\n#%s %s" % (f["id"] if f.get("id") else "-", head))
    print("  карта %.0f%% | %s %.0f%% | счёт: %s" % (100 * f["p_map"], "серия" if f["bo"] > 1 else "карта", 100 * f["p_bet"],
                                                   ", ".join("%s %.0f%%" % (x["score"], 100 * x["p"]) for x in f["scores"][:4])))
    if f["odds_a"]:
        print("  ваши кэфы %.2f / %.2f" % (f["odds_a"], f["odds_b"]))
    fair = v.get("fair")
    if fair:
        print("  эталон %s: %.2f / %.2f, без маржи %.0f%% / %.0f%%, справедливая цена %.2f / %.2f" % (
            fair["book"], fair["odds_a"], fair["odds_b"], 100 * fair["p_a"], 100 * fair["p_b"], fair["fair_a"], fair["fair_b"]))
    print("  ВЕРДИКТ: %s" % v["text"])
    print("  Надёжность: %s%s" % (v["reliability"], (" — " + "; ".join(v["reliability_reasons"])) if v["reliability_reasons"] else ""))
    print("  %s" % v["recommendation"])
    pl = f.get("players") or {}
    if pl.get("duel"):
        print("  Игроки: " + pl["duel"])
    for ln in (pl.get("lines") or [])[:5]:
        print("    - " + ln["text"])
    for w in f["warnings"]:
        print("  ! " + w)


def cmd_forecast(args):
    from .predict import Predictor
    from .schedule import forecasts
    conn = connect()
    res = forecasts(conn, Predictor(conn), include_done=args.done)
    s = res["settings"]
    print("Флет = %.1f%% банка (%.0f при банке %.0f). Политика: %.0f%% линии + %.0f%% модели, порог edge %.0f п.п." % (
        100 * s["flat"], s["flat"] * s["bankroll"], s["bankroll"], 100 * s["blend"], 100 * (1 - s["blend"]), 100 * s["min_edge"]))
    if not res["items"]:
        print('лента пуста: добавьте матчи — schedule add "A vs B bo3 1.80 2.00"')
    for f in res["items"]:
        print_forecast(f)
    if args.notify:
        from . import notify
        print("\nуведомлений: %d" % notify.notify_forecasts(conn, res["items"]))


def cmd_schedule(args):
    from . import schedule
    conn = connect()
    if args.action == "add":
        text = " ".join(args.rest)
        if args.file:
            with open(args.file, encoding="utf-8") as fh:
                text = fh.read()
        added, errors = schedule.add_text(conn, text.replace(";", "\n"))
        print("добавлено: %s" % added)
        for e in errors:
            print("ошибка: " + e)
    elif args.action == "rm":
        for x in args.rest:
            schedule.remove(conn, int(x))
        print("удалено")
    elif args.action == "odds":
        sid, oa, ob = int(args.rest[0]), float(args.rest[1]), float(args.rest[2])
        schedule.update(conn, sid, odds_a=oa, odds_b=ob)
        print("#%d: кэфы %.2f / %.2f" % (sid, oa, ob))
    else:
        for r in conn.execute("select * from schedule order by done, coalesce(starts, created), id"):
            print("#%-3d %-22s vs %-22s bo%d %s кэфы %s/%s%s" % (r["id"], r["name_a"][:22], r["name_b"][:22], r["bo"], r["tier"],
                                                              r["odds_a"] or "-", r["odds_b"] or "-", " [сыграна]" if r["done"] else ""))


def register(sub):
    s = sub.add_parser("forecast", help="лента прогнозов: расписание + идущие серии, вердикт и флеты")
    s.add_argument("--done", action="store_true", help="показывать и сыгранные")
    s.add_argument("--notify", action="store_true", help="отправить новые рекомендации в Telegram")
    s.set_defaults(func=cmd_forecast)

    s = sub.add_parser("schedule", help='расписание: add "A vs B bo3 1.8 2.0; C vs D" | list | rm <id> | odds <id> a b')
    s.add_argument("action", choices=("add", "list", "rm", "odds"))
    s.add_argument("rest", nargs="*")
    s.add_argument("--file", default=None, help="add: файл со строками расписания")
    s.set_defaults(func=cmd_schedule)

    s = sub.add_parser("series", help="распределение счёта серии")
    s.add_argument("--p", type=float, required=True, help="вероятность победы A на карте")
    s.add_argument("--bo", type=int, default=3)
    s.add_argument("--lead-a", type=int, default=0)
    s.add_argument("--lead-b", type=int, default=0)
    s.add_argument("--momentum", type=float, default=0.0)
    s.set_defaults(func=cmd_series)

    s = sub.add_parser("bracket", help="Монте-Карло по сетке из JSON")
    s.add_argument("file")
    s.add_argument("--runs", type=int, default=20000)
    s.set_defaults(func=cmd_bracket)

    s = sub.add_parser("odds", help="edge и Келли по кэфам")
    s.add_argument("a", type=float)
    s.add_argument("b", type=float)
    s.add_argument("--p", type=float, required=True, help="наша вероятность A")
    s.set_defaults(func=cmd_odds)

    s = sub.add_parser("bets", help="журнал прогнозов и ставок (бумажная торговля по умолчанию)")
    s.add_argument("action", choices=("record", "close", "import", "config", "settle", "report", "list"))
    s.add_argument("a", nargs="?", help="record: команда A; close: id записи; import: csv id,closing_a,closing_b")
    s.add_argument("b", nargs="?")
    s.add_argument("--bo", type=int, default=3)
    s.add_argument("--tier", choices=("professional", "premium"), default="professional")
    s.add_argument("--odds", default="", help="record: открывающие кэфы A,B; close: закрывающие A,B")
    s.add_argument("--side", choices=("a", "b"), default=None, help="реальная ставка (с --money)")
    s.add_argument("--stake", type=float, default=None)
    s.add_argument("--money", action="store_true", help="не бумага: реальная ставка")
    s.add_argument("--market", choices=("series", "map"), default="series", help="рынок: серия или карта")
    s.add_argument("--picks-a", default="", help="для ставки на карту после драфта")
    s.add_argument("--picks-b", default="")
    s.add_argument("--map-side", choices=("unknown", "radiant", "dire"), default="unknown", help="сторона A на карте")
    s.add_argument("--note", default=None)
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--bankroll", type=float, default=None)
    s.add_argument("--blend", type=float, default=None, help="доля линии в смешанной p (0.7)")
    s.add_argument("--min-edge", type=float, default=None)
    s.add_argument("--max-stake", type=float, default=None)
    s.add_argument("--min-bo", type=int, default=None)
    s.add_argument("--flat", type=float, default=None, help="размер флета в долях банка (0.01)")
    s.set_defaults(func=cmd_bets)
