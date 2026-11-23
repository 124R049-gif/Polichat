"""Discord Bot。資料に基づく出典付きの回答と、貼られた URL (記事・X の投稿・YouTube) の解説を返す。

- /ask 質問              資料に基づいて回答する
- /explain URL [質問]    URL の中身を解説し、収集した一次資料と照らし合わせる
- @Bot URL [質問]        メンションした投稿に返信して解説する。URL がなければ /ask と同じく質問として答える。
                         URL を貼った投稿への返信でメンションしたときは、返信先の URL を解説する
                         (返信先の本文を読むには Developer Portal で Message Content Intent を有効にする)
- 右クリック → アプリ → 「URL を解説」  他の人が貼った URL も解説できる
- /status                モデル・資料数・本日の残り回数 (本人にだけ表示)

Bot は Discord へ外向きに接続するだけなので、このPCにポート開放や VPN は要らない。
利用者の受付・回答の生成・回数の精算は Bot 共通 (bot/common.py) と core/ が行い、
ここは Discord での表示と操作だけを書く。利用回数は "discord:<ID>" という利用者として数え、解説も1回と数える。
URL を読めなかったときは回数に数えない。
"""
import asyncio
import re
import time
import traceback
from typing import Optional

import discord
from discord import app_commands

from bot import common
from config import DISCORD, secret
from core import accounts, url_reader
from core.answer_service import service_status

MESSAGE_LIMIT = 1900   # Discord の上限 2000 文字から余裕を持たせる
EMBED_DESC_LIMIT = 3800    # 埋め込み1個の説明 (上限 4096)
EMBED_TOTAL_LIMIT = 5800   # 1メッセージの埋め込み全体 (上限 6000)
QUESTION_LIMIT = 2000
CURSOR = " ▌"
# 招待URLの権限: チャンネルを見る・送信・スレッドで送信・埋め込みリンク・履歴を読む (返信に必要)
PERMISSIONS = 1024 | 2048 | 274877906944 | 16384 | 65536

NOT_HERE = "この場所では使えません。"
BUSY_MESSAGE = "前の質問の回答を生成中です。終わってから次の質問をしてください。"
USAGE = ("使い方: `@Bot名 URL` で記事・X の投稿・YouTube を解説します (`@Bot名 URL 質問` で質問も付けられます)。"
         "URL なしの `@Bot名 質問` は資料に基づいて回答します。")
SEARCHING = "資料を検索しています…"
_BUSY = set()   # 回答を生成中の利用者。連投・二重送信は回数を使う前に断る


def _source_embeds(sources):
    lines = []
    for s in common.source_items(sources):
        title = s["title"][:80].replace("[", "(").replace("]", ")")
        lines.append(f"**[{s['n']}]** [{title}]({s['url']}){s['page']}\n"
                     f"{s['source_name']} ({s['org_label']}) {s['date']}")
    # 埋め込みは1個の説明が 4096 文字まで、1メッセージの埋め込み全体で 6000 文字まで
    embeds, body, total = [], "", len("参照した資料")
    for i, line in enumerate(lines):
        rest = f"ほか {len(lines) - i} 件"
        if total + len(line) + 1 + len(rest) > EMBED_TOTAL_LIMIT:
            body += rest
            break
        if len(body) + len(line) + 1 > EMBED_DESC_LIMIT:
            embeds.append(body)
            body = ""
        body += line + "\n"
        total += len(line) + 1
    embeds.append(body)
    return [discord.Embed(title="参照した資料" if i == 0 else None, description=b, color=0x4a6fa5)
            for i, b in enumerate(embeds[:10]) if b]


def _quote(question):
    return f"> {' '.join(question.split())[:300]}\n\n"


def _page_header(page):
    """解説の先頭: 何を読んだか。<URL> で Discord のプレビューを出さない。"""
    title = discord.utils.escape_markdown(page["title"] or page["url"])[:200]
    head = f"🔗 **{title}**\n<{page['url']}>\n"
    if page.get("note"):
        head += f"-# {page['note']}\n"
    return head + "\n"


