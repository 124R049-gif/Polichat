"""Bot (Discord / Slack など) に共通の部分。各 Bot はサービス固有の表示と操作だけを書く。

- 利用者の受付 (外部の利用者IDを "discord:<ID>" のような利用者にし、停止中・回数の上限を確かめる)
- core.answer_service.answer() のイベントを、別スレッドから asyncio で受け取る橋渡し
- 長い回答の分割、出典の一覧のための値
"""
import asyncio
import threading

from core import accounts
from core.answer_service import answer
from rag.answer import ORG_LABELS

EDIT_INTERVAL = 1.5    # 生成中に表示を更新する間隔 (秒)。短すぎると各サービスの速度制限にかかる
_END = object()


class Refused(Exception):
    """質問を受け付けない理由 (本人にだけ見せる文)。"""


def accept(external_id):
    """外部サービスの利用者ID (例 "discord:123", "slack:T01:U02") の質問を1回分受け付ける。

    受け付けたら (利用者の行, 消費した日) を返す。受け付けられなければ Refused を投げる。
    DB に触るので、asyncio の中からは asyncio.to_thread で呼ぶ。"""
    user = accounts.external_user(external_id)
    if user["disabled"]:
        raise Refused("利用が停止されています。")
    day = accounts.today()
    if not accounts.consume(user):
        raise Refused(accounts.limit_message(user))
    return user, day


def remaining(external_id):
    """(本日の残り回数, 1日の上限)。"""
    user = accounts.external_user(external_id)
    return accounts.remaining(user), accounts.daily_limit(user)


async def events(user, day, question, history=None, plan_fn=None):
    """answer() を別スレッドで動かし、イベントを順に返す async generator。

    生成は時間がかかり、DB や LLM への処理は同期的なので、Bot の接続を止めないよう別スレッドにする。
    plan_fn は answer() を参照 (URL の解説などで資料の検索と段取りを差し替える)。"""
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()

    def work():
        try:
            for ev in answer(user, question, history, None, day, plan_fn):
                loop.call_soon_threadsafe(queue.put_nowait, ev)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _END)

    threading.Thread(target=work, daemon=True, name="answer").start()
    while (ev := await queue.get()) is not _END:
        yield ev


def explain_plan(page, question=None):
    """URL の解説 (rag.explain) を events() の plan_fn にする。page は core.url_reader.read の結果。"""
    from rag.explain import prepare
    return lambda: prepare(page, question)


def pages(text, limit):
    """長い回答を limit 文字以下に分ける。なるべく改行で切る。"""
    result = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        result.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return result + [text]


def source_items(sources):
    """出典の一覧を、各サービスで整形しやすい dict のリストにする。番号は1から。"""
    return [{"n": n, "title": (r["title"] or "無題"), "url": r["url"],
             "page": f" p.{r['page']}" if r["page"] else "",
             "source_name": r["source_name"],
             "org_label": ORG_LABELS.get(r["org_type"], r["org_type"]),
             "date": r["published_date"] or "日付不明"}
            for n, r in enumerate(sources, 1)]
