"""Headless Claude Code: запуск `claude -p` в папке проекта, стрим событий, разбор блока «Вердикт».

Промпт в stdin, `--output-format stream-json`, allowlist инструментов, одна задача за раз.
API-ключ не нужен — работает подписка Claude Code. Сервер запускать из обычного терминала.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time

from . import ROOT

ALLOWED_TOOLS = [
    "Bash(.venv/Scripts/python -m dota2bets:*)",
    "Bash(.venv/Scripts/python.exe -m dota2bets:*)",
    "PowerShell(.venv/Scripts/python -m dota2bets:*)",
    "PowerShell(.venv/Scripts/python.exe -m dota2bets:*)",
    "Bash(cat:*)", "Bash(head:*)", "Bash(ls:*)", "Bash(grep:*)",
    "Read", "Write", "Glob", "Grep", "Skill",
    "WebSearch", "WebFetch",      # только для проверки новостей о составах: замены, болезни, визы
]
MODELS = ("fable", "opus", "sonnet")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
SETTINGS = {"skip_permissions": False, "max_turns": 25}

_current = {"proc": None}
_job_lock = threading.Lock()

VERDICT_RE = re.compile(
    r"Вердикт:\s*(?P<team>.+?)\s*\|\s*p_A:\s*(?P<p>[01](?:[.,]\d+)?)\s*\|\s*уверенность:\s*(?P<conf>\S+)\s*\|\s*ставка:\s*(?P<bet>.+)",
    re.IGNORECASE)


def claude_exe():
    for name in ("claude.cmd", "claude.exe", "claude"):
        p = shutil.which(name)
        if p:
            return p
    raise FileNotFoundError("claude CLI не найден на PATH")


def claude_args(model=None, max_turns=None, effort=None):
    turns = min(int(max_turns or SETTINGS["max_turns"]), 60)
    args = [claude_exe(), "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--max-turns", str(turns)]
    if model in MODELS:
        args += ["--model", model]
    if effort in EFFORTS:
        args += ["--effort", effort]
    if SETTINGS["skip_permissions"]:
        args.append("--dangerously-skip-permissions")
    else:
        args += ["--permission-mode", "acceptEdits", "--allowedTools", *ALLOWED_TOOLS]
    return args


def _tool_summary(block):
    inp = block.get("input") or {}
    name = block.get("name", "?")
    if name in ("Bash", "PowerShell"):
        return "%s: %s" % (name, inp.get("command", "")[:300])
    if name in ("Read", "Write", "Edit"):
        return "%s: %s" % (name, inp.get("file_path", ""))
    if name == "Skill":
        return "Skill: /%s %s" % (inp.get("skill", ""), inp.get("args", ""))
    return "%s: %s" % (name, json.dumps(inp, ensure_ascii=False)[:200])


def run_claude(prompt, emit, model=None, max_turns=None, effort=None):
    """Запуск claude -p; emit(event, data) на каждое событие. Возвращает итоговый текст ответа."""
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        claude_args(model, max_turns, effort), cwd=ROOT, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    _current["proc"] = proc
    result_text = ""
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        started = time.time()
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                emit("log", line[:500])
                continue
            t = ev.get("type")
            if t == "stream_event":
                e = ev.get("event", {})
                if e.get("type") == "message_start":
                    emit("text", "\n\n")
                elif e.get("type") == "content_block_delta":
                    d = e.get("delta", {})
                    if d.get("type") == "text_delta":
                        emit("text", d.get("text", ""))
            elif t == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        emit("tool", _tool_summary(block))
            elif t == "user":
                for block in ev.get("message", {}).get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        content = block.get("content")
                        if isinstance(content, list):
                            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                        content = str(content or "")
                        emit("tool_result", content[:400] + ("…" if len(content) > 400 else ""))
            elif t == "result":
                result_text = ev.get("result", "") or ""
                emit("done", json.dumps({
                    "is_error": ev.get("is_error", False), "cost_usd": ev.get("total_cost_usd"),
                    "duration_s": round(time.time() - started, 1), "num_turns": ev.get("num_turns"),
                    "subtype": ev.get("subtype"),
                }, ensure_ascii=False))
            elif t == "system" and ev.get("subtype") == "init":
                emit("log", "session %s model %s" % (ev.get("session_id"), ev.get("model")))
        proc.wait()
        if proc.returncode not in (0, None):
            emit("log", "claude завершился с кодом %s" % proc.returncode)
    finally:
        _current["proc"] = None
    return result_text


def stop_current():
    proc = _current.get("proc")
    if proc and proc.poll() is None:
        proc.kill()
        return True
    return False


def busy():
    return _job_lock.locked()


def parse_verdict(text):
    """Блок «Вердикт: <команда> | p_A: 0.xx | уверенность: ... | ставка: ...» → dict или None."""
    if not text:
        return None
    m = None
    for m in VERDICT_RE.finditer(text):
        pass
    if not m:
        return None
    return {"team": m.group("team").strip(), "p_a": float(m.group("p").replace(",", ".")),
            "confidence": m.group("conf").strip().lower(), "bet": m.group("bet").strip()}


def analyze(dossier_path, emit, model=None, effort=None, max_turns=None):
    """Полный цикл: /analyze <досье> → текст ответа и разобранный вердикт."""
    if not _job_lock.acquire(blocking=False):
        raise RuntimeError("разбор уже идёт")
    try:
        rel = os.path.relpath(dossier_path, ROOT).replace("\\", "/")
        text = run_claude("/analyze %s" % rel, emit, model=model, effort=effort, max_turns=max_turns)
        return text, parse_verdict(text)
    finally:
        _job_lock.release()