# ---- 表示 ----

class Reply:
    """1つの回答の表示。最初のメッセージを書き換え、長くなれば続きのメッセージを送る。

    start(content) は最初のメッセージを出して Message を返す。extra(content, **kw) は続きを送って Message を返す。"""

    def __init__(self, start, extra, header=""):
        self.start, self.extra, self.header = start, extra, header
        self.messages, self.shown = [], []   # 送ったメッセージと、その表示中の内容
        self.last_edit = 0.0

    async def show(self, body, final=False):
        text = self.header + body + ("" if final else CURSOR)
        for i, page in enumerate(common.pages(text, MESSAGE_LIMIT)):
            if i < len(self.messages):
                if self.shown[i] != page:
                    await self.messages[i].edit(content=page)
            elif i == 0:
                self.messages.append(await self.start(page))
                self.shown.append(None)
            else:
                self.messages.append(await self.extra(page))
                self.shown.append(None)
            self.shown[i] = page
        self.last_edit = time.monotonic()


def interaction_reply(interaction):
    """defer 済みのスラッシュコマンド・右クリックメニューへの返答。"""
    async def start(content):
        return await interaction.edit_original_response(content=content)

    async def extra(content, **kw):
        return await interaction.followup.send(content, wait=True, **kw)

    return Reply(start, extra)


def message_reply(message):
    """メンションされた投稿への返信。続きは同じチャンネルに送る。"""
    async def start(content):
        return await message.reply(content, mention_author=False)

    async def extra(content, **kw):
        return await message.channel.send(content, **kw)

    return Reply(start, extra)


async def run_answer(reply, user, day, question, ext_id, status=SEARCHING, plan_fn=None, notify=None):
    """回答を生成しながら reply を書き換え、最後に出典と残り回数を送る。

    notify(text): 本人にだけ見せる通知 (出典がないときの残り回数)。なければ送らない。"""
    text, sources, error = "", [], None
    await reply.show(status)
    async for ev in common.events(user, day, question, plan_fn=plan_fn):
        if ev["type"] == "queued":
            status = "順番待ちです (他の質問の回答を生成中)…"
            await _quietly(reply.show(status))
        elif ev["type"] == "sources":
            sources = ev["sources"]
        elif ev["type"] == "delta":
            text += ev["text"]
            if time.monotonic() - reply.last_edit >= common.EDIT_INTERVAL:
                await _quietly(reply.show(text))   # 途中の失敗は最後の表示でやり直す
        elif ev["type"] == "error":
            error = ev["message"]

    if error:
        text += f"\n\n⚠️ {error}"
    await reply.show(text, final=True)
    left, _ = await asyncio.to_thread(common.remaining, ext_id)
    if sources and not error:
        await reply.extra(f"本日の残り {left} 回", embeds=_source_embeds(sources))
    elif not error and notify:
        await notify(f"本日の残り {left} 回")


async def explain(reply, user, day, url, question, ext_id, notify=None):
    """URL を読んでから解説を生成する。読めなければ回数を戻して理由を表示する。"""
    await reply.show("URL を読み込んでいます…")
    try:
        page = await asyncio.to_thread(url_reader.read, url)
    except Exception as e:
        await asyncio.to_thread(accounts.refund, user["id"], day)
        if not isinstance(e, url_reader.ReadError):
            traceback.print_exc()
            e = f"URL の読み込み中にエラーが起きました ({type(e).__name__})。"
        await reply.show(f"⚠️ {e} (この依頼は回数に数えません)", final=True)
        return
    reply.header = _page_header(page)
    await run_answer(reply, user, day, question or "", ext_id, "関連する一次資料を検索しています…",
                     common.explain_plan(page, question), notify)


async def _quietly(coro):
    try:
        await coro
    except discord.HTTPException as e:
        print(f"Discord の表示の更新に失敗しました: {e}")


