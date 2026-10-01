"""Подкоманды AI: dossier, analyze."""

import sys

from .db import connect


def _prepare(args):
    from .cli_data import resolve_team
    from .cli_model import parse_heroes, parse_ids
    from .predict import Predictor
    from . import dossier
    conn = connect()
    a, b = resolve_team(conn, args.a), resolve_team(conn, args.b)
    pr = Predictor(conn)
    res = pr.predict(a["team_id"], b["team_id"], parse_ids(args.lineup_a), parse_ids(args.lineup_b),
                     parse_heroes(conn, args.picks_a), parse_heroes(conn, args.picks_b), side=args.side,
                     tier=args.tier, bo=args.bo)
    odds = tuple(float(x) for x in args.odds.split(",")) if args.odds else None
    text = dossier.build(pr, conn, a["team_id"], b["team_id"], res, bo=args.bo, odds=odds)
    key = dossier.key_for(a["name"], b["name"])
    path = dossier.save(text, key)
    return conn, a, b, res, text, key, path


def cmd_dossier(args):
    conn, a, b, res, text, key, path = _prepare(args)
    print(path)
    if args.print:
        print(text)


def cmd_analyze(args):
    from . import ai, bets
    from .series import series_distribution
    conn, a, b, res, text, key, path = _prepare(args)
    print("досье: %s" % path, file=sys.stderr)

    def emit(event, data):
        if event == "text":
            sys.stdout.write(data)
            sys.stdout.flush()
        elif event in ("tool", "log", "done"):
            print("\n[%s] %s" % (event, data), file=sys.stderr)

    answer, verdict = ai.analyze(path, emit, model=args.model, effort=args.effort, max_turns=args.max_turns)
    print()
    if verdict:
        print("Вердикт разобран: %s" % verdict, file=sys.stderr)
    else:
        print("Блок «Вердикт» не найден в ответе", file=sys.stderr)
    if args.record:
        p = series_distribution(res["p"], args.bo)["p_a"] if args.bo > 1 else res["p"]
        oa, ob = (tuple(float(x) for x in args.odds.split(",")) if args.odds else (None, None))
        stale = any("играли за другие команды" in w for w in res.get("warnings", []))
        change = stale or res["detail"]["a"]["lineup_change"] > 0 or res["detail"]["b"]["lineup_change"] > 0
        pid = bets.record(conn, res["mode"], a["team_id"], b["team_id"], a["name"], b["name"], p, bo=args.bo,
                          odds_a=oa, odds_b=ob, p_claude=verdict["p_a"] if verdict else None, dossier=key,
                          analysis=answer, tier=args.tier, roster_change=change,
                          confidence=verdict["confidence"] if verdict else None)
        r = conn.execute("select policy_bet, policy_side, policy_stake, policy_reason from predictions where id=?", (pid,)).fetchone()
        print("политика: %s — %s" % ("ставка на %s %.0f" % (r["policy_side"].upper(), r["policy_stake"]) if r["policy_bet"] else "без ставки",
                                    r["policy_reason"]), file=sys.stderr)
        print("записано в журнал #%d" % pid, file=sys.stderr)


def add_match_args(s):
    s.add_argument("a")
    s.add_argument("b")
    s.add_argument("--picks-a", default="")
    s.add_argument("--picks-b", default="")
    s.add_argument("--lineup-a", default="")
    s.add_argument("--lineup-b", default="")
    s.add_argument("--side", choices=("unknown", "radiant", "dire"), default="unknown")
    s.add_argument("--tier", choices=("professional", "premium"), default="professional")
    s.add_argument("--bo", type=int, default=3)
    s.add_argument("--odds", default="")


def register(sub):
    s = sub.add_parser("dossier", help="досье матча для Claude в data/dossiers/")
    add_match_args(s)
    s.add_argument("--print", action="store_true")
    s.set_defaults(func=cmd_dossier)

    s = sub.add_parser("analyze", help="досье + разбор headless Claude (/analyze)")
    add_match_args(s)
    s.add_argument("--model", choices=("fable", "opus", "sonnet"), default="opus")
    s.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"), default="high")
    s.add_argument("--max-turns", type=int, default=25)
    s.add_argument("--record", action="store_true", help="записать прогноз и вердикт в журнал")
    s.set_defaults(func=cmd_analyze)
