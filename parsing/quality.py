"""検索対象から外す文書・チャンクの判定。

分量の大きい名簿・一覧表・外国語版は、内容の割にチャンク数が多く検索結果を占めてしまう。
原本と抽出結果は残し、exclude_reason に理由を記録して検索だけから外す。

- 文書単位: 題名から名簿・応募書類と分かるもの
- チャンク単位: 外国語の部分、表や一覧で文章がほとんどない部分
  (統計の速報のように、冒頭の日本語の要約だけ残し、数表の部分を外せる)
"""
import re

from database.db import get_connection

_TITLE = re.compile(r"名簿|警告書の発出を行った|募集要項|出願書類|記入見本")
_JA = re.compile(r"[ぁ-んァ-ヴ一-龥々]")
_LATIN = re.compile(r"[A-Za-z]")
_KANA = re.compile(r"[ぁ-ん]")
MIN_CHARS = 200  # これより短いチャンクは判定しない


def assess_document(title):
    """文書全体を外す理由。対象にしてよければ None。"""
    if _TITLE.search(title or ""):
        return "名簿・一覧・応募書類"
    return None


def assess_chunk(text):
    """チャンクを外す理由。対象にしてよければ None。"""
    if len(text) < MIN_CHARS:
        return None
    ja, latin = len(_JA.findall(text)), len(_LATIN.findall(text))
    if ja < (ja + latin) * 0.5:
        return "外国語"
    # 日本語の文章ならひらがなが2〜4割を占める。極端に少なく、文の終わり (。) もほとんど
    # ないのは表や一覧。数値の多い統計の要約文は「。」があるので残る
    if len(_KANA.findall(text)) < len(text) * 0.05 and text.count("。") < len(text) / 200:
        return "文章が少ない (表・一覧)"
    return None


def mark_excluded():
    """抽出済みの全文書・全チャンクを判定し直し、(外した文書数, 外したチャンク数) を返す。"""
    conn = get_connection()
    for r in conn.execute("SELECT id, title FROM documents WHERE status = 'extracted'").fetchall():
        conn.execute("UPDATE documents SET exclude_reason = ? WHERE id = ?",
                     (assess_document(r["title"]), r["id"]))
    for r in conn.execute("SELECT id, text FROM chunks").fetchall():
        conn.execute("UPDATE chunks SET exclude_reason = ? WHERE id = ?",
                     (assess_chunk(r["text"]), r["id"]))
    conn.commit()
    docs = conn.execute("SELECT COUNT(*) FROM documents WHERE exclude_reason IS NOT NULL").fetchone()[0]
    chunks = conn.execute("SELECT COUNT(*) FROM chunks WHERE exclude_reason IS NOT NULL").fetchone()[0]
    conn.close()
    return docs, chunks