# ---- Bot ----

class Bot(discord.Client):
    def __init__(self):
        # 既定の intents でメンションされた投稿・DM の本文は読める (Message Content Intent は不要)
        intents = discord.Intents.default()
        intents.message_content = bool(DISCORD.get("message_content_intent", False))
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)
        self.allowed_guilds = {int(g) for g in DISCORD.get("allowed_guild_ids") or []}

    async def setup_hook(self):
        await self.tree.sync()

    async def on_ready(self):
        print(f"Discord に接続しました: {self.user} (サーバー {len(self.guilds)} 件)")
        print(f"招待URL: https://discord.com/oauth2/authorize?client_id={self.user.id}"
              f"&scope=bot+applications.commands&permissions={PERMISSIONS}")

    def allowed_place(self, guild_id):
        """使ってよい場所か。DM は discord.allow_dm、サーバーは allowed_guild_ids で決める。"""
        if guild_id is None:
            return DISCORD.get("allow_dm", False)
        return not self.allowed_guilds or guild_id in self.allowed_guilds

    def permitted(self, interaction):
        return self.allowed_place(interaction.guild_id)


bot = Bot()


def _ext_id(user):
    return f"discord:{user.id}"


async def _accept_interaction(interaction):
    """スラッシュコマンド・メニューの受付。受け付けたら defer して (利用者, 日) を、断ったら None を返す。

    受け付けたときは _BUSY に入れるので、呼び出し側は終わったら外す。"""
    if not bot.permitted(interaction):
        await interaction.response.send_message(NOT_HERE, ephemeral=True)
        return None
    ext_id = _ext_id(interaction.user)
    if ext_id in _BUSY:
        await interaction.response.send_message(BUSY_MESSAGE, ephemeral=True)
        return None
    _BUSY.add(ext_id)
    try:
        user, day = await asyncio.to_thread(common.accept, ext_id)
    except common.Refused as e:
        _BUSY.discard(ext_id)
        await interaction.response.send_message(str(e), ephemeral=True)
        return None
    await interaction.response.defer(thinking=True)
    return user, day


def _notifier(interaction):
    async def notify(text):
        await interaction.followup.send(text, ephemeral=True)
    return notify


@bot.tree.command(name="ask", description="収集した一次資料に基づいて、出典付きで回答します")
@app_commands.describe(question="質問 (例: 介護報酬の改定で何が変わったか)")
async def ask(interaction: discord.Interaction, question: app_commands.Range[str, 1, QUESTION_LIMIT]):
    accepted = await _accept_interaction(interaction)
    if not accepted:
        return
    try:
        reply = interaction_reply(interaction)
        reply.header = _quote(question)
        await run_answer(reply, *accepted, question, _ext_id(interaction.user), notify=_notifier(interaction))
    finally:
        _BUSY.discard(_ext_id(interaction.user))


@bot.tree.command(name="explain", description="記事・X の投稿・YouTube の URL を解説し、一次資料と照らし合わせます")
@app_commands.describe(url="記事などの URL", question="あわせて聞きたいこと (省略可)")
async def explain_command(interaction: discord.Interaction, url: str,
                          question: Optional[app_commands.Range[str, 1, QUESTION_LIMIT]] = None):
    urls = url_reader.find_urls(url)
    if not urls:
        await interaction.response.send_message("http:// または https:// で始まる URL を入れてください。",
                                                ephemeral=True)
        return
    accepted = await _accept_interaction(interaction)
    if not accepted:
        return
    try:
        await explain(interaction_reply(interaction), *accepted, urls[0], question,
                      _ext_id(interaction.user), _notifier(interaction))
    finally:
        _BUSY.discard(_ext_id(interaction.user))


def _urls_of(message):
    return url_reader.find_urls(message.content) + [e.url for e in message.embeds if e.url]


