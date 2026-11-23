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
from core.answer_service import answer, service_status
from database.db import get_connection
from rag.answer import ORG_LABELS, speech_treatment
from server.throttle import LoginThrottle
from timeline import view as timeline_view

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


def _events(req: ChatRequest, user, day):
    """1行1イベントのJSONを順に返す: (queued) → sources → delta (複数) → done / error。
    回数の精算は answer() が行う (サーバー側の失敗は戻し、利用者の中止は戻さない)。"""
    history = [m.model_dump() for m in req.history[-HISTORY_MESSAGES:]]
    for ev in answer(user, req.question, history, req.filters.model_dump(mode="json"), day):
        if ev["type"] == "sources":
            ev = {"type": "sources", "sources": [_source_json(n, r) for n, r in enumerate(ev["sources"], 1)]}
        yield json.dumps(ev, ensure_ascii=False) + "\n"


@app.post("/api/chat")
def chat(req: ChatRequest, user=Depends(current_user)):
    day = auth.today()
    if not auth.consume(user):
        raise HTTPException(429, auth.limit_message(user))
    return StreamingResponse(_events(req, user, day), media_type="application/x-ndjson")
