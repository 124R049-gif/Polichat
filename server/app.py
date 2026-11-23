"""API サーバー。チャット画面 (web/index.html) と、その裏の回答生成を提供する。

画面以外の API はログインが必要。質問は利用者ごとに1日の回数を制限する (core/accounts.py)。
回答の生成そのものは core/answer_service.py (Bot と共通) が行い、ここは HTTP への受け渡しだけをする。
"""
import json
from datetime import date
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from config import AUTH, RES_DIR
from core import accounts as auth
from core import conversations
from core.answer_service import answer, service_status
from database.db import get_connection
from rag.answer import ORG_LABELS, speech_treatment
from server.throttle import LoginThrottle
from timeline import view as timeline_view
from timeline.ask import prepare as timeline_ask_prepare

COOKIE = "session"
HISTORY_MESSAGES = 6  # LLMに渡す過去のやりとり (質問と回答で3往復分)
INDEX_HTML = RES_DIR / "web" / "index.html"

app = FastAPI(title="日本社会政治監視機構", docs_url=None, redoc_url=None, openapi_url=None)
throttle = LoginThrottle(AUTH["login_lock_minutes"] * 60)


@app.middleware("http")
async def require_json(request: Request, call_next):
    """書き込み系の API は JSON だけを受け付ける。他サイトのフォームからの送信 (CSRF) を防ぐ。"""
    if request.method == "POST" and request.url.path.startswith("/api/"):
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return JSONResponse({"detail": "JSON で送信してください"}, status_code=415)
    return await call_next(request)


def current_user(request: Request):
    user = auth.session_user(request.cookies.get(COOKIE))
    if user is None:
        raise HTTPException(401, "ログインしてください")
    return user


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20000)


class Filters(BaseModel):
    org_types: list[str] = Field(default=[], max_length=20)
    fields: list[str] = Field(default=[], max_length=30)
    date_from: date | None = None
    date_to: date | None = None


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[Message] = []
    filters: Filters = Filters()
    conversation_id: int | None = None   # 続きの質問なら会話の ID (履歴に保存する先)
    label: str = Field(default="", max_length=500)   # 履歴に残す「絞り込み・対象」の表示


class TimelineScope(BaseModel):
    """タイムラインで表示中の条件。"""
    fields: list[str] = Field(default=[], max_length=30)
    actors: list[str] = Field(default=[], max_length=30)
    personal: bool = False
    date_from: date | None = None
    date_to: date | None = None


class TimelineAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[Message] = []
    scope: TimelineScope = TimelineScope()
    focus: int | None = None   # 選択中の出来事
    conversation_id: int | None = None
    label: str = Field(default="", max_length=500)


@app.get("/")
def index():
    return FileResponse(INDEX_HTML)


@app.post("/api/login")
def login(req: LoginRequest, request: Request, response: Response):
    limits = {f"user:{req.username}": AUTH["login_max_failures"],
              f"ip:{request.client.host if request.client else '-'}": AUTH["login_max_failures_per_ip"]}
    keys = tuple(limits)
    if throttle.blocked(limits):
        raise HTTPException(429, f"ログインの失敗が続いたため、{AUTH['login_lock_minutes']}分ほど待ってから試してください")
    user = auth.authenticate(req.username, req.password)
    if user is None:
        throttle.fail(*keys)
        raise HTTPException(401, "利用者名またはパスワードが違います")
    throttle.reset(keys[0])  # 接続元の記録は消さない (自分のアカウントで解除できないように)
    response.set_cookie(COOKIE, auth.create_session(user["id"]), max_age=AUTH["session_days"] * 86400,
                        httponly=True, samesite="lax", secure=AUTH["cookie_secure"])
    return {"username": user["username"]}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    auth.delete_session(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE)
    return {}


@app.get("/api/me")
def me(user=Depends(current_user)):
    limit, used = auth.daily_limit(user), auth.used_today(user["id"])
    return {"username": user["username"], "limit": limit, "used": used,
            "remaining": max(limit - used, 0)}


@app.get("/api/status")
def status(user=Depends(current_user)):
    return service_status()


@app.get("/api/options")
def options(user=Depends(current_user)):
    """絞り込みの選択肢。資料が1件以上あるものだけを返す。"""
    conn = get_connection()
    org_types = [r[0] for r in conn.execute("""
        SELECT s.org_type FROM documents d JOIN sources s ON s.id = d.source_id
        WHERE d.status = 'extracted' GROUP BY s.org_type ORDER BY COUNT(*) DESC""")]
    fields = [r[0] for r in conn.execute("""
        SELECT t.name FROM tags t JOIN document_tags dt ON dt.tag_id = t.id
        WHERE t.kind = '分野' GROUP BY t.id ORDER BY t.id""")]
    conn.close()
    return {"org_types": [{"value": o, "label": ORG_LABELS.get(o, o)} for o in org_types],
            "fields": fields}


@app.get("/api/timeline/options")
def timeline_options(user=Depends(current_user)):
    conn = get_connection()
    try:
        return timeline_view.options(conn)
    finally:
        conn.close()