@bot.tree.context_menu(name="URL を解説")
async def explain_menu(interaction: discord.Interaction, message: discord.Message):
    # メニューで選んだ投稿の本文は、Message Content Intent がなくても読める
    urls = _urls_of(message)
    if not urls:
        await interaction.response.send_message("この投稿には URL がありません。", ephemeral=True)
        return
    accepted = await _accept_interaction(interaction)
    if not accepted:
        return
    try:
        await explain(interaction_reply(interaction), *accepted, urls[0], None,
                      _ext_id(interaction.user), _notifier(interaction))
    finally:
        _BUSY.discard(_ext_id(interaction.user))


@bot.tree.command(name="status", description="使用中のモデル、資料数、本日の残り回数を表示します")
async def status(interaction: discord.Interaction):
    if not bot.permitted(interaction):
        await interaction.response.send_message(NOT_HERE, ephemeral=True)
        return
    st = await asyncio.to_thread(service_status)
    left, limit = await asyncio.to_thread(common.remaining, _ext_id(interaction.user))
    await interaction.response.send_message(
        f"モデル: {st['model']} ({st['device'].upper()})\n資料: {st['documents']} 件 ({st['sources']} 機関)\n"
        f"本日の残り: {left} / {limit} 回", ephemeral=True)


# ---- メンション ----

def _strip_mentions(text):
    return re.sub(r"<@!?\d+>", "", text or "").strip()


async def _referenced_urls(message):
    """返信先の投稿の URL。本文が読めない (Message Content Intent なし) ときは埋め込みの URL だけ。"""
    if not message.reference or not message.reference.message_id:
        return []
    target = message.reference.resolved
    if not isinstance(target, discord.Message):
        try:
            target = await message.channel.fetch_message(message.reference.message_id)
        except discord.HTTPException:
            return []
    return _urls_of(target)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or bot.user is None:
        return
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        return
    dm = message.guild is None
    # 本文で明示的にメンションされたときだけ答える (Bot の投稿への返信で自動で付く通知には反応しない)
    if not dm and bot.user.id not in message.raw_mentions:
        return
    if not bot.allowed_place(message.guild.id if message.guild else None):
        return
    text = _strip_mentions(message.content)
    urls = url_reader.find_urls(text)
    question = text
    for url in urls:
        question = question.replace(f"<{url}>", "").replace(url, "")
    question = " ".join(question.split()) or None
    if not urls and message.reference:
        urls = await _referenced_urls(message)
    if not urls and not question:
        await _quietly(message.reply(USAGE, mention_author=False))
        return
    if question and len(question) > QUESTION_LIMIT:
        await _quietly(message.reply(f"質問は {QUESTION_LIMIT} 文字以内にしてください。", mention_author=False))
        return

    ext_id = _ext_id(message.author)
    if ext_id in _BUSY:
        await _quietly(message.reply(BUSY_MESSAGE, mention_author=False))
        return
    _BUSY.add(ext_id)
    try:
        try:
            user, day = await asyncio.to_thread(common.accept, ext_id)
        except common.Refused as e:
            await _quietly(message.reply(str(e), mention_author=False))
            return
        reply = message_reply(message)
        try:
            await reply.show("URL を読み込んでいます…" if urls else SEARCHING)
        except discord.HTTPException as e:
            # 送信・返信の権限がない場所。回数は戻す
            await asyncio.to_thread(accounts.refund, user["id"], day)
            print(f"Discord に返信できません ({message.channel}): {e}")
            return
        if urls:
            await explain(reply, user, day, urls[0], question, ext_id)
        else:
            reply.header = _quote(question)
            await run_answer(reply, user, day, question, ext_id)
    finally:
        _BUSY.discard(ext_id)


def run():
    token = secret("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("Discord の Bot トークンがありません。環境変数 DISCORD_BOT_TOKEN か、"
                         ".env ファイルに DISCORD_BOT_TOKEN=... を書いてください")
    bot.run(token, log_handler=None)
