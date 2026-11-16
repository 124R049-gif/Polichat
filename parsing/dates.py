"""公開日の補完。RSS由来でない文書は日付を持たないので、題名・URL・本文冒頭から推定する。

推定なので誤りうる (本文冒頭に別の日付が先に出る場合など)。期間での絞り込み用。
"""
import re
import unicodedata
from datetime import date

from database.db import get_connection

_URL_YMD = re.compile(r"(?<!\d)(20\d{2})[/_-]?(\d{2})[/_-]?(\d{2})(?!\d{3})")
_URL_YYMMDD = re.compile(r"/20(\d{2})/\D*\1(\d{2})(\d{2})(?!\d)")  # /2026/261112.html
_SEIREKI = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_REIWA = re.compile(r"令和\s*(\d{1,2}|元)\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_DOTTED = re.compile(r"(?<!\d)(20\d{2})[./](\d{1,2})[./](\d{1,2})(?!\d)")
TEXT_HEAD = 1500


def _valid(y, m, d):
    try:
        found = date(int(y), int(m), int(d))
    except ValueError:
        return None
    return found.isoformat() if 2000 <= found.year and found <= date.today() else None


def _from_text(text):
    text = unicodedata.normalize("NFKC", text or "")
    candidates = []
    for m in _SEIREKI.finditer(text):
        candidates.append((m.start(), _valid(*m.groups())))
    for m in _REIWA.finditer(text):
        year = 2019 if m.group(1) == "元" else 2018 + int(m.group(1))
        candidates.append((m.start(), _valid(year, m.group(2), m.group(3))))
    for m in _DOTTED.finditer(text):
        candidates.append((m.start(), _valid(*m.groups())))
    for _, found in sorted(c for c in candidates if c[1]):
        return found  # 最初に現れる日付
    return None


def guess_date(url, title, text):
    """題名 → URL → 本文冒頭の順に日付を探し、'YYYY-MM-DD' か None を返す。"""
    found = _from_text(title)
    if found:
        return found
    m = _URL_YYMMDD.search(url)
    if m and _valid("20" + m.group(1), m.group(2), m.group(3)):
        return _valid("20" + m.group(1), m.group(2), m.group(3))
    for m in _URL_YMD.finditer(url):
        if _valid(*m.groups()):
            return _valid(*m.groups())
    return _from_text((text or "")[:TEXT_HEAD])


def fill_dates():
    """公開日のない抽出済み文書に日付を補う。添付は掲載ページの日付を優先する。補った件数を返す。"""
    conn = get_connection()
    docs = conn.execute("""
        SELECT d.id, d.url, d.title, d.parent_id FROM documents d
        WHERE d.status = 'extracted' AND d.published_date IS NULL
        ORDER BY d.parent_id IS NOT NULL, d.id""").fetchall()  # 掲載ページを先に処理
    filled = 0
    for doc in docs:
        found = None
        if doc["parent_id"]:
            found = conn.execute("SELECT published_date FROM documents WHERE id = ?",
                                 (doc["parent_id"],)).fetchone()[0]
        if not found:
            rows = conn.execute("SELECT text FROM chunks WHERE document_id = ? ORDER BY seq LIMIT 3",
                                (doc["id"],)).fetchall()
            found = guess_date(doc["url"], doc["title"], "\n".join(r["text"] for r in rows))
        if found:
            conn.execute("UPDATE documents SET published_date = ? WHERE id = ?", (found, doc["id"]))
            filled += 1
    conn.commit()
    conn.close()
    return filled
