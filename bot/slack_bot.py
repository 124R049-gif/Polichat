"""Slack Bot。/ask・メンション・DM で資料に基づく出典付き回答を返す。

Socket Mode で Slack へ外向きに接続するだけなので、公開 URL・ポート開放・HTTPS の用意は要らない。
利用者の受付・回答の生成・回数の精算は Bot 共通 (bot/common.py) と core/ が行い、
ここは Slack での表示と操作だけを書く。利用回数は "slack:<ワークスペースID>:<利用者ID>" として数える。

- @Bot 質問       メンションした投稿のスレッドに回答する。スレッド内でのメンションは続きの質問になり、
                  スレッドのやりとりを history として渡す。メンションのない発言には反応しない
- DM              (slack.allow_dm が true のとき) Bot への DM がそのまま質問になる
- /status         モデル・資料数・本日の残り回数 (本人にだけ表示)
- /ask 質問       (slack.slash_ask が true のときだけ) チャンネルに回答を投稿する。
                  出典・長文の続きはその投稿のスレッドに付ける

意図しない回答 (暴発) を避けるため、編集・Bot の発言・subtype 付きのイベントには反応せず、
同じ投稿 (Socket Mode の再送) には1回だけ答え、同じ利用者の生成中の質問は回数を使う前に断る。
"""
import asyncio
import collections
import html
import re
import time

from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError

from bot import common
from config import SLACK, secret
from core import accounts
from core.answer_service import service_status

MESSAGE_LIMIT = 3500        # Slack は 4000 文字を超えると切れることがあるので余裕を持たせる
# chat.update は Tier 3 (1分に50回程度)。共通の間隔より短くはしない
EDIT_INTERVAL = max(common.EDIT_INTERVAL, float(SLACK.get("edit_interval") or 2.0))
HISTORY_MESSAGES = 6        # LLMに渡す過去のやりとり (Web 画面と同じく3往復分)
QUESTION_LIMIT = 2000
# 投稿に付けるメタデータの種類。スレッドから history を作るときに、回答と出典・通知を見分ける
META_ANSWER = "kanshi_answer"
META_OTHER = "kanshi_other"
CURSOR = " ▌"
_MENTION = re.compile(r"<@[A-Z0-9]+(?:\|[^>]*)?>")


def external_id(team_id, user_id):
    return f"slack:{team_id}:{user_id}"


def permitted(team_id, channel_id):
    """使ってよい場所か。ワークスペース・チャンネルは slack.allowed_*、DM は slack.allow_dm で決める。"""
    teams = {str(t) for t in SLACK.get("allowed_team_ids") or []}
    if teams and team_id not in teams:
        return False
    if channel_id.startswith("D"):
        return bool(SLACK.get("allow_dm", False))
    channels = {str(c) for c in SLACK.get("allowed_channel_ids") or []}
    return not channels or channel_id in channels


# ---- 書式 (mrkdwn) ----

