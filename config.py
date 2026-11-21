"""config.yaml を読み込み、パスを絶対パスに解決して提供する。

exe (PyInstaller) で動かすときは exe のあるフォルダを BASE_DIR にする。
- config.yaml と source/ の各ファイルは、exe の隣にないものだけ同梱分を書き出す
  (利用者の編集を上書きしない。新しい exe で増えたファイルは追加される)
- 画面 (web/) は利用者が編集しないので、常に exe に同梱したものを使う (RES_DIR)
"""
import os
import shutil
import sys
from pathlib import Path

import yaml

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    RES_DIR = Path(sys._MEIPASS)
    if not (BASE_DIR / "config.yaml").exists():
        shutil.copy2(RES_DIR / "config.yaml", BASE_DIR / "config.yaml")
    for _src in (RES_DIR / "source").glob("*"):
        _dst = BASE_DIR / "source" / _src.name
        if not _dst.exists():
            _dst.parent.mkdir(exist_ok=True)
            shutil.copy2(_src, _dst)
else:
    BASE_DIR = RES_DIR = Path(__file__).resolve().parent

with open(BASE_DIR / "config.yaml", encoding="utf-8") as f:
    _cfg = yaml.safe_load(f)

PATHS = {key: BASE_DIR / value for key, value in _cfg["paths"].items()}
CRAWL = _cfg["crawl"]
CHUNK = _cfg["chunk"]
NEWS_RSS = _cfg.get("news_rss", [])
LLM = _cfg["llm"]
RAG = _cfg["rag"]
AUTH = _cfg["auth"]
DISCORD = _cfg.get("discord", {})
TUNNEL = _cfg.get("tunnel", {})
SCHEDULE = _cfg.get("schedule") or {}


def secret(name):
    """環境変数、なければ BASE_DIR/.env (NAME=値 の行) から秘密情報を読む。"""
    if value := os.environ.get(name):
        return value
    env = BASE_DIR / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == name:
                return value.strip().strip('"').strip("'")
    return None
