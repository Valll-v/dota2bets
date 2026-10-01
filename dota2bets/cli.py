"""CLI: python -m dota2bets <cmd>."""

import argparse
import json
import sys
import time

from . import opendota as od
from .db import connect, counts, kv_get


def utf8_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) if ts else "-"


# ---------------------------------------------------------------- команды

def cmd_sync(args):
    from .ingest import sync
    conn = sync(fresh_limit=args.fresh)
    print(json.dumps(counts(conn), ensure_ascii=False, indent=1))


def cmd_status(args):
    conn = connect()
    c = counts(conn)
    c["last_start_time"] = fmt_ts(c["last_start_time"])
    c["last_sync"] = fmt_ts(kv_get(conn, "last_sync"))
    c["budget"] = od.BUDGET.status()
    print(json.dumps(c, ensure_ascii=False, indent=1))


def cmd_web(args):
    from .web import main as web_main
    argv = ["--port", str(args.port), "--host", args.host]
    if args.no_browser:
        argv.append("--no-browser")
    if args.skip_permissions:
        argv.append("--skip-permissions")
    web_main(argv)


def build_parser():
    p = argparse.ArgumentParser(prog="dota2bets", description="Анализ про-матчей Dota 2 для ставок")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="синхронизация базы с OpenDota")
    s.add_argument("--fresh", type=int, default=100, help="лимит запросов /matches/{id} за прогон (0 — без REST)")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("status", help="размер базы и бюджет OpenDota")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("web", help="локальный веб-интерфейс")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--skip-permissions", action="store_true")
    s.set_defaults(func=cmd_web)

    from . import cli_data, cli_model, cli_bets, cli_ai, cli_book
    cli_data.register(sub)
    cli_model.register(sub)
    cli_bets.register(sub)
    cli_ai.register(sub)
    cli_book.register(sub)
    return p


def main(argv=None):
    utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except od.BudgetError as e:
        print("бюджет OpenDota: %s" % e, file=sys.stderr)
        sys.exit(2)
