"""原本 (HTML/PDF/DOCX) から本文を取り出し、チャンクに分けてDBへ保存する。"""
import re
import zipfile
from xml.etree import ElementTree

import pymupdf
from bs4 import BeautifulSoup

from config import BASE_DIR, CHUNK
from database.db import get_connection

MIN_CHARS = 50              # これ未満は本文なしとみなす
MIN_CHARS_PER_PDF_PAGE = 30  # 平均がこれ未満のPDFはスキャン画像とみなす
_NOISE_TAGS = ["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe"]
# 先頭ほど優先。note の記事本文 ([data-name=body]) は題名・タグ欄を含まない
_MAIN_SELECTORS = ["[data-name=body]", "main", "article", "[role=main]", "#main", "#contents", "#content", ".main"]
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _tidy(text):
    lines = (re.sub(r"[ \t　]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _join_lines(text):
    """PDFの見た目上の改行をつなぐ。日本語同士は詰め、英数字同士は空白を入れる。"""
    out = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if out and out[-1].isascii() and line[0].isascii():
            out += " "
        out += line
    return out


def extract_html(body):
    """(本文, <title>) を返す。ナビゲーション等を除き、本文領域を優先する。"""
    soup = BeautifulSoup(body, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else None
    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    container = soup.body or soup
    for selector in _MAIN_SELECTORS:
        found = soup.select_one(selector)
        if found and len(found.get_text(strip=True)) >= 200:
            container = found
            break
    return _tidy(container.get_text("\n")), title


def extract_pdf(body):
    """ページごとの本文リストを返す。"""
    pages = []
    with pymupdf.open(stream=body, filetype="pdf") as pdf:
        for page in pdf:
            blocks = page.get_text("blocks")
            texts = [_join_lines(b[4]) for b in blocks if b[6] == 0]
            pages.append("\n".join(t for t in texts if t))
    return pages


def extract_docx(path):
    with zipfile.ZipFile(path) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    paragraphs = ("".join(t.text or "" for t in p.iter(_W + "t")) for p in root.iter(_W + "p"))
    return _tidy("\n".join(paragraphs))


def split_chunks(text):
    """段落を詰めて目安の文字数のチャンクにする。長い段落は文末で切る。"""
    target, overlap = CHUNK["target_chars"], CHUNK["overlap_chars"]
    pieces = []
    for para in text.split("\n"):
        if len(para) <= target:
            pieces.append(para)
            continue
        for sentence in re.findall(r"[^。]*。?", para):
            while len(sentence) > target:
                pieces.append(sentence[:target])
                sentence = sentence[target:]
            if sentence:
                pieces.append(sentence)

    chunks, current = [], ""
    for piece in pieces:
        if current and len(current) + len(piece) + 1 > target:
            chunks.append(current)
            current = current[-overlap:] if overlap else ""
        current = f"{current}\n{piece}" if current else piece
    if current.strip():
        chunks.append(current)
    return chunks


def _extract_document(doc):
    """[(ページ番号 or None, 本文)] と、判明したタイトルを返す。"""
    path = BASE_DIR / doc["raw_path"]
    if doc["file_type"] == "pdf":
        pages = extract_pdf(path.read_bytes())
        return [(i + 1, text) for i, text in enumerate(pages)], None
    if doc["file_type"] == "docx":
        return [(None, extract_docx(path))], None
    text, title = extract_html(path.read_bytes())
    return [(None, text)], title


def extract_pending():
    """未抽出 (status = fetched) の文書をすべて処理する。"""
    conn = get_connection()
    docs = conn.execute("SELECT * FROM documents WHERE status = 'fetched'").fetchall()
    counts = {}
    for doc in docs:
        try:
            pages, title = _extract_document(doc)
            n_chars = sum(len(text) for _, text in pages)
            if doc["file_type"] == "pdf" and n_chars < MIN_CHARS_PER_PDF_PAGE * len(pages):
                status, error = "needs_ocr", "テキスト層がない (スキャン画像)"
            elif n_chars < MIN_CHARS:
                status, error = "empty", "本文を抽出できない"
            else:
                status, error = "extracted", None
        except Exception as e:  # 壊れたファイル1件で全体を止めない
            pages, title, n_chars = [], None, 0
            status, error = "error", f"抽出失敗: {type(e).__name__}: {e}"

        conn.execute("DELETE FROM chunks WHERE document_id = ?", (doc["id"],))
        if status == "extracted":
            seq = 0
            for page, text in pages:
                for chunk in split_chunks(text):
                    conn.execute(
                        "INSERT INTO chunks (document_id, seq, page, text) VALUES (?, ?, ?, ?)",
                        (doc["id"], seq, page, chunk),
                    )
                    seq += 1
        conn.execute(
            """UPDATE documents SET status = ?, error = ?, n_chars = ?,
                   title = COALESCE(title, ?), extracted_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (status, error, n_chars, title, doc["id"]),
        )
        conn.commit()
        counts[status] = counts.get(status, 0) + 1
    conn.close()
    return counts
