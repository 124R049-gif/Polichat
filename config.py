"""config.yaml を読み込み、パスを絶対パスに解決して提供する。"""
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent

with open(BASE_DIR / "config.yaml", encoding="utf-8") as f:
    _cfg = yaml.safe_load(f)

PATHS = {key: BASE_DIR / value for key, value in _cfg["paths"].items()}
CRAWL = _cfg["crawl"]
CHUNK = _cfg["chunk"]
NEWS_RSS = _cfg.get("news_rss", [])
LLM = _cfg["llm"]
RAG = _cfg["rag"]
AUTH = _cfg["auth"]
