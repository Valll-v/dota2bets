"""Подкоманды: book (кэфы и проверка на истории), ingame (вероятность по ходу карты), notify."""

import json
import os
import time

from . import opendota as od
from .db import connect


def cmd_book(args):
    from . import oddsapi, oddsbook
    conn = connect()
    if args.action == "status":
        n_fx = conn.execute("select count(*) from odds_fixtures").fetchone()[0]
        n_ln = conn.execute("select count(*) from odds_lines").fetchone()[0]
        n_wf = conn.execute("select count(*) from wf_pred").fetchone()[0]
        print("ключ OddsPapi: %s" % ("задан" if oddsapi.available() else "не задан (переменная окружения ODDSPAPI_KEY)"))
        print("матчей с кэфами: %d, линий: %d, прогнозов walk-forward: %d" % (n_fx, n_ln, n_wf))
        for r in conn.execute("select bookmaker, count(*) n from odds_lines group by 1 order by 2 desc"):
            print("  %-16s %d" % (r["bookmaker"], r["n"]))
    elif args.action == "fixtures":
        now = int(time.time())
        d1 = time.strftime("%Y-%m-%d", time.gmtime(now - 6 * 3600))
        d2 = time.strftime("%Y-%m-%d", time.gmtime(now + args.days * 86400))
        idx = oddsbook.TeamIndex(conn)
        for fx in oddsapi.fixtures(d1, d2):
            a, b = idx.find(fx.get("participant1Name")), idx.find(fx.get("participant2Name"))
            print("%s  %-24s — %-24s %s  %s" % (fx.get("startTime", "")[:16], fx.get("participant1Name"), fx.get("participant2Name"),
                                                 fx.get("tournamentName") or "", "" if (a and b) else "[команда не найдена в базе]"))
    elif args.action == "sync":
        r = oddsbook.sync_upcoming(conn, days=args.days, log=od.log)
        print("расписание: добавлено %(added)d, обновлено %(updated)d, не сопоставлено %(skipped)d" % r)
    elif args.action == "history":
        now = int(time.time())
        n = oddsbook.sync_history(conn, now - args.days * 86400, now, log=od.log, limit=args.limit)
        print("матчей с историей линии: %d" % n)
    elif args.action == "import":
        if not args.file:
            raise SystemExit("укажите файл: book import odds.csv")
        n, bad = oddsbook.import_csv(conn, args.file, book=args.book or "csv")
        print("импортировано: %d" % n)
        for b in bad[:20]:
            print("  " + b)
    elif args.action == "backtest":
        rep = oddsbook.backtest(conn, args.book)
        print(oddsbook.describe(rep))
        from . import DATA
        with open(os.path.join(DATA, "odds_backtest.json"), "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)


def cmd_ingame(args):
    from . import ingame
    conn = connect()
    if args.train:
        rep = ingame.train(conn, od.log)
        print("обучено минут: %d" % len(rep))
        return
    b = ingame.load(conn)
    if not b:
        raise SystemExit("модель по ходу карты не обучена: python -m dota2bets train")
    if args.lead is not None:
        p = ingame.predict(b, args.p, args.minute, args.lead)
        print("минута %d, перевес %+d золота, до карты %.0f%% → сейчас %.1f%%" % (args.minute, args.lead, 100 * args.p, 100 * p))
    print("минута %d, до карты %.0f%%:" % (args.minute, 100 * args.p))
    for r in ingame.table(b, args.p, args.minute):
        print("  перевес %+6d → %.1f%%" % (r["lead"], 100 * r["p"]))
    rep = b["report"].get(str(min(ingame.MINUTES, key=lambda m: abs(m - args.minute))))
    if rep:
        print("проверка на свежих картах: log loss %.3f с золотом против %.3f без (%d карт)" % (
            rep["model"]["logloss"], rep["pregame_only"]["logloss"], rep["n"]))


def cmd_notify(args):
    from . import notify
    from .predict import Predictor
    from .schedule import forecasts
    conn = connect()
    res = forecasts(conn, Predictor(conn))
    n = notify.notify_forecasts(conn, res["items"], dry=args.dry)
    print("рекомендаций отправлено: %d%s" % (n, "" if notify.configured() else " (Telegram не настроен: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)"))


def register(sub):
    s = sub.add_parser("book", help="кэфы: status | fixtures | sync | history | import <csv> | backtest")
    s.add_argument("action", choices=("status", "fixtures", "sync", "history", "import", "backtest"))
    s.add_argument("file", nargs="?")
    s.add_argument("--days", type=int, default=3, help="sync, fixtures: дней вперёд; history: дней назад")
    s.add_argument("--limit", type=int, default=None, help="history: не больше матчей за прогон")
    s.add_argument("--book", default=None, help="букмекер: pinnacle, bet365, 1xbet, либо метка для CSV")
    s.set_defaults(func=cmd_book)

    s = sub.add_parser("ingame", help="вероятность по ходу карты: --p 0.6 --minute 20 --lead 5000")
    s.add_argument("--p", type=float, default=0.5, help="вероятность команды до начала карты")
    s.add_argument("--minute", type=int, default=20)
    s.add_argument("--lead", type=int, default=None, help="перевес команды по золоту (отрицательный — отставание)")
    s.add_argument("--train", action="store_true")
    s.set_defaults(func=cmd_ingame)

    s = sub.add_parser("notify", help="отправить в Telegram новые рекомендации «СТАВКА»")
    s.add_argument("--dry", action="store_true", help="только показать, ничего не отправлять")
    s.set_defaults(func=cmd_notify)
