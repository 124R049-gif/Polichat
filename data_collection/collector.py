"""収集の本体: 入口から文書を見つけ、原本を保存し、documents に記録する。"""
import hashlib
import json
from urllib.parse import urlparse

from config import BASE_DIR, CRAWL, PATHS
from data_collection.discover import discover, find_attachments
from data_collection.fetcher import FetchError, Fetcher
from data_collection.kokkai import collect_kokkai
from database.db import get_connection


def load_sources():
    with open(PATHS["sources"], encoding="utf-8") as f:
        return json.load(f)


def detect_type(body, content_type, url):
    """中身の先頭バイトを優先して種別を判定する。"""
    if body[:5] == b"%PDF-":
        return "pdf"
    if body[:2] == b"PK" and urlparse(url).path.lower().endswith(".docx"):
        return "docx"
    if "html" in content_type or body[:200].lstrip().lower().startswith((b"<!doctype", b"<html")):
        return "html"
    return None


def _is_known(conn, url):
    row = conn.execute("SELECT status FROM documents WHERE url = ?", (url,)).fetchone()
    return row is not None and row["status"] != "error"


def _save(conn, fetcher, source, url, title, date, parent_id=None):
    """1件取得して記録する。戻り値は (document_id, 種別, 本文bytes)。失敗時は種別 None。"""
    record = {"status": "fetched", "error": None, "file_type": None,
              "content_hash": None, "raw_path": None}
    body = None
    try:
        body, content_type, _ = fetcher.get(url)
        record["file_type"] = detect_type(body, content_type, url)
        if record["file_type"] is None:
            record["status"] = "skipped"
            record["error"] = f"対象外の形式: {content_type}"
        else:
            record["content_hash"] = hashlib.sha256(body).hexdigest()
            same = conn.execute(
                "SELECT id FROM documents WHERE content_hash = ? AND url != ?",
                (record["content_hash"], url),
            ).fetchone()
            if same:
                record["status"] = "duplicate"
                record["error"] = f"document {same['id']} と同一内容"
            else:
                path = (PATHS["raw_dir"] / source["id"]
                        / f"{record['content_hash'][:16]}.{record['file_type']}")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                record["raw_path"] = path.relative_to(BASE_DIR).as_posix()
    except FetchError as e:
        record["status"] = "error"
        record["error"] = str(e)

    cur = conn.execute(
        """INSERT INTO documents
               (source_id, parent_id, url, title, published_date,
                file_type, content_hash, raw_path, status, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(url) DO UPDATE SET
               file_type = excluded.file_type, content_hash = excluded.content_hash,
               raw_path = excluded.raw_path, status = excluded.status,
               error = excluded.error, fetched_at = CURRENT_TIMESTAMP
           RETURNING id""",
        (source["id"], parent_id, url, title or None, date, record["file_type"],
         record["content_hash"], record["raw_path"], record["status"], record["error"]),
    )
    doc_id = cur.fetchone()["id"]
    conn.commit()
    print(f"  [{record['status']}] {record['file_type'] or '-'} {title[:40] if title else url}")
    ok = record["status"] == "fetched"
    return doc_id, (record["file_type"] if ok else None), body


def collect(source_ids=None, limit=None):
    """台帳のソースを巡回して新しい文書を取得する。"""
    limit = limit or CRAWL["max_new_per_source"]
    conn = get_connection()
    fetcher = Fetcher()
    for source in load_sources():
        if source_ids and source["id"] not in source_ids:
            continue
        if not source_ids and source.get("enabled") is False:
            continue
        print(f"== {source['name']}")
        if source.get("type") == "kokkai":
            try:
                collect_kokkai(conn, fetcher, source, limit)
            except FetchError as e:
                print(f"  会議一覧の取得に失敗: {e}")
            conn.execute("UPDATE sources SET last_scan = CURRENT_TIMESTAMP WHERE id = ?",
                         (source["id"],))
            conn.commit()
            continue
        try:
            items = discover(fetcher, source)
        except FetchError as e:
            print(f"  入口の取得に失敗: {e}")
            continue
        new_items = [i for i in items if not _is_known(conn, i[0])]
        print(f"  発見 {len(items)} 件 / 未取得 {len(new_items)} 件")

        for url, title, date in new_items[:limit]:
            doc_id, file_type, body = _save(conn, fetcher, source, url, title, date)
            if file_type != "html":
                continue
            attachments = [a for a in find_attachments(body, url) if not _is_known(conn, a[0])]
            for a_url, a_title in attachments[:CRAWL["max_attachments"]]:
                _save(conn, fetcher, source, a_url, a_title or title, date, parent_id=doc_id)

        conn.execute(
            "UPDATE sources SET last_scan = CURRENT_TIMESTAMP WHERE id = ?", (source["id"],)
        )
        conn.commit()
    conn.close()
