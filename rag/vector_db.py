"""埋め込みベクトルの作成・保存と類似検索。

ベクトルは SQLite に float32 のまま保存し、検索時に numpy で総当たりする。
数十万チャンクまではこれで足りる。それ以上は専用のベクトルDBに移す。
"""
import numpy as np

from config import LLM
from database.db import get_connection
from llm.client import embed

BATCH = 16
MAX_CHARS = 1500  # 埋め込みモデルの入力上限に収める


def _doc_text(title, text):
    # embeddinggemma が推奨する入力形式
    return f"title: {title or 'none'} | text: {text}"[:MAX_CHARS]


def _query_text(query):
    return f"task: search result | query: {query}"


def embed_pending():
    """埋め込みのないチャンク (モデルが変わったものを含む) を処理し、件数を返す。"""
    model = LLM["embed"]["model"]
    conn = get_connection()
    conn.execute("DELETE FROM embeddings WHERE model != ?", (model,))
    rows = conn.execute("""
        SELECT c.id, c.text, d.title FROM chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.chunk_id = c.id)
          AND c.exclude_reason IS NULL AND d.exclude_reason IS NULL  -- 検索対象外は埋め込まない
        ORDER BY c.id""").fetchall()
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        vectors = embed([_doc_text(r["title"], r["text"]) for r in batch])
        conn.executemany(
            "INSERT INTO embeddings (chunk_id, model, vector) VALUES (?, ?, ?)",
            [(r["id"], model, np.asarray(v, dtype=np.float32).tobytes())
             for r, v in zip(batch, vectors)])
        conn.commit()
        print(f"  {min(i + BATCH, len(rows))} / {len(rows)}", end="\r")
    conn.close()
    return len(rows)


def vector_search(conn, query, limit=30, allowed=None):
    """質問に意味が近いチャンクを [(chunk_id, 類似度)] で返す。
    allowed (チャンクIDの集合) を渡すと、その中だけを対象にする。"""
    rows = conn.execute("SELECT chunk_id, vector FROM embeddings").fetchall()
    if allowed is not None:
        rows = [r for r in rows if r["chunk_id"] in allowed]
    if not rows:
        return []
    matrix = np.frombuffer(b"".join(r["vector"] for r in rows), dtype=np.float32)
    matrix = matrix.reshape(len(rows), -1)
    q = np.asarray(embed([_query_text(query)])[0], dtype=np.float32)
    scores = matrix @ q / (np.linalg.norm(matrix, axis=1) * np.linalg.norm(q) + 1e-9)
    order = np.argsort(-scores)[:limit]
    return [(rows[i]["chunk_id"], float(scores[i])) for i in order]
