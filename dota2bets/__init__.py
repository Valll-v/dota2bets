"""dota2bets — анализ про-матчей Dota 2 для ставок."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("DOTA2BETS_DATA") or os.path.join(ROOT, "data")
