"""国会会議録 (国立国会図書館 国会会議録検索システムの検索用API) からの収集。

会議1回を親文書、発言1件を子文書として保存する。発言には発言者と会派が付くので、
政党のWebサイトに載らない各党の主張や、政府答弁を一次資料として扱える。

API: https://kokkai.ndl.go.jp/api.html
 - meeting_list: 期間内の会議の一覧 (1回100件まで)
 - meeting:      会議1回分の全発言 (1回10件まで。ここでは1会議ずつ取る)
会議録は開催から公開まで数日〜数週間かかる。
"""
import hashlib
import json
import re
from datetime import date, timedelta
from urllib.parse import urlencode

from config import BASE_DIR, PATHS
from data_collection.fetcher import FetchError
from parsing.extract import split_chunks

API = "https://kokkai.ndl.go.jp/api"
# 議事進行だけの発言 (「〇〇君」「これにて散会します」) を除くための目安
_CHAIR = re.compile(r"^○(委員長|議長|副議長|会長|小委員長|理事|分科会主査)")
MIN_SPEECH_CHARS = 150
MIN_CHAIR_CHARS = 600


def _get_json(fetcher, endpoint, **params):
    params["recordPacking"] = "json"
    body, _, _ = fetcher.get(f"{API}/{endpoint}?{urlencode(params)}")
    return json.loads(body)


def list_meetings(fetcher, since, until=None):
    """since 以降 (until があればその日まで) の会議 [(issueID, 開催日)] を返す。"""
    meetings, start = [], 1
    period = {"from": since.isoformat()} | ({"until": until.isoformat()} if until else {})
    while start:
        page = _get_json(fetcher, "meeting_list", **period, maximumRecords=100, startRecord=start)
        meetings += [(m["issueID"], m["date"]) for m in page.get("meetingRecord", [])]
        start = page.get("nextRecordPosition")
    return meetings


def _keep(speech):
    text = speech["speech"]
    if speech["speechOrder"] == 0 or len(text) < MIN_SPEECH_CHARS:
        return False
    return not (_CHAIR.match(text) and len(text) < MIN_CHAIR_CHARS)


def _save_meeting(conn, source, meeting, raw):
    """会議1回分を保存し、保存した発言数を返す。"""
    title = f"{meeting['nameOfHouse']} {meeting['nameOfMeeting']} {meeting['issue']}"
    digest = hashlib.sha256(raw).hexdigest()
    path = PATHS["raw_dir"] / source["id"] / f"{meeting['issueID']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    speeches = [s for s in meeting["speechRecord"] if _keep(s)]

    parent = conn.execute(
        """INSERT INTO documents (source_id, url, title, published_date, file_type,
                                  content_hash, raw_path, status, n_chars, extracted_at)
           VALUES (?, ?, ?, ?, 'meeting', ?, ?, 'extracted', ?, CURRENT_TIMESTAMP)
           RETURNING id""",
        (source["id"], meeting["meetingURL"], title, meeting["date"], digest,
         path.relative_to(BASE_DIR).as_posix(), sum(len(s["speech"]) for s in speeches)),
    ).fetchone()["id"]

    for s in speeches:
        # 大臣・副大臣などの答弁は政府の説明なので、会派より役職を優先する
        group = s["speakerPosition"] or s["speakerGroup"] or ""
        label = "・".join(x for x in (s["speakerPosition"], s["speakerGroup"]) if x)
        speaker = f"{s['speaker']}（{label}）" if label else s["speaker"]
        text = s["speech"].replace("\r\n", "\n").replace("　", " ")
        doc_id = conn.execute(
            """INSERT INTO documents (source_id, parent_id, url, title, published_date,
                                      file_type, status, n_chars, author, affiliation,
                                      extracted_at)
               VALUES (?, ?, ?, ?, ?, 'speech', 'extracted', ?, ?, ?, CURRENT_TIMESTAMP)
               RETURNING id""",
            (source["id"], parent, s["speechURL"], f"{title} {speaker}", meeting["date"],
             len(text), s["speaker"], group or None),
        ).fetchone()["id"]
        conn.executemany(
            "INSERT INTO chunks (document_id, seq, page, text) VALUES (?, ?, NULL, ?)",
            [(doc_id, i, chunk) for i, chunk in enumerate(split_chunks(text))])
    conn.commit()
    return len(speeches)


def collect_kokkai(conn, fetcher, source, limit, since=None, until=None):
    """直近 source["days"] 日 (since を渡せばその日以降) の会議録のうち、未取得の会議を新しい順に最大 limit 件取得する。

    過去分を遡るときは since を古い日付にして何回かに分けて実行する (1会議ごとに2秒待つので、1年分で1時間ほど)。
    until を渡すと、その日までの会議だけにする。"""
    since = since or date.today() - timedelta(days=source.get("days", 45))
    meetings = list_meetings(fetcher, since, until)
    known = {r["url"] for r in conn.execute(
        "SELECT url FROM documents WHERE source_id = ? AND file_type = 'meeting'", (source["id"],))}
    new = [m for m in meetings if f"https://kokkai.ndl.go.jp/txt/{m[0]}" not in known]
    print(f"  会議 {len(meetings)} 件 / 未取得 {len(new)} 件 ({since} 以降{f'、{until} まで' if until else ''})")
    for issue_id, _ in sorted(new, key=lambda m: m[1], reverse=True)[:limit]:
        try:
            body, _, _ = fetcher.get(f"{API}/meeting?{urlencode({'issueID': issue_id, 'recordPacking': 'json'})}")
            meeting = json.loads(body)["meetingRecord"][0]
        except (FetchError, KeyError, IndexError, ValueError) as e:
            print(f"  [error] {issue_id}: {e}")
            continue
        n = _save_meeting(conn, source, meeting, body)
        print(f"  [extracted] {meeting['date']} {meeting['nameOfHouse']} "
              f"{meeting['nameOfMeeting']} {meeting['issue']} 発言 {n} 件")


def rebuild(conn, source):
    """保存済みの原本 (JSON) から会議録の文書とチャンクを作り直す。取り込み方を変えたとき用。"""
    conn.execute("DELETE FROM documents WHERE source_id = ? AND file_type = 'speech'", (source["id"],))
    conn.execute("DELETE FROM documents WHERE source_id = ? AND file_type = 'meeting'", (source["id"],))
    for path in sorted((PATHS["raw_dir"] / source["id"]).glob("*.json")):
        raw = path.read_bytes()
        _save_meeting(conn, source, json.loads(raw)["meetingRecord"][0], raw)


def inherit_tags(conn):
    """発言に、会議に付いたタグを引き継ぐ。引き継いだ発言数を返す。"""
    cur = conn.execute("""
        INSERT OR IGNORE INTO document_tags (document_id, tag_id, method)
        SELECT d.id, dt.tag_id, 'parent' FROM documents d
        JOIN document_tags dt ON dt.document_id = d.parent_id
        WHERE d.file_type = 'speech'""")
    conn.commit()
    return cur.rowcount
