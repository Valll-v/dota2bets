"""Синтаксическая проверка inline-скрипта dota2bets/web/index.html через node --check.

    .venv/Scripts/python scripts/check_page.py

Одна синтаксическая ошибка молча убивает все кнопки на странице, поэтому прогонять после каждой правки.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "dota2bets", "web", "index.html")


def main():
    node = shutil.which("node")
    if not node:
        print("node не найден на PATH — проверка пропущена")
        return 0
    with open(PAGE, encoding="utf-8") as f:
        html = f.read()
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    if not scripts:
        print("inline-скрипт не найден")
        return 1
    rc = 0
    for i, src in enumerate(scripts):
        path = os.path.join(tempfile.gettempdir(), "dota2bets_page_%d.js" % i)
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        if r.returncode:
            print(r.stderr)
            rc = 1
        else:
            print("script %d: ok (%d строк)" % (i, src.count("\n")))
    return rc


if __name__ == "__main__":
    sys.exit(main())
