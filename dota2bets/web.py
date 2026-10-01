"""Локальный веб-интерфейс на stdlib.

    python -m dota2bets web [--port 8765] [--skip-permissions]

Эндпоинты:
  GET  /                      страница
  GET  /api/status            база, бюджет OpenDota, бэктест
  GET  /api/teams?q=          поиск команд
  GET  /api/team?id=          фактическая пятёрка и последние матчи
  GET  /api/heroes            список героев
  POST /api/predict           прогноз (JSON: team_a, team_b, picks_a, picks_b, lineup_a, lineup_b, side, bo, odds_a, odds_b)
  POST /api/series            распределение серии (p, bo, lead_a, lead_b)
  POST /api/bracket           Монте-Карло по сетке (teams — имена или id)
  GET  /api/live              лиговые игры сейчас
  GET  /api/backtest          отчёт бэктеста
  GET  /api/bets              журнал; POST /api/bets — запись; POST /api/bets/settle — результаты
  GET  /api/analyze?...       SSE: досье → claude -p /analyze → события text/tool/done/verdict
  POST /api/stop              остановить разбор
  POST /api/sync              синхронизация в фоне (fresh=N)
  GET  /api/forecasts         лента прогнозов: расписание + идущие серии, вердикт и флеты
  GET  /api/history?days=     сыгранные серии с прогнозом «как в тот день»: верно или мимо
  GET  /static/...            шрифты и прочие файлы страницы
  GET  /api/rails             боковые панели разбора: видео и реклама (notes/rails.json)
  GET  /media/...             файлы из data/media для боковых панелей
  POST /api/schedule          добавить в расписание (text — несколько строк, или поля); /update, /delete
"""

import argparse
import gzip
import json
import os
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import ROOT
from . import opendota as od
from .db import connect, counts, kv_get

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".woff2": "font/woff2"}

_predictor = {"obj": None, "lock": threading.Lock()}
_calc = threading.RLock()       # состояние модели общее: прогнозы считаются по одному
_sync = {"running": False, "log": []}
_live = {"ts": 0.0, "data": None}
LIVE_TTL = 20


def predictor():
    with _predictor["lock"]:
        if _predictor["obj"] is None:
            from .predict import Predictor
            _predictor["obj"] = Predictor(connect())
        return _predictor["obj"]


def reload_predictor():
    with _predictor["lock"]:
        _predictor["obj"] = None


def team_logo(conn, team_id):
    r = conn.execute("select logo_url from teams where team_id=?", (team_id,)).fetchone()
    return r["logo_url"] if r and r["logo_url"] else None


def qint(q, key, default=None):
    v = q.get(key, [None])[0]
    return int(v) if v not in (None, "") else default


def parse_hero_list(conn, text):
    from .cli_model import parse_heroes
    if isinstance(text, list):
        return [int(h) for h in text if h]
    return parse_heroes(conn, text) if text else None


def do_predict(conn, body):
    with _calc:
        return _do_predict(conn, body)


