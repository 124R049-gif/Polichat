"""Discord Bot。/ask で資料に基づく出典付き回答を返す。

Bot は Discord へ外向きに接続するだけなので、このPCにポート開放や VPN は要らない。
利用回数は Discord の利用者ごとに "discord:<ID>" という利用者として数える (server/auth.py)。
回答の生成は共用 GPU に負荷をかけないよう、同時に discord.max_concurrent 件までにする。
"""
import asyncio
import threading
import time

import discord
from discord import app_commands

from config import DISCORD, secret
from database.db import get_connection
from llm.client import LLMError, chat_stream, endpoint
from rag.answer import NOT_FOUND, ORG_LABELS, prepare
from server import auth

MESSAGE_LIMIT = 1900   # Discord の上限 2000 文字から余裕を持たせる
EDIT_INTERVAL = 1.5    # 生成中に表示を更新する間隔 (秒)。短すぎると Discord の速度制限にかかる
_END = object()


def _pages(text):
    """長い回答を MESSAGE_LIMIT 以下に分ける。なるべく改行で切る。"""
    pages = []
    while len(text) > MESSAGE_LIMIT:
        cut = text.rfind("\n", 0, MESSAGE_LIMIT)
        cut = cut if cut > MESSAGE_LIMIT // 2 else MESSAGE_LIMIT
        pages.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return pages + [text]


def _source_embeds(sources):
    lines = []
    for n, r in enumerate(sources, 1):
        page = f" p.{r['page']}" if r["page"] else ""
        title = (r["title"] or "無題")[:80].replace("[", "(").replace("]", ")")
        lines.append(f"**[{n}]** [{title}]({r['url']}){page}\n"
                     f"{r['source_name']} ({ORG_LABELS.get(r['org_type'], r['org_type'])})"
                     f" {r['published_date'] or '日付不明'}")
    embeds, body = [], ""
    for line in lines:
        if len(body) + len(line) > 3800:
            embeds.append(body)
            body = ""
        body += line + "\n"
    embeds.append(body)
    return [discord.Embed(title="参照した資料" if i == 0 else None, description=b, color=0x4a6fa5)
            for i, b in enumerate(embeds[:5]) if b]


def _generate(question, push):
    """別スレッドで動く。資料の検索と生成を行い、push(種類, 値) で結果を渡す。"""
    try:
        plan, sources = prepare(question)
        push("sources", sources)
        if plan is None:
            push("delta", NOT_FOUND)
        else:
            for prefix, messages in plan:
                if prefix:
                    push("delta", prefix)
                for text in chat_stream("chat", messages) if messages else ():
                    push("delta", text)
    except LLMError as e:
        push("error", str(e))
    except Exception as e:  # 想定外の失敗も利用者に伝え、回数を戻す
        push("error", f"内部エラー: {type(e).__name__}")
    finally:
        push(_END, None)


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default(),
                         allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)
        self.slots = asyncio.Semaphore(DISCORD.get("max_concurrent", 1))
        self.waiting = 0
        self.allowed_guilds = {int(g) for g in DISCORD.get("allowed_guild_ids") or []}

    async def setup_hook(self):
        await self.tree.sync()

    async def on_ready(self):
        print(f"Discord に接続しました: {self.user} (サーバー {len(self.guilds)} 件)")
        print(f"招待URL: https://discord.com/oauth2/authorize?client_id={self.user.id}"
              "&scope=bot+applications.commands&permissions=2048")

    def permitted(self, interaction):
        """使ってよい場所か。DM は discord.allow_dm、サーバーは allowed_guild_ids で決める。"""
        if interaction.guild_id is None:
            return DISCORD.get("allow_dm", False)
        return not self.allowed_guilds or interaction.guild_id in self.allowed_guilds


bot = Bot()