@app.get("/api/timeline")
def timeline(user=Depends(current_user), fields: str = "", actors: str = "", personal: bool = False,
             date_from: date | None = None, date_to: date | None = None):
    """政府・各党の出来事と予定。fields / actors はカンマ区切り。期間は出来事の時期で絞る。"""
    split = lambda s: [x for x in s.split(",") if x][:30]
    conn = get_connection()
    try:
        return timeline_view.timeline(conn, split(fields), split(actors), personal,
                                      date_from and date_from.isoformat(), date_to and date_to.isoformat())
    finally:
        conn.close()


def _source_json(n, r):
    return {"n": n, "title": r["title"], "url": r["url"], "page": r["page"],
            "date": r["published_date"], "source_name": r["source_name"],
            "org_type": r["org_type"], "org_label": ORG_LABELS.get(r["org_type"], r["org_type"]),
            "treatment": speech_treatment(r), "text": r["text"]}


def _events(req, user, day, kind, plan_fn=None):
    """1行1イベントのJSONを順に返す: (queued) → sources → delta (複数) → done / error。
    最後まで生成できたら質問と回答を会話履歴に保存し、conversation (会話の ID) を返す。
    回数の精算は answer() が行う (サーバー側の失敗は戻し、利用者の中止は戻さない)。"""
    history = [m.model_dump() for m in req.history[-HISTORY_MESSAGES:]]
    filters = req.filters.model_dump(mode="json") if hasattr(req, "filters") else None
    text, sources = [], []
    for ev in answer(user, req.question, history, filters, day, plan_fn):
        if ev["type"] == "sources":
            sources = [_source_json(n, r) for n, r in enumerate(ev["sources"], 1)]
            ev = {"type": "sources", "sources": sources}
        elif ev["type"] == "delta":
            text.append(ev["text"])
        elif ev["type"] == "done" and text:
            cid = req.conversation_id or conversations.create(user["id"], kind, req.question)
            conversations.add_turn(cid, req.question, {"label": req.label}, "".join(text), {"sources": sources})
            yield json.dumps({"type": "conversation", "id": cid}, ensure_ascii=False) + "\n"
        yield json.dumps(ev, ensure_ascii=False) + "\n"


def _check_conversation(req, user):
    if req.conversation_id is not None and conversations.owned(user["id"], req.conversation_id) is None:
        raise HTTPException(404, "会話が見つかりません")


@app.post("/api/chat")
def chat(req: ChatRequest, user=Depends(current_user)):
    day = auth.today()
    _check_conversation(req, user)
    if not auth.consume(user):
        raise HTTPException(429, auth.limit_message(user))
    return StreamingResponse(_events(req, user, day, "chat"), media_type="application/x-ndjson")


@app.post("/api/timeline/ask")
def timeline_ask(req: TimelineAskRequest, user=Depends(current_user)):
    """タイムラインで表示中の出来事について質問する。回数はチャットと共通。"""
    day = auth.today()
    _check_conversation(req, user)
    if not auth.consume(user):
        raise HTTPException(429, auth.limit_message(user))
    history = [m.model_dump() for m in req.history[-HISTORY_MESSAGES:]]

    def plan():
        conn = get_connection()
        try:
            return timeline_ask_prepare(conn, req.question, history, req.scope.model_dump(mode="json"), req.focus)
        finally:
            conn.close()
    return StreamingResponse(_events(req, user, day, "timeline", plan), media_type="application/x-ndjson")


# ---- 会話履歴 (チャットとタイムラインで共通) ----
@app.get("/api/conversations")
def conversation_list(user=Depends(current_user)):
    return conversations.list_for(user["id"])


@app.get("/api/conversations/{conversation_id}")
def conversation_get(conversation_id: int, user=Depends(current_user)):
    conv = conversations.load(user["id"], conversation_id)
    if conv is None:
        raise HTTPException(404, "会話が見つかりません")
    return conv


@app.delete("/api/conversations/{conversation_id}")
def conversation_delete(conversation_id: int, user=Depends(current_user)):
    if not conversations.delete(user["id"], conversation_id):
        raise HTTPException(404, "会話が見つかりません")
    return {}


@app.get("/api/timeline/source/{event_id}")
def timeline_source(event_id: int, radius: int = 1, user=Depends(current_user)):
    """出来事の根拠の原文を、該当のチャンクとその前後 radius 個まで返す (項目を押したときのプレビュー)。"""
    radius = max(0, min(radius, 20))
    conn = get_connection()
    try:
        ev = conn.execute("""SELECT e.document_id, e.quote, c.seq FROM events e
                             LEFT JOIN chunks c ON c.id = e.chunk_id WHERE e.id = ?""", (event_id,)).fetchone()
        if ev is None:
            raise HTTPException(404, "出来事が見つかりません")
        seq = ev["seq"] or 0
        rows = conn.execute("""SELECT seq, page, text FROM chunks WHERE document_id = ? AND seq BETWEEN ? AND ?
                               ORDER BY seq""", (ev["document_id"], seq - radius, seq + radius)).fetchall()
        last = conn.execute("SELECT MAX(seq) FROM chunks WHERE document_id = ?", (ev["document_id"],)).fetchone()[0]
        return {"quote": ev["quote"], "radius": radius,
                "chunks": [{"seq": r["seq"], "page": r["page"], "text": r["text"], "hit": r["seq"] == seq} for r in rows],
                "has_before": seq - radius > 0, "has_after": last is not None and seq + radius < last}
    finally:
        conn.close()
