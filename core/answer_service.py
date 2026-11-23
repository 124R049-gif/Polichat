"""質問1件の処理 (順番待ち → 資料の検索 → 回答の生成 → 回数の精算) を、入口に依らない形で提供する。

Web 画面・Discord Bot・Slack Bot はどれも answer() が返すイベントを自分の画面の形に直すだけにする。
回数を1つ使う (accounts.consume) のは入口の側で行う。断るときの返し方が入口ごとに違うため
(Web は HTTP 429、Bot は本人にだけ見えるメッセージ)。

イベントは dict で、type ごとに次のキーを持つ。
  queued   {}                        同時生成数の上限に達していて順番待ちになった (1回だけ)
  sources  {"sources": [行, ...]}    回答に使う資料 (rag.answer.retrieve の行。番号は1から順)
  delta    {"text": str}             回答の続き
  error    {"message": str}          回答できなかった。この質問の回数は戻してある
  done     {"remaining": int}        正常に終わった。本日の残り回数

同時に生成する回答の数は llm.max_concurrent まで。上限は Web と Bot の別プロセスをまたいで数える
(このPCの一時フォルダのロックファイルで数える。プロセスが落ちればロックは OS が外す)。
"""
import hashlib
import os
import tempfile
import time
import traceback
from pathlib import Path

from config import DISCORD, LLM, PATHS
from core import accounts
from database.db import get_connection
from llm.client import LLMError, chat_stream, endpoint
from rag.answer import NOT_FOUND, prepare

CPU_REFUSAL = ("現在は共用サーバーのGPUを使えない時間帯のため、回答を生成できません "
               "(利用できるのは 21時〜翌9時と日曜)。")
POLL_SECONDS = 1.0

# 旧設定 (discord.max_concurrent) しかなければそれを使う
MAX_CONCURRENT = max(int(LLM.get("max_concurrent") or DISCORD.get("max_concurrent") or 1), 1)
# 同じPCで別の場所に置いたコピーとは別に数える
_SLOT_DIR = Path(tempfile.gettempdir()) / (
    "nihon-kanshi-" + hashlib.sha256(str(PATHS["db"]).encode()).hexdigest()[:12])

if os.name == "nt":
    import msvcrt

    def _try_lock(fd):
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)


def _try_slot():
    """空いている生成枠を1つ取る。取れたらファイル記述子、空きがなければ None。"""
    _SLOT_DIR.mkdir(parents=True, exist_ok=True)
    for i in range(MAX_CONCURRENT):
        fd = os.open(_SLOT_DIR / f"slot-{i}.lock", os.O_RDWR | os.O_CREAT)
        if _try_lock(fd):
            return fd
        os.close(fd)
    return None


def _release(fd):
    try:
        _unlock(fd)
    finally:
        os.close(fd)


def answer(user, question, history=None, filters=None, day=None, plan_fn=None):
    """質問1件を処理し、イベント (モジュールの説明を参照) を順に返す generator。

    user: 回数を消費済みの利用者の行。day: 消費した日 (日付をまたいでも正しく戻すため)。
    history: 過去のやりとり [{"role", "content"}]。filters: rag.answer.allowed_chunks を参照。
    plan_fn: 資料の検索と段取りを差し替えるとき (URL の解説など) に渡す、引数なしで
      (段取り, 資料のリスト) を返す関数。省略すると質問への回答 (rag.answer.prepare)。
    途中で利用者が中止した (generator が閉じられた) 場合は、生成を始めているので回数は戻さない。"""
    day = day or accounts.today()
    fd = None
    try:
        if endpoint("chat")[2] == "cpu":
            # CPU では1問に30分以上かかり終わらないため、受け付けない
            accounts.refund(user["id"], day)
            yield {"type": "error", "message": CPU_REFUSAL}
            return
        fd = _try_slot()
        if fd is None:
            yield {"type": "queued"}
            while (fd := _try_slot()) is None:
                time.sleep(POLL_SECONDS)
        plan, sources = plan_fn() if plan_fn else prepare(question, history, filters)
        yield {"type": "sources", "sources": sources}
        if plan is None:
            yield {"type": "delta", "text": NOT_FOUND}
        else:
            # 本体と「議員個人の発言」「非公式の論評」の節を順に生成してつなげる
            for prefix, messages in plan:
                if prefix:
                    yield {"type": "delta", "text": prefix}
                for text in chat_stream("chat", messages) if messages else ():
                    yield {"type": "delta", "text": text}
    except LLMError as e:
        accounts.refund(user["id"], day)
        yield {"type": "error", "message": f"{e} (この質問は回数に数えません)"}
        return
    except Exception as e:
        traceback.print_exc()
        accounts.refund(user["id"], day)
        yield {"type": "error", "message": f"内部エラー: {type(e).__name__} (この質問は回数に数えません)"}
        return
    finally:
        if fd is not None:
            _release(fd)
    yield {"type": "done", "remaining": accounts.remaining(user)}


def service_status():
    """入口に共通の状態表示: 使用中のモデル・GPU/CPU・資料の数。"""
    _, model, device = endpoint("chat")
    conn = get_connection()
    documents, sources = conn.execute("""SELECT COUNT(*), COUNT(DISTINCT source_id) FROM documents
                                         WHERE status = 'extracted'""").fetchone()
    conn.close()
    return {"device": device, "model": model, "documents": documents, "sources": sources}