def _do_predict(conn, body):
    from .cli_data import resolve_team
    from .series import series_distribution
    from .odds import evaluate
    pr = predictor()
    a, b = resolve_team(conn, body["team_a"]), resolve_team(conn, body["team_b"])
    lineup_a = [int(x) for x in body.get("lineup_a") or []] or None
    lineup_b = [int(x) for x in body.get("lineup_b") or []] or None
    picks_a = parse_hero_list(conn, body.get("picks_a"))
    picks_b = parse_hero_list(conn, body.get("picks_b"))
    assign_a = [(int(p), int(h)) for p, h in body["assign_a"]] if body.get("assign_a") else None
    assign_b = [(int(p), int(h)) for p, h in body["assign_b"]] if body.get("assign_b") else None
    bo = int(body.get("bo") or 1)
    market = body.get("market") or "series"
    # идущая серия: счёт и series_id берём из базы, если их не задали руками
    from .schedule import series_state, decided
    state = series_state(conn, a["team_id"], b["team_id"], int(time.time()) - 14 * 3600)
    if state and decided(state, state["bo"] or bo):
        state = None
    if state and not body.get("series_id"):
        body = dict(body, series_id=state["series_id"])
        if not int(body.get("lead_a") or 0) and not int(body.get("lead_b") or 0):
            body["lead_a"], body["lead_b"] = state["a"], state["b"]
    res = pr.predict(a["team_id"], b["team_id"], lineup_a, lineup_b, picks_a, picks_b,
                     side=body.get("side") or "unknown", series_id=body.get("series_id"),
                     assign_a=assign_a, assign_b=assign_b, tier=body.get("tier") or "professional", bo=bo)
    pn, hn = pr.player_names, pr.hero_names
    out = {
        "team_a": {"id": a["team_id"], "name": a["name"], "logo": a["logo_url"]},
        "team_b": {"id": b["team_id"], "name": b["name"], "logo": b["logo_url"]},
        "p": res["p"], "p_radiant": res["p_radiant"], "p_dire": res["p_dire"], "mode": res["mode"], "side": res["side"],
        "warnings": res.get("warnings", []), "draft_only": res.get("draft_only"),
        "canon_a": res["detail"].get("canon_a"), "canon_b": res["detail"].get("canon_b"),
        "features": res["features"], "contributions": res["contributions"],
        "sides": {"a": res["detail"]["a"], "b": res["detail"]["b"]}, "h2h": res["detail"]["h2h"],
        "lineup_a": [{"id": p, "name": pn.get(p, str(p))} for p in res["lineup_a"]],
        "lineup_b": [{"id": p, "name": pn.get(p, str(p))} for p in res["lineup_b"]],
        "draft_a": [{"id": p, "name": pn.get(p, str(p)), "hero": h, "hero_name": hn.get(h, str(h))} for p, h in (res["draft_a"] or [])],
        "draft_b": [{"id": p, "name": pn.get(p, str(p)), "hero": h, "hero_name": hn.get(h, str(h))} for p, h in (res["draft_b"] or [])],
        "text": pr.describe(res, a["name"], b["name"]),
    }
    for k in ("draft_a", "draft_b"):
        if k in res["detail"]:
            d = dict(res["detail"][k])
            d["heroes"] = [hn.get(h, str(h)) for h in d["heroes"]]
            out["sides"][k] = d
    if bo > 1:
        dist = series_distribution(res["p"], bo, int(body.get("lead_a") or 0), int(body.get("lead_b") or 0))
        out["series"] = {"bo": bo, "p_a": dist["p_a"], "p_b": dist["p_b"], "p_draw": dist["p_draw"],
                         "scores": [{"score": "%d-%d" % s, "p": p} for s, p in dist["scores"].items()]}
    p_bet = out["series"]["p_a"] if (bo > 1 and market != "map") else res["p"]
    map_no = int(body.get("lead_a") or 0) + int(body.get("lead_b") or 0) + 1
    out.update({"market": market, "map_no": map_no, "lead_a": int(body.get("lead_a") or 0),
                "lead_b": int(body.get("lead_b") or 0)})
    if body.get("odds_a") and body.get("odds_b"):
        out["odds"] = evaluate(p_bet, float(body["odds_a"]), float(body["odds_b"]))
        out["odds"].update({"odds_a": float(body["odds_a"]), "odds_b": float(body["odds_b"])})
    out["p_bet"] = p_bet
    from . import policy
    scores = [(x["score"], x["p"]) for x in out["series"]["scores"]] if bo > 1 else []
    out["verdict"] = policy.verdict(res, a["name"], b["name"], p_bet, scores, bo,
                                    float(body["odds_a"]) if body.get("odds_a") else None,
                                    float(body["odds_b"]) if body.get("odds_b") else None,
                                    policy.settings(conn), confidence=body.get("confidence"), market=market,
                                    map_no=map_no, ref_a=float(body["ref_a"]) if body.get("ref_a") else None,
                                    ref_b=float(body["ref_b"]) if body.get("ref_b") else None,
                                    ref_book=body.get("ref_book"))
    from . import players
    try:
        out["players"] = players.matchup(pr, conn, res, a["team_id"], b["team_id"], a["name"], b["name"], int(time.time()))
    except Exception as e:
        out["players"] = {"a": {"players": []}, "b": {"players": []}, "lines": [], "duel": None,
                          "caveat": "разрез по игрокам недоступен: %r" % e}
    from . import draft as draft_mod
    try:
        out["draft"] = draft_mod.analyze(pr, conn, res, a["team_id"], b["team_id"], a["name"], b["name"],
                                          with_pubs=body.get("pubs", True) not in (False, "0", 0))
    except Exception as e:
        out["draft"] = {"error": repr(e)}
    from . import feed
    out["form_a"], out["form_b"] = feed.team_form(conn, a["team_id"], 10), feed.team_form(conn, b["team_id"], 10)
    out["h2h_rows"] = feed.h2h_rows(conn, a["team_id"], b["team_id"])
    out["checks"] = feed.checks(conn, res)
    out["analysis"] = feed.analysis_latest(a["name"], b["name"])
    out["tier"], out["bo"] = body.get("tier") or "professional", bo
    from . import markets as mk
    spec = body.get("markets") or {}
    lines = {k: v.get("line") for k, v in spec.items() if v.get("line")}
    if lines.get("hcp_a") or lines.get("hcp_b"):
        lines["hcp"] = lines.get("hcp_a") or lines.get("hcp_b")
    modds = {k: (float(v["odds_yes"]), float(v["odds_no"])) for k, v in spec.items()
             if v.get("odds_yes") and v.get("odds_no")}
    full_dist = series_distribution(res["p"], bo, int(body.get("lead_a") or 0), int(body.get("lead_b") or 0)) if bo > 1 else None
    out["markets"] = mk.build(pr, res, a["name"], b["name"], bo, full_dist, policy.settings(conn), lines, modds,
                              int(body.get("lead_a") or 0), int(body.get("lead_b") or 0))
    return res, out, a, b


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---------------------------------------------------------- ответы

    def send_json(self, obj, status=HTTPStatus.OK):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
        if gz:
            data = gzip.compress(data)
        self.send_response(status)
        self.send_header("Content-Type", MIME[".json"])
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, cache="no-cache"):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", MIME.get(os.path.splitext(path)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def send_static(self, rel):
        path = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not path.startswith(WEB_DIR + os.sep) or os.path.splitext(path)[1] not in MIME:
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        self.send_file(path, cache="max-age=86400" if path.endswith(".woff2") else "no-cache")

    def send_media(self, rel):
        """Файл из data/media с поддержкой Range: без неё браузер не зацикливает и не перематывает видео."""
        from . import rails
        from urllib.parse import unquote
        path = rails.media_path(unquote(rel))
        if not path:
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        size = os.path.getsize(path)
        start, end, status = 0, size - 1, HTTPStatus.OK
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].partition("-")
            try:
                if a:
                    start, end = int(a), min(int(b), size - 1) if b else size - 1
                else:
                    start = max(0, size - int(b))
                status = HTTPStatus.PARTIAL_CONTENT
            except ValueError:
                start, end = 0, size - 1
            if start > end or start >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(status)
        self.send_header("Content-Type", rails.MEDIA_MIME[os.path.splitext(path)[1].lower()])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        left = end - start + 1
        try:
            with open(path, "rb") as f:
                f.seek(start)
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    # ---------------------------------------------------------- маршруты

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                return self.send_file(os.path.join(WEB_DIR, "index.html"))
            if u.path.startswith("/static/"):
                return self.send_static(u.path[len("/static/"):])
            if u.path.startswith("/media/"):
                return self.send_media(u.path[len("/media/"):])
            if u.path == "/api/rails":
                from . import rails
                return self.send_json(rails.load())
            if u.path == "/api/history":
                from . import feed
                return self.send_json(feed.history(connect(), days=qint(q, "days", 14), limit=qint(q, "limit", 400)))
            if u.path == "/api/status":
                return self.api_status()
            if u.path == "/api/teams":
                return self.api_teams(q)
            if u.path == "/api/team":
                return self.api_team(q)
            if u.path == "/api/heroes":
                conn = connect()
                return self.send_json([{"id": r["hero_id"], "name": r["localized_name"],
                                        "key": (r["name"] or "").replace("npc_dota_hero_", "")} for r in
                                       conn.execute("select hero_id, localized_name, name from heroes order by localized_name")])
            if u.path == "/api/live":
                from . import live as live_mod
                conn = connect()
                from . import feed
                with _calc:                     # страница опрашивает раз в 30 с: один запрос к источнику на все вкладки
                    if time.time() - _live["ts"] > LIVE_TTL:
                        _live.update(ts=time.time(), data=live_mod.games(conn))
                    res_live = _live["data"]
                    games = feed.live_view(conn, predictor(), res_live["games"])
                for g in games:
                    g["logo_radiant"] = team_logo(conn, g["team_id_radiant"])
                    g["logo_dire"] = team_logo(conn, g["team_id_dire"])
                return self.send_json({"source": res_live["source"], "error": res_live["error"], "games": games})
            if u.path == "/api/book/backtest":
                from . import oddsbook, oddsapi
                rep = oddsbook.backtest(connect(), q.get("book", [None])[0])
                rep["text"] = oddsbook.describe(rep)
                rep["key"] = oddsapi.available()
                return self.send_json(rep)
            if u.path == "/api/backtest":
                from .backtest import load_report
                return self.send_json(load_report() or {})
            if u.path == "/api/bets":
                from .bets import list_rows, report
                conn = connect()
                return self.send_json({"rows": list_rows(conn, qint(q, "limit", 100)), "report": report(conn)})
            if u.path == "/api/forecasts":
                from .schedule import forecasts
                with _calc:
                    data = forecasts(connect(), predictor(), include_done=q.get("done", ["0"])[0] == "1")
                return self.send_json(data)
            if u.path == "/api/analyze":
                return self.api_analyze(q)
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except SystemExit as e:
            self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        except Exception as e:
            self.log_message("ошибка %s: %r", u.path, e)
            self.send_json({"error": repr(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            body = self.read_body()
            conn = connect()
            if u.path == "/api/predict":
                _, out, _, _ = do_predict(conn, body)
                return self.send_json(out)
            if u.path == "/api/series":
                from .series import series_distribution
                d = series_distribution(float(body["p"]), int(body.get("bo") or 3), int(body.get("lead_a") or 0),
                                        int(body.get("lead_b") or 0), float(body.get("momentum") or 0))
                d["scores"] = [{"score": "%d-%d" % s, "p": p} for s, p in d["scores"].items()]
                return self.send_json(d)
            if u.path == "/api/bracket":
                return self.api_bracket(conn, body)
            if u.path == "/api/bets":
                return self.api_bet_record(conn, body)
            if u.path == "/api/bets/settle":
                from .bets import settle
                return self.send_json({"settled": settle(conn)})
            if u.path == "/api/bets/close":
                from .bets import close
                d = close(conn, int(body["id"]), float(body["closing_a"]), float(body["closing_b"]))
                return self.send_json(d)
            if u.path == "/api/bets/config":
                from .policy import configure
                return self.send_json(configure(conn, **{k: body.get(k) for k in ("bankroll", "blend", "min_edge", "max_stake", "min_bo", "flat")}))
            if u.path == "/api/schedule":
                from . import schedule
                if body.get("text"):
                    added, errors = schedule.add_text(conn, body["text"])
                    return self.send_json({"added": added, "errors": errors})
                sid = schedule.add(conn, body["team_a"], body["team_b"], int(body.get("bo") or 3),
                                   body.get("tier") or "professional", body.get("starts"),
                                   float(body["odds_a"]) if body.get("odds_a") else None,
                                   float(body["odds_b"]) if body.get("odds_b") else None, body.get("note"))
                return self.send_json({"added": [sid], "errors": []})
            if u.path == "/api/schedule/update":
                from . import schedule
                schedule.update(conn, int(body["id"]), **{k: body[k] for k in body if k != "id"})
                return self.send_json({"ok": True})
            if u.path == "/api/schedule/delete":
                from . import schedule
                schedule.remove(conn, int(body["id"]))
                return self.send_json({"ok": True})
            if u.path == "/api/ingame":
                from . import ingame
                b = ingame.load(conn)
                if not b:
                    raise SystemExit("модель по ходу карты не обучена: нажмите «Синхронизировать»")
                p, minute = float(body["p"]), int(body.get("minute") or 20)
                lead = body.get("lead")
                rep = b["report"].get(str(min(ingame.MINUTES, key=lambda m: abs(m - minute))))
                return self.send_json({"p": ingame.predict(b, p, minute, float(lead)) if lead not in (None, "") else None,
                                       "table": ingame.table(b, p, minute), "minute": minute, "check": rep})
            if u.path == "/api/book/sync":
                from . import oddsbook
                return self.send_json(oddsbook.sync_upcoming(conn, days=int(body.get("days") or 3), log=lambda m: None))
            if u.path == "/api/stop":
                from .ai import stop_current
                return self.send_json({"stopped": stop_current()})
            if u.path == "/api/sync":
                return self.api_sync(body)
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except SystemExit as e:
            self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        except Exception as e:
            self.log_message("ошибка %s: %r", u.path, e)
            self.send_json({"error": repr(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    # ---------------------------------------------------------- API

    def api_status(self):
        from .backtest import load_report
        conn = connect()
        c = counts(conn)
        r = load_report()
        ch = (r or {}).get("chosen") or {}
        model = None
        if r and ch.get("post") in r.get("models", {}):
            post, pre = r["models"][ch["post"]], r["models"].get(ch.get("pre")) or {}
            model = {"built": r.get("built"), "n": post["overall"]["n"], "acc_post": post["overall"]["acc"],
                     "acc_pre": (pre.get("overall") or {}).get("acc"), "acc_elo": post["baseline_elo"]["acc"],
                     "logloss_post": post["overall"]["logloss"], "logloss_elo": post["baseline_elo"]["logloss"],
                     "brier_post": post["overall"]["brier"], "features_post": len(post["features"]),
                     "acc_draft": ((r["models"].get("draft") or {}).get("overall") or {}).get("acc"),
                     "logloss_draft": ((r["models"].get("draft") or {}).get("overall") or {}).get("logloss"),
                     "features_pre": len(pre.get("features") or [])}
        self.send_json({"db": c, "last_sync": kv_get(conn, "last_sync"), "budget": od.BUDGET.status(), "model": model,
                        "backtest": {k: v["overall"] for k, v in (r or {}).get("models", {}).items()},
                        "sync": _sync, "claude": self.claude_available(), "sources": self.sources()})

    @staticmethod
    def sources():
        from . import oddsapi, live, notify
        return {"odds": oddsapi.available(), "steam": live.steam_available(), "telegram": notify.configured()}

    @staticmethod
    def claude_available():
        from .ai import claude_exe
        try:
            return bool(claude_exe())
        except FileNotFoundError:
            return False

    def api_teams(self, q):
        from . import ratings
        conn = connect()
        s = "%" + (q.get("q", [""])[0]) + "%"
        rows = conn.execute(
            "select t.team_id, t.name, t.tag, t.rating, t.logo_url, coalesce((select max(m.start_time) from matches m "
            "where m.radiant_team_id=t.team_id or m.dire_team_id=t.team_id), t.last_match_time) as last_match_time "
            "from teams t where t.name like ? or t.tag like ? order by last_match_time desc limit ?",
            (s, s, qint(q, "limit", 20))).fetchall()
        out = []
        for t in rows:
            elo, games = ratings.current(conn, "team", t["team_id"])
            out.append({"id": t["team_id"], "name": t["name"], "tag": t["tag"], "elo": round(elo), "games": games,
                        "last": t["last_match_time"], "logo": t["logo_url"]})
        self.send_json(out)

    def api_team(self, q):
        from .cli_data import actual_roster, team_matches, player_role, departed
        from . import ratings
        conn = connect()
        tid = qint(q, "id")
        pr = predictor()
        roster, last_ts = actual_roster(conn, tid)
        gone = departed(conn, tid, roster, last_ts)
        teams = {r["team_id"]: r["name"] for r in conn.execute("select team_id, name from teams")}
        trow = conn.execute("select name from teams where team_id=?", (tid,)).fetchone()
        team_name = trow["name"] if trow else str(tid)
        out = {"id": tid, "name": team_name, "last_ts": last_ts, "logo": team_logo(conn, tid),
               "roster": [{"id": p, "name": pr.player_names.get(p, str(p)), "elo": round(ratings.current(conn, "player", p)[0]),
                           "role": player_role(conn, p),
                           "departed": ("%s, %s" % (gone[p][1], time.strftime("%Y-%m-%d", time.gmtime(gone[p][2])))) if p in gone else None}
                          for p in roster],
               "matches": [{"ts": m["start_time"], "won": m["won"], "opp": teams.get(m["opp"], str(m["opp"])),
                            "radiant": bool(m["is_radiant"]), "tier": m["tier"]} for m in team_matches(conn, tid, 15)]}
        # кандидаты в состав: все, кто играл за команду за 120 дней
        since = int(time.time()) - 120 * 86400
        cands = conn.execute(
            "select mp.account_id, count(*) n from match_players mp join matches m on m.match_id=mp.match_id "
            "where m.start_time>=? and ((m.radiant_team_id=? and mp.is_radiant=1) or (m.dire_team_id=? and mp.is_radiant=0)) "
            "and mp.account_id is not null group by mp.account_id order by n desc limit 12", (since, tid, tid)).fetchall()
        out["candidates"] = [{"id": r["account_id"], "name": pr.player_names.get(r["account_id"], str(r["account_id"])),
                              "games": r["n"]} for r in cands]
        self.send_json(out)

    def api_bracket(self, conn, body):
        from .cli_data import resolve_team
        from .series import simulate_bracket
        pr = predictor()
        bracket = body["bracket"]
        ids = [resolve_team(conn, t)["team_id"] for t in bracket["teams"]]
        names = {i: conn.execute("select name from teams where team_id=?", (i,)).fetchone()["name"] for i in ids}
        with _calc:
            res = simulate_bracket(dict(bracket, teams=ids), lambda a, b: pr.predict(a, b)["p"],
                                   runs=int(body.get("runs") or 10000))
        self.send_json({"champion": sorted(({"team": names[t], "p": p} for t, p in res["champion"].items()),
                                           key=lambda x: -x["p"]),
                        "reached": {k: sorted(({"team": names[t], "p": p} for t, p in v.items() if p > 0),
                                              key=lambda x: -x["p"]) for k, v in res["reached"].items()}})

    def api_bet_record(self, conn, body):
        from . import bets
        res, out, a, b = do_predict(conn, body)
        mkey = body.get("market_key")
        if mkey:
            m = next((x for x in out["markets"] if x["key"] == mkey), None)
            if not m:
                raise SystemExit("рынок %s недоступен для этого матча" % mkey)
            stale = any("играли за другие команды" in w for w in res.get("warnings", []))
            pid = bets.record(conn, res["mode"], a["team_id"], b["team_id"], a["name"], b["name"], m["p_yes"],
                              bo=int(body.get("bo") or 1), odds_a=m["odds_yes"], odds_b=m["odds_no"],
                              side=body.get("bet_side"), stake=float(body["stake"]) if body.get("stake") else None,
                              series_id=body.get("series_id"), tier=body.get("tier"), roster_change=stale,
                              paper=not (body.get("bet_side") and body.get("stake")), market=mkey,
                              map_no=out["map_no"] if mkey in ("kills", "dur") else None, line=m["line"],
                              pick="%s|%s" % (m["yes"], m["no"]), note=m["title"])
            r = conn.execute("select p_blend, policy_bet, policy_side, policy_stake, policy_reason from predictions where id=?", (pid,)).fetchone()
            return self.send_json({"id": pid, "policy": dict(r), "market": m["title"]})
        stale = any("играли за другие команды" in w for w in res.get("warnings", []))
        change = stale or res["detail"]["a"]["lineup_change"] > 0 or res["detail"]["b"]["lineup_change"] > 0
        pid = bets.record(conn, res["mode"], a["team_id"], b["team_id"], a["name"], b["name"], out["p_bet"],
                          bo=int(body.get("bo") or 1), picks={"a": body.get("picks_a"), "b": body.get("picks_b")},
                          odds_a=float(body["odds_a"]) if body.get("odds_a") else None,
                          odds_b=float(body["odds_b"]) if body.get("odds_b") else None,
                          side=body.get("bet_side"), stake=float(body["stake"]) if body.get("stake") else None,
                          p_claude=float(body["p_claude"]) if body.get("p_claude") else None,
                          series_id=body.get("series_id"), note=body.get("note"), tier=body.get("tier"),
                          roster_change=change, confidence=body.get("confidence"),
                          paper=not (body.get("bet_side") and body.get("stake")),
                          market=out["market"], map_no=out["map_no"] if out["market"] == "map" else None)
        r = conn.execute("select p_blend, policy_bet, policy_side, policy_stake, policy_reason from predictions where id=?", (pid,)).fetchone()
        self.send_json({"id": pid, "policy": dict(r)})

    def api_sync(self, body):
        if _sync["running"]:
            return self.send_json({"running": True})

        def job():
            _sync["running"] = True
            _sync["log"] = []
            try:
                from .ingest import sync
                from . import ratings, backtest
                conn = sync(fresh_limit=int(body.get("fresh") or 100), log=lambda m: _sync["log"].append(m))
                if body.get("train", True):
                    ratings.compute(conn, lambda m: _sync["log"].append(m))
                    backtest.run(conn, lambda m: _sync["log"].append(m), save=True)
                    reload_predictor()
            except Exception as e:
                _sync["log"].append("ошибка: %r" % e)
            finally:
                _sync["running"] = False
        threading.Thread(target=job, daemon=True).start()
        self.send_json({"running": True})

    def api_analyze(self, q):
        from . import ai, dossier, bets
        conn = connect()
        body = {k: v[0] for k, v in q.items()}
        for k in ("lineup_a", "lineup_b"):
            body[k] = [x for x in body.get(k, "").split(",") if x]
        res, out, a, b = do_predict(conn, body)
        pr = predictor()
        odds = (float(body["odds_a"]), float(body["odds_b"])) if body.get("odds_a") and body.get("odds_b") else None
        bo = int(body.get("bo") or 1)
        text = dossier.build(pr, conn, a["team_id"], b["team_id"], res, bo=bo, odds=odds)
        key = dossier.key_for(a["name"], b["name"])
        path = dossier.save(text, key)

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(event, data):
            try:
                self.wfile.write(("event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))).encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                ai.stop_current()

        emit("log", "досье %s (%d КБ), модель %s, усилие %s" % (key, len(text.encode("utf-8")) // 1024,
                                                                 body.get("model", "opus"), body.get("effort", "high")))
        try:
            answer, verdict = ai.analyze(path, emit, model=body.get("model"), effort=body.get("effort"),
                                         max_turns=int(body.get("max_turns") or 25))
        except RuntimeError as e:
            emit("log", str(e))
            emit("verdict", None)
            return
        pid = None
        if body.get("record") in ("1", "true"):
            stale = any("играли за другие команды" in w for w in res.get("warnings", []))
            change = stale or res["detail"]["a"]["lineup_change"] > 0 or res["detail"]["b"]["lineup_change"] > 0
            pid = bets.record(conn, res["mode"], a["team_id"], b["team_id"], a["name"], b["name"], out["p_bet"], bo=bo,
                              picks={"a": body.get("picks_a"), "b": body.get("picks_b")},
                              odds_a=odds[0] if odds else None, odds_b=odds[1] if odds else None,
                              p_claude=verdict["p_a"] if verdict else None, dossier=key, analysis=answer,
                              tier=body.get("tier"), roster_change=change,
                              confidence=verdict["confidence"] if verdict else None)
        emit("verdict", {"verdict": verdict, "prediction_id": pid, "dossier": key})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"   # на Windows второй сервер молча занял бы порт


def main(argv=None):
    from .ai import SETTINGS
    p = argparse.ArgumentParser(prog="dota2bets web")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--skip-permissions", action="store_true", help="claude с --dangerously-skip-permissions")
    args = p.parse_args(argv)
    SETTINGS["skip_permissions"] = args.skip_permissions
    srv = Server((args.host, args.port), Handler)
    url = "http://%s:%d/" % (args.host, args.port)
    sys.stderr.write("dota2bets web: %s\n" % url)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
