"""試験公開用の静的サイト (site/) を作る。

web/index.html (サーバー版の画面) をそのまま写し、サーバーの API の代わりに demo-api.js が
site/data/ の JSON を返す。LLM の回答・ログイン・会話の履歴は使えない (画面には案内を出す)。

    python tools/export_site.py          # data/crawler.db から site/ を作り直す

載せないもの: 国会議員個人の発言 (personal)、地方選挙の候補者についての党の記事、
会議録の題名にある発言者の氏名 (肩書だけ残す)。根拠のチャンクは該当の1つだけ (前後は原文で読む)。
"""
import json
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import PATHS  # noqa: E402
from rag.answer import ORG_LABELS  # noqa: E402
from timeline import view  # noqa: E402

SITE = ROOT / "site"
DATA = SITE / "data"
WEB_INDEX = ROOT / "web" / "index.html"

LOCAL_ELECTION = re.compile(r"(市|町|村|区)議選|議会議員選挙")
NOTICE_GPU = "現在は共用サーバーのGPUを使えない時間帯のため、回答を生成できません。利用できるのは 21時〜翌9時と日曜です。"
NOTICE_DEMO = ("試験公開版です。AI による回答・質問とログインは使えません。"
               "上の「タイムライン」で、収集した資料から取り出した政府・各党の出来事と、その根拠の原文を見られます。")
PILL_GPU = '${gpu ? "回答できます" : "時間外"}'
PILL_DEMO = '${gpu ? "回答できます" : s.device === "demo" ? "AI の回答なし" : "時間外"}'


def clean_title(s):
    """会議録の題名 "院 委員会 第N号 氏名（肩書）" から氏名を外す。"""
    if not s:
        return s
    m = re.match(r"^(.*?第\d+号)\s+\S+?（(.*)）$", s)
    if m:
        return f"{m.group(1)}（{m.group(2)}の発言）"
    m = re.match(r"^(.*?第\d+号)\s+\S+$", s)
    return m.group(1) if m else s


def doc_fields(conn):
    rows = conn.execute("""SELECT dt.document_id, t.name FROM document_tags dt JOIN tags t ON t.id = dt.tag_id
                           WHERE t.kind = '分野'""").fetchall()
    out = {}
    for doc, name in rows:
        out.setdefault(doc, []).append(name)
    return out


def write(name, obj):
    path = DATA / name
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"  {path.relative_to(ROOT)}  {path.stat().st_size / 1024:,.0f} KB")


def main():
    conn = sqlite3.connect(f"file:{PATHS['db']}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    DATA.mkdir(parents=True, exist_ok=True)

    # ---- タイムラインの出来事 (同じ出来事をまとめたもの) ----
    t = view.timeline(conn)
    ev_doc = dict(conn.execute("SELECT id, document_id FROM events").fetchall())
    tags = doc_fields(conn)
    events = []
    for e in t["events"]:
        if e["actor"] != "government" and any(LOCAL_ELECTION.search(s["title"] or "") for s in e["sources"]):
            continue
        for s in e["sources"]:
            s["title"] = clean_title(s["title"])
            s["speaker"] = None
        # 分野の絞り込み用: 出来事の分野、なければ資料の分野タグ (サーバー版と同じ規則)
        e["_fields"] = [e["field"]] if e["field"] else sorted({f for s in e["sources"] for f in tags.get(ev_doc[s["event_id"]], [])})
        events.append(e)
    present = {e["actor"] for e in events}
    t["actors"] = [a for a in t["actors"] if a["id"] in present]
    t["events"] = events

    # ---- 絞り込みの選択肢 (載せた出来事で数え直す) ----
    o = view.options(conn)
    per_field = {f["name"]: sum(1 for e in events if f["name"] in e["_fields"]) for f in o["fields"]}
    o["fields"] = [{"name": k, "count": v} for k, v in per_field.items() if v]
    for a in o["actors"]:
        a["count"] = sum(1 for e in events if e["actor"] == a["id"])

    # ---- 根拠の原文 (該当のチャンクだけ) ----
    ids = [s["event_id"] for e in events for s in e["sources"]]
    sources = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in conn.execute(f"""SELECT e.id, e.quote, c.seq, c.page, c.text FROM events e
                                  LEFT JOIN chunks c ON c.id = e.chunk_id
                                  WHERE e.id IN ({','.join('?' * len(part))})""", part):
            chunks = [{"seq": r["seq"], "page": r["page"], "text": r["text"], "hit": True}] if r["text"] else []
            sources[r["id"]] = {"quote": r["quote"], "radius": 0, "chunks": chunks,
                                "has_before": False, "has_after": False}

    # ---- チャット画面の表示に要るもの ----
    documents, n_sources = conn.execute("""SELECT COUNT(*), COUNT(DISTINCT source_id) FROM documents
                                           WHERE status = 'extracted'""").fetchone()
    org_types = [r[0] for r in conn.execute("""
        SELECT s.org_type FROM documents d JOIN sources s ON s.id = d.source_id
        WHERE d.status = 'extracted' GROUP BY s.org_type ORDER BY COUNT(*) DESC""")]
    fields = [r[0] for r in conn.execute("""
        SELECT t.name FROM tags t JOIN document_tags dt ON dt.tag_id = t.id
        WHERE t.kind = '分野' GROUP BY t.id ORDER BY t.id""")]
    conn.close()

    print(f"出来事 {len(events)} 件 / 根拠 {len(sources)} 件")
    write("timeline.json", t)
    write("timeline-options.json", o)
    write("sources.json", sources)
    write("options.json", {"org_types": [{"value": v, "label": ORG_LABELS.get(v, v)} for v in org_types],
                           "fields": fields})
    write("status.json", {"device": "demo", "model": "試験公開版", "documents": documents, "sources": n_sources,
                          "built": date.today().isoformat()})

    # ---- 画面: web/index.html をそのまま写し、API の代わりの demo-api.js を先に読み込む ----
    html = WEB_INDEX.read_text(encoding="utf-8")
    assert NOTICE_GPU in html and PILL_GPU in html and html.count("<script>") == 1, \
        "web/index.html の作りが変わった。この関数を見直す"
    html = html.replace(NOTICE_GPU, NOTICE_DEMO).replace(PILL_GPU, PILL_DEMO)
    html = html.replace("<script>", '<script src="demo-api.js"></script>\n<script>', 1)
    (SITE / "index.html").write_text(html, encoding="utf-8", newline="\n")
    print(f"  site/index.html  ({WEB_INDEX.relative_to(ROOT)} を写したもの)")


if __name__ == "__main__":
    main()
