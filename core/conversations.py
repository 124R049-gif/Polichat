"""会話履歴。Web のチャットとタイムラインの質問で同じ表を使う (kind で区別)。

1往復 (質問と回答) は、回答が最後まで生成できたときだけ保存する。失敗・中止した質問は残さない。
会話は利用者ごとで、他の利用者の会話は読めない。
"""
import json

from database.db import get_connection

KINDS = ("chat", "timeline")
TITLE_CHARS = 40
LIST_LIMIT = 100


def _conn():
    return get_connection()


def owned(user_id, conversation_id):
    """その利用者の会話なら行を返す。"""
    conn = _conn()
    try:
        return conn.execute("SELECT * FROM conversations WHERE id = ? AND user_id = ?",
                            (conversation_id, user_id)).fetchone()
    finally:
        conn.close()


def create(user_id, kind, question):
    assert kind in KINDS
    title = " ".join(question.split())[:TITLE_CHARS]
    conn = _conn()
    try:
        cid = conn.execute("INSERT INTO conversations (user_id, kind, title) VALUES (?, ?, ?) RETURNING id",
                           (user_id, kind, title)).fetchone()[0]
        conn.commit()
        return cid
    finally:
        conn.close()


def add_turn(conversation_id, question, question_meta, answer, answer_meta):
    """質問と回答を1組保存する。"""
    conn = _conn()
    try:
        conn.executemany(
            "INSERT INTO conversation_messages (conversation_id, role, content, meta) VALUES (?, ?, ?, ?)",
            [(conversation_id, "user", question, json.dumps(question_meta, ensure_ascii=False)),
             (conversation_id, "assistant", answer, json.dumps(answer_meta, ensure_ascii=False))])
        conn.execute("UPDATE conversations SET updated_at = datetime('now', 'localtime') WHERE id = ?",
                     (conversation_id,))
        conn.commit()
    finally:
        conn.close()


def list_for(user_id):
    """新しい順の会話の一覧 (往復が1つ以上あるもの)。"""
    conn = _conn()
    try:
        rows = conn.execute("""
            SELECT c.id, c.kind, c.title, c.updated_at, COUNT(m.id) / 2 AS turns
            FROM conversations c JOIN conversation_messages m ON m.conversation_id = c.id
            WHERE c.user_id = ? GROUP BY c.id ORDER BY c.updated_at DESC, c.id DESC LIMIT ?""",
                            (user_id, LIST_LIMIT)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def load(user_id, conversation_id):
    """会話とそのやりとり。その利用者の会話でなければ None。"""
    conv = owned(user_id, conversation_id)
    if conv is None:
        return None
    conn = _conn()
    try:
        messages = [{"role": r["role"], "content": r["content"], "meta": json.loads(r["meta"] or "{}")}
                    for r in conn.execute("""SELECT role, content, meta FROM conversation_messages
                                             WHERE conversation_id = ? ORDER BY id""", (conversation_id,))]
    finally:
        conn.close()
    return {"id": conv["id"], "kind": conv["kind"], "title": conv["title"], "messages": messages}


def delete(user_id, conversation_id):
    conn = _conn()
    try:
        n = conn.execute("DELETE FROM conversations WHERE id = ? AND user_id = ?",
                         (conversation_id, user_id)).rowcount
        conn.commit()
        return n > 0
    finally:
        conn.close()
