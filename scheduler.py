"""資料の更新 (収集 → 本文抽出 → ニュース見出し → タグ付け → 埋め込み) をまとめて、または定時に実行する。

  python main.py update          1回だけ実行
  python main.py update --loop   config.yaml の schedule.times の時刻に繰り返し実行 (Ctrl+C で終了)

serve / bot の起動中も、schedule.times があれば裏で同じ時刻に実行する。
同時に2つ走らないよう data/update.lock で排他する (別プロセスの update とも)。
結果の要約は data/update.log に追記する。
"""
import os
import threading
import time
import traceback
from datetime import datetime, timedelta

from config import PATHS, SCHEDULE

LOCK = PATHS["db"].parent / "update.lock"
LOG = PATHS["db"].parent / "update.log"
STALE_HOURS = 12   # これより古いロックは異常終了の残骸とみなす
ALL_STEPS = ["collect", "extract", "news", "tag", "embed"]


def _log(message):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    print(f"[更新] {line}")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _acquire():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime > STALE_HOURS * 3600:
        LOCK.unlink(missing_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def _step(name):
    if name == "collect":
        from data_collection.collector import collect
        collect()
        return ""
    if name == "extract":
        from parsing.dates import fill_dates
        from parsing.extract import extract_pending
        from parsing.quality import mark_excluded
        result = extract_pending()
        fill_dates()
        mark_excluded()
        return ", ".join(f"{k} {v}" for k, v in result.items())
    if name == "news":
        from data_collection.rss import run
        run()
        return ""
    if name == "tag":
        from parsing.tagger import tag_pending
        return f"{tag_pending()} 件"
    if name == "embed":
        from rag.vector_db import embed_pending
        return f"{embed_pending()} チャンク"
    raise ValueError(f"不明な手順: {name}")


def run_update(steps=None):
    """手順を順に実行する。1つが失敗しても残りは続ける。成功なら True。"""
    from llm.client import LLMError
    steps = steps or SCHEDULE.get("steps") or ALL_STEPS
    if not _acquire():
        _log("別の更新が実行中のため見送りました")
        return False
    ok = True
    started = time.monotonic()
    _log("開始: " + " → ".join(steps))
    try:
        for name in steps:
            t = time.monotonic()
            try:
                detail = _step(name)
                _log(f"  {name}: 完了 ({time.monotonic() - t:.0f}秒) {detail}".rstrip())
            except LLMError as e:
                # LLM サーバーに届かないときはタグ付け・埋め込みだけ次回に回す
                ok = False
                _log(f"  {name}: LLM に接続できないため見送り ({e})")
            except Exception as e:
                ok = False
                _log(f"  {name}: 失敗 {type(e).__name__}: {e}")
                traceback.print_exc()
    finally:
        LOCK.unlink(missing_ok=True)
    _log(f"終了 ({time.monotonic() - started:.0f}秒){'' if ok else ' 一部失敗あり'}")
    return ok


def _times():
    result = []
    for s in SCHEDULE.get("times") or []:
        h, m = str(s).split(":")
        result.append((int(h), int(m)))
    return sorted(result)


def next_run(now=None):
    """次に実行する日時。schedule.times が空なら None。"""
    times = _times()
    if not times:
        return None
    now = now or datetime.now()
    for day in (0, 1):
        for h, m in times:
            at = (now + timedelta(days=day)).replace(hour=h, minute=m, second=0, microsecond=0)
            if at > now:
                return at
    return None


def loop():
    """schedule.times の時刻ごとに run_update を呼び続ける。"""
    while (at := next_run()) is not None:
        _log(f"次回の更新: {at:%m/%d %H:%M}")
        # PC のスリープ復帰などで時刻がずれても気づけるよう、短い間隔で確認する
        while datetime.now() < at:
            time.sleep(min(60, max(1, (at - datetime.now()).total_seconds())))
        run_update()


def start_background():
    """schedule.times があれば、定時更新を裏のスレッドで始める (serve / bot から呼ぶ)。"""
    if not _times():
        return
    threading.Thread(target=loop, daemon=True, name="scheduled-update").start()
