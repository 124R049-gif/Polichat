"""タイムライン画面に渡すデータ。同じ主体の同じ出来事 (複数の資料に載ったもの) を1件にまとめる。

予定 (planned) として載った出来事が後の資料で実施済み (done) になっている、のように状態が違っても
同じ出来事ならまとめ、題名・状態・時期は新しい資料のものを使う。"""
from rag.answer import ORG_LABELS, speech_treatment
from timeline.events import ACTORS, KINDS, STATUSES, grams, similar

SIMILAR = 0.5       # 時期が近いとき、題名の文字の2-gram の重なりがこれ以上なら同じ出来事とみなす
SIMILAR_FAR = 0.65  # 片方の時期が未定、または状態が違って時期が離れているときの基準 (厳しめ)
NEAR_DAYS = 45      # 時期の差がこれ以内なら「時期が近い」
_RANK = {"planned": 0, "ongoing": 1, "done": 2}


def _days(d):
    """YYYY[-MM[-DD]] をおおよその通日にする (近さの比較用)。"""
    if not d:
        return None
    parts = [int(x) for x in d.split("-")]
    y, m, day = parts + [6, 15][len(parts) - 1:]  # 月・日が分からなければ年・月の中ほどとする
    return y * 365 + (m - 1) * 30.5 + day


def _same(a, b):
    if a["actor"] != b["actor"] or a["personal"] != b["personal"]:
        return False
    score = similar(a["_grams"], b["_grams"])
    da, db = _days(a["start"]), _days(b["start"])
    if da is not None and db is not None and abs(da - db) <= NEAR_DAYS:
        return score >= SIMILAR
    if da is None or db is None or a["status"] != b["status"]:
        return score >= SIMILAR_FAR  # 時期未定の予定、予定→実施のように時期の動いたもの
    return False  # 同じ状態で時期が離れている (毎年の行事など) は別の出来事


def timeline(conn, fields=None, actors=None, personal=False, date_from=None, date_to=None):
    """出来事を主体ごとにまとめて返す。時期のない予定 (date_from/to に関係なく) も含める。"""
    where, params = ["1 = 1"], []
    if not personal:
        where.append("e.personal = 0")
    if fields:
        where.append(f"e.field IN ({','.join('?' * len(fields))})")
        params += fields
    if actors:
        where.append(f"e.actor IN ({','.join('?' * len(actors))})")
        params += actors
    if date_from:
        where.append("(e.start_date IS NULL OR COALESCE(e.end_date, e.start_date) >= ?)")
        params.append(date_from[:len("YYYY-MM")])
    if date_to:
        where.append("(e.start_date IS NULL OR e.start_date <= ?)")
        params.append(date_to)
    rows = conn.execute(f"""
        SELECT e.*, d.title AS doc_title, d.url, d.published_date, d.affiliation, d.author,
               s.name AS source_name, s.org_type, c.text AS chunk_text
        FROM events e
        JOIN documents d ON d.id = e.document_id
        JOIN sources s ON s.id = d.source_id
        LEFT JOIN chunks c ON c.id = e.chunk_id
        WHERE {' AND '.join(where)}
        ORDER BY e.start_date, e.id""", params).fetchall()

    merged = []
    for r in rows:
        source = {"title": r["doc_title"], "url": r["url"], "date": r["published_date"],
                  "source_name": r["source_name"], "org_label": ORG_LABELS.get(r["org_type"], r["org_type"]),
                  "speaker": r["author"], "treatment": speech_treatment(r), "quote": r["quote"]}
        ev = {"id": r["id"], "actor": r["actor"], "personal": bool(r["personal"]), "status": r["status"],
              "kind": r["kind"], "field": r["field"], "title": r["title"], "summary": r["summary"],
              "start": r["start_date"], "end": r["end_date"], "sources": [source],
              "_grams": grams(r["title"]), "_key": (r["published_date"] or "", _RANK[r["status"]])}
        same = next((m for m in merged if _same(m, ev)), None)
        if same is None:
            merged.append(ev)
            continue
        same["sources"].append(source)
        # 題名・状態・時期は新しい資料のものを使う (同じ日なら実施済み > 進行中 > 予定)
        if ev["_key"] > same["_key"]:
            for k in ("status", "kind", "title", "summary", "start", "end", "_key"):
                same[k] = ev[k]
        elif ev["end"] and ev["start"] == same["start"] and (not same["end"] or ev["end"] > same["end"]):
            same["end"] = ev["end"]
    for m in merged:
        del m["_grams"], m["_key"]
        m["sources"].sort(key=lambda s: s["date"] or "", reverse=True)

    present = {m["actor"] for m in merged}
    return {
        "actors": [{"id": a["id"], "label": a["label"], "name": a["name"]} for a in ACTORS
                   if a["id"] in present or (actors and a["id"] in actors)],
        "events": merged,
        "statuses": STATUSES,
        "kinds": list(KINDS),
    }


def options(conn):
    """絞り込みの選択肢 (出来事のある分野と主体)、件数、資料の期間。"""
    fields = [r[0] for r in conn.execute(
        "SELECT field FROM events WHERE field IS NOT NULL GROUP BY field ORDER BY COUNT(*) DESC")]
    counts = dict(conn.execute("SELECT actor, COUNT(*) FROM events WHERE personal = 0 GROUP BY actor").fetchall())
    span = conn.execute("SELECT MIN(start_date), MAX(COALESCE(end_date, start_date)) FROM events").fetchone()
    scanned = conn.execute("SELECT COUNT(*) FROM event_scans").fetchone()[0]
    return {"fields": fields,
            "actors": [{"id": a["id"], "label": a["label"], "name": a["name"], "count": counts.get(a["id"], 0)}
                       for a in ACTORS],
            "min_date": span[0], "max_date": span[1], "scanned": scanned}