def escape(text):
    """Slack の制御文字 (& < >) をエスケープする。<!channel> などの通知も起きなくなる。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _url(url):
    return (url.replace(" ", "%20").replace("<", "%3C").replace(">", "%3E").replace("|", "%7C"))


def _link(url, label):
    return f"<{_url(url)}|{label.replace('|', '｜')}>"


def to_mrkdwn(markdown):
    """LLM の Markdown を Slack の mrkdwn に直す (リンク・太字・見出し・箇条書き)。"""
    text = escape(markdown)
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
                  lambda m: _link(html.unescape(m[2]), m[1]), text)
    text = re.sub(r"^(\s*)[-*]\s+", r"\1• ", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"*\1*", text)
    return re.sub(r"^#{1,6}\s+(.+?)\s*#*$", r"*\1*", text, flags=re.M)


def from_mrkdwn(text):
    """history に入れるため、投稿の mrkdwn を素の文に戻す。"""
    text = re.sub(r"<(?:https?://[^|>]+)\|([^>]+)>", r"\1", text)
    return html.unescape(text)


def sources_text(sources, left):
    lines = ["*参照した資料*"]
    for s in common.source_items(sources):
        lines.append(f"*[{s['n']}]* {_link(s['url'], escape(s['title'][:80]))}{s['page']}\n"
                     f"      {escape(s['source_name'])} ({escape(s['org_label'])}) {s['date']}")
    lines.append(f"\n本日の残り {left} 回")
    return "\n".join(lines)


def question_of(text):
    return _MENTION.sub("", text or "").strip()


# ---- 表示 ----

class Reply:
    """1つの回答の表示。最初の投稿を chat.update で書き換え、長くなれば同じスレッドへ続きを投稿する。

    thread_ts が None なら最初の投稿がチャンネル直下になり、続き・出典はそのスレッドに付ける。"""

    def __init__(self, client, channel, thread_ts, header="", question=""):
        self.client, self.channel, self.thread_ts = client, channel, thread_ts
        self.header, self.question = header, question
        self.messages, self.shown = [], []   # 投稿の ts と、その表示中の内容
        self.last_edit = 0.0

    @property
    def root(self):
        return self.thread_ts or (self.messages[0] if self.messages else None)

    def _meta(self, page):
        # 先頭の投稿には、スレッドに人の発言がない場合 (/ask) の質問を残す
        return {"event_type": META_ANSWER,
                "event_payload": {"page": page, "q": self.question if page == 0 else ""}}

    async def show(self, body, final=False):
        text = self.header + body + ("" if final else CURSOR)
        for i, page in enumerate(common.pages(text, MESSAGE_LIMIT)):
            if i < len(self.messages):
                if self.shown[i] != page:
                    await self.client.chat_update(channel=self.channel, ts=self.messages[i], text=page,
                                                  metadata=self._meta(i))
            else:
                resp = await self.post(page, self._meta(i))
                self.messages.append(resp["ts"])
                self.shown.append(None)
            self.shown[i] = page
        self.last_edit = time.monotonic()

    async def post(self, text, metadata=None):
        return await self.client.chat_postMessage(
            channel=self.channel, thread_ts=self.root, text=text, unfurl_links=False, unfurl_media=False,
            metadata=metadata or {"event_type": META_OTHER, "event_payload": {}})


async def thread_history(client, channel, thread_ts, before_ts, bot_user_id):
    """スレッドのやりとりを [{"role", "content"}] にする。before_ts の投稿 (今の質問) より前だけ。

    人の発言は Bot へのメンションを含むもの (DM ではすべて)、Bot の発言は回答だけを使う。"""
    resp = await client.conversations_replies(channel=channel, ts=thread_ts, include_all_metadata=True,
                                              limit=200)
    history = []
    for m in resp["messages"]:
        if float(m["ts"]) >= float(before_ts):
            break
        meta = m.get("metadata") or {}
        if m.get("bot_id") or m.get("user") == bot_user_id:
            if meta.get("event_type") != META_ANSWER:
                continue
            payload = meta.get("event_payload") or {}
            text = m.get("text", "").removesuffix(CURSOR.strip()).rstrip()
            if payload.get("q"):
                history.append({"role": "user", "content": payload["q"]})
                text = text.split("\n\n", 1)[-1]   # 見出し (誰の質問か) を除く
            text = from_mrkdwn(text)
            if payload.get("page") and history and history[-1]["role"] == "assistant":
                history[-1]["content"] += "\n" + text
            else:
                history.append({"role": "assistant", "content": text})
        elif channel.startswith("D") or f"<@{bot_user_id}" in m.get("text", ""):
            if q := question_of(html.unescape(m.get("text", ""))):
                history.append({"role": "user", "content": q})
    return history[-HISTORY_MESSAGES:]


async def run_answer(reply, user, day, question, ext_id, history=None, notify=None):
    """回答を生成しながら reply を書き換え、最後に出典と残り回数をスレッドに付ける。

    notify(text): 本人にだけ見せる通知 (出典がないときの残り回数)。"""
    text, sources, error, status = "", [], None, "資料を検索しています…"
    async for ev in common.events(user, day, question, history):
        if ev["type"] == "queued":
            status = "順番待ちです (他の質問の回答を生成中)…"
            await _quietly(reply.show(status))
        elif ev["type"] == "sources":
            sources = ev["sources"]
        elif ev["type"] == "delta":
            text += ev["text"]
            if time.monotonic() - reply.last_edit >= EDIT_INTERVAL:
                await _quietly(reply.show(to_mrkdwn(text)))   # 途中の失敗は最後の表示でやり直す
        elif ev["type"] == "error":
            error = ev["message"]

    body = to_mrkdwn(text)
    if error:
        body += f"\n\n:warning: {escape(error)}"
    await reply.show(body, final=True)
    left, _ = await asyncio.to_thread(common.remaining, ext_id)
    if sources and not error:
        for page in common.pages(sources_text(sources, left), MESSAGE_LIMIT):
            await reply.post(page)
    elif not error and notify:
        await notify(f"本日の残り {left} 回")


async def _quietly(coro):
    try:
        await coro
    except SlackApiError as e:
        print(f"Slack の表示の更新に失敗しました: {e.response.get('error')}")


async def start(client, channel, thread_ts, user, day, question, ext_id, header="", slash_q="",
                history=None, notify=None):
    """最初の投稿をしてから回答を始める。投稿できなければ回数を戻して False。"""
    reply = Reply(client, channel, thread_ts, header, slash_q)
    try:
        await reply.show("資料を検索しています…")
    except SlackApiError as e:
        await asyncio.to_thread(accounts.refund, user["id"], day)
        print(f"Slack に投稿できません ({channel}): {e.response.get('error')}")
        return False
    await run_answer(reply, user, day, question, ext_id, history, notify)
    return True


# ---- Slack の操作 ----

NOT_HERE = "この場所では使えません。"
NOT_POSTED = ("このチャンネルに投稿できませんでした (この質問は回数に数えません)。"
              "Bot をチャンネルに追加してください (`/invite @Bot名`)。")


def build_app(token=None, client=None, slash_ask=None):
    """Slack アプリ (Bolt) を作り、コマンドとイベントを登録する。接続は AsyncSocketModeHandler が行う。
    client: Slack API の呼び出し先を差し替えるとき (テスト) に渡す。
    slash_ask: /ask を受け付けるか (既定は slack.slash_ask。標準は DM とメンションだけで受ける)。"""
    app = AsyncApp(token=token, client=client)
    if slash_ask is None:
        slash_ask = bool(SLACK.get("slash_ask", False))

    async def ask(ack, command, client, respond):
        team, slack_user, channel = command["team_id"], command["user_id"], command["channel_id"]
        question = (command.get("text") or "").strip()
        if not permitted(team, channel):
            await ack(NOT_HERE)
            return
        if not question:
            await ack("使い方: `/ask 質問` (例: `/ask 介護報酬の改定で何が変わったか`)")
            return
        if len(question) > QUESTION_LIMIT:
            await ack(f"質問は {QUESTION_LIMIT} 文字以内にしてください。")
            return
        ext_id = external_id(team, slack_user)
        if ext_id in _BUSY:
            await ack(BUSY_MESSAGE)
            return
        await ack()   # 3秒以内に応答する。以降の受付と回答は時間がかかってよい
        _BUSY.add(ext_id)
        try:
            try:
                user, day = await asyncio.to_thread(common.accept, ext_id)
            except common.Refused as e:
                await respond(str(e), response_type="ephemeral")
                return
            if channel.startswith("D"):
                # DM で使われたとき、相手との DM には投稿できないので Bot との DM に返す
                try:
                    channel = (await client.conversations_open(users=slack_user))["channel"]["id"]
                except SlackApiError:
                    await asyncio.to_thread(accounts.refund, user["id"], day)
                    await respond(NOT_POSTED, response_type="ephemeral")
                    return
            one_line = " ".join(question.split())
            header = f"<@{slack_user}> の質問:\n&gt; {escape(one_line[:300])}\n\n"

            async def notify(text):
                await respond(text, response_type="ephemeral")

            if not await start(client, channel, None, user, day, question, ext_id, header=header,
                               slash_q=question, notify=notify):
                await respond(NOT_POSTED, response_type="ephemeral")
        finally:
            _BUSY.discard(ext_id)

    if slash_ask:
        app.command("/ask")(ask)

    @app.command("/status")
    async def status(ack, command, respond):
        if not permitted(command["team_id"], command["channel_id"]):
            await ack(NOT_HERE)
            return
        await ack()
        st = await asyncio.to_thread(service_status)
        left, limit = await asyncio.to_thread(common.remaining,
                                              external_id(command["team_id"], command["user_id"]))
        await respond(f"モデル: {st['model']} ({st['device'].upper()})\n"
                      f"資料: {st['documents']} 件 ({st['sources']} 機関)\n"
                      f"本日の残り: {left} / {limit} 回", response_type="ephemeral")

    @app.event("app_mention")
    async def on_mention(event, body, client, context):
        if event["channel"].startswith("D") or not _is_plain_post(event):
            return   # DM は message イベントで扱う
        await on_question(client, body.get("team_id") or event.get("team"), event, context.bot_user_id)

    @app.event("message")
    async def on_message(event, body, client, context):
        # Bot との DM だけ。チャンネルの発言はメンションされたとき (app_mention) だけ答える
        if event.get("channel_type") != "im" or not _is_plain_post(event):
            return
        await on_question(client, body.get("team_id") or event.get("team"), event, context.bot_user_id)

    return app


def _is_plain_post(event):
    """人が新しく書いた投稿か。編集・削除・参加通知などの subtype や、Bot の発言には反応しない。"""
    return not (event.get("subtype") or event.get("bot_id") or event.get("edited")
                or event.get("bot_profile")) and bool(event.get("user"))


_SEEN = collections.OrderedDict()   # 処理済みの投稿 (channel, ts)。Socket Mode の再送で2回答えないため
SEEN_MAX = 2000
_BUSY = set()                       # 回答を生成中の利用者。連投・二重送信は回数を使う前に断る
BUSY_MESSAGE = "前の質問の回答を生成中です。終わってから次の質問をしてください。"


def first_time(channel, ts):
    key = (channel, ts)
    if key in _SEEN:
        return False
    _SEEN[key] = True
    while len(_SEEN) > SEEN_MAX:
        _SEEN.popitem(last=False)
    return True


async def on_question(client, team, event, bot_user_id):
    channel, slack_user = event["channel"], event.get("user")
    if not slack_user or not first_time(channel, event["ts"]):
        return
    thread_ts = event.get("thread_ts")
    dm = channel.startswith("D")
    reply_thread = thread_ts or (None if dm else event["ts"])

    async def notify(text):
        await client.chat_postEphemeral(channel=channel, user=slack_user, text=text, thread_ts=reply_thread)

    if not permitted(team, channel):
        await _quietly(notify(NOT_HERE))
        return
    question = question_of(html.unescape(event.get("text", "")))
    if not question:
        await _quietly(notify("質問を書いてください (例: `@Bot名 介護報酬の改定で何が変わったか`)"))
        return
    if len(question) > QUESTION_LIMIT:
        await _quietly(notify(f"質問は {QUESTION_LIMIT} 文字以内にしてください。"))
        return
    ext_id = external_id(team, slack_user)
    if ext_id in _BUSY:
        await _quietly(notify(BUSY_MESSAGE))
        return
    _BUSY.add(ext_id)
    try:
        try:
            user, day = await asyncio.to_thread(common.accept, ext_id)
        except common.Refused as e:
            await _quietly(notify(str(e)))
            return
        history = None
        if thread_ts:
            try:
                history = await thread_history(client, channel, thread_ts, event["ts"], bot_user_id)
            except SlackApiError as e:
                print(f"スレッドを読めません (権限 *:history を確認): {e.response.get('error')}")
        if not await start(client, channel, reply_thread, user, day, question, ext_id,
                           history=history, notify=notify):
            await _quietly(notify(NOT_POSTED))
    finally:
        _BUSY.discard(ext_id)


# ---- 起動 ----

def tokens():
    bot_token, app_token = secret("SLACK_BOT_TOKEN"), secret("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        raise SystemExit("Slack のトークンがありません。環境変数か .env ファイルに "
                         "SLACK_BOT_TOKEN=xoxb-... と SLACK_APP_TOKEN=xapp-... を書いてください")
    if not bot_token.startswith("xoxb-") or not app_token.startswith("xapp-"):
        raise SystemExit("SLACK_BOT_TOKEN は xoxb-、SLACK_APP_TOKEN は xapp- で始まるトークンです")
    return bot_token, app_token


async def _main(bot_token, app_token):
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
    app = build_app(bot_token)
    # 速度制限 (429) にかかったら Retry-After だけ待ってやり直す
    app.client.retry_handlers.append(AsyncRateLimitErrorRetryHandler(max_retry_count=3))
    me = await app.client.auth_test()
    print(f"Slack に接続します: {me['team']} / {me['user']} (Ctrl+C で終了)")
    await AsyncSocketModeHandler(app, app_token).start_async()


def run():
    bot_token, app_token = tokens()
    try:
        asyncio.run(_main(bot_token, app_token))
    except KeyboardInterrupt:
        pass