@bot.tree.command(name="ask", description="収集した一次資料に基づいて、出典付きで回答します")
@app_commands.describe(question="質問 (例: 介護報酬の改定で何が変わったか)")
async def ask(interaction: discord.Interaction, question: app_commands.Range[str, 1, 2000]):
    if not bot.permitted(interaction):
        await interaction.response.send_message("この場所では使えません。", ephemeral=True)
        return
    user = await asyncio.to_thread(auth.external_user, f"discord:{interaction.user.id}")
    if user["disabled"]:
        await interaction.response.send_message("利用が停止されています。", ephemeral=True)
        return
    day = auth.today()
    if not await asyncio.to_thread(auth.consume, user):
        await interaction.response.send_message(
            f"本日の質問回数の上限 ({auth.daily_limit(user)} 回) に達しました。明日になると再び使えます。",
            ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    header = f"> {question[:300]}\n\n"

    waited = bot.slots.locked()
    if waited:
        bot.waiting += 1
        await interaction.edit_original_response(content=f"{header}順番待ちです ({bot.waiting} 件待ち)…")
    async with bot.slots:
        if waited:
            bot.waiting -= 1
        await _answer(interaction, user, day, question, header)


async def _answer(interaction, user, day, question, header):
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    threading.Thread(target=_generate, daemon=True,
                     args=(question, lambda kind, value: loop.call_soon_threadsafe(
                         queue.put_nowait, (kind, value)))).start()

    text, sources, error = "", [], None
    messages, shown = [], []   # 送った Discord のメッセージと、その表示中の内容
    last_edit = 0.0

    async def show(final=False):
        nonlocal last_edit
        body = header + (text or "資料を検索しています…")
        if not final:
            body += " ▌"
        for i, page in enumerate(_pages(body)):
            if i < len(messages):
                if shown[i] != page:
                    await messages[i].edit(content=page)
                    shown[i] = page
            elif i == 0:
                await interaction.edit_original_response(content=page)
                messages.append(await interaction.original_response())
                shown.append(page)
            else:
                messages.append(await interaction.followup.send(page, wait=True))
                shown.append(page)
        last_edit = time.monotonic()

    await show()
    while True:
        kind, value = await queue.get()
        if kind is _END:
            break
        if kind == "sources":
            sources = value
        elif kind == "delta":
            text += value
        elif kind == "error":
            error = value
        if kind == "delta" and time.monotonic() - last_edit >= EDIT_INTERVAL:
            try:
                await show()
            except discord.HTTPException:
                pass  # 途中の更新の失敗は、最後の表示でやり直す

    if error:
        await asyncio.to_thread(auth.refund, user["id"], day)
        text += f"\n\n⚠️ {error} (この質問は回数に数えません)"
    await show(final=True)
    remaining = max(auth.daily_limit(user) - await asyncio.to_thread(auth.used_today, user["id"]), 0)
    if sources:
        await interaction.followup.send(f"本日の残り {remaining} 回", embeds=_source_embeds(sources))
    elif not error:
        await interaction.followup.send(f"本日の残り {remaining} 回", ephemeral=True)


@bot.tree.command(name="status", description="使用中のモデル、資料数、本日の残り回数を表示します")
async def status(interaction: discord.Interaction):
    if not bot.permitted(interaction):
        await interaction.response.send_message("この場所では使えません。", ephemeral=True)
        return

    def collect():
        user = auth.external_user(f"discord:{interaction.user.id}")
        conn = get_connection()
        docs, srcs = conn.execute("""SELECT COUNT(*), COUNT(DISTINCT source_id) FROM documents
                                     WHERE status = 'extracted'""").fetchone()
        conn.close()
        return user, docs, srcs

    user, docs, srcs = await asyncio.to_thread(collect)
    _, model, device = endpoint("chat")
    remaining = max(auth.daily_limit(user) - auth.used_today(user["id"]), 0)
    await interaction.response.send_message(
        f"モデル: {model} ({device.upper()})\n資料: {docs} 件 ({srcs} 機関)\n"
        f"本日の残り: {remaining} / {auth.daily_limit(user)} 回", ephemeral=True)


def run():
    token = secret("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("Discord の Bot トークンがありません。環境変数 DISCORD_BOT_TOKEN か、"
                         ".env ファイルに DISCORD_BOT_TOKEN=... を書いてください")
    bot.run(token, log_handler=None)
