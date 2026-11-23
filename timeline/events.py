"""タイムライン用に、資料から「誰が・いつ・何をした / している / するつもりか」を取り出す。

  python main.py events            未処理の資料から取り出す (新しい資料から順に timeline.max_per_run 件)
  python main.py events -n 20      件数を指定

主体 (行) は資料の発行元で決め、LLM には選ばせない (他党や政府の行動を取り違えないため)。
- 政党の Web サイトの資料 → その党。行政機関の発表 → 政府
- 国会会議録: 大臣・政務官・官僚の答弁 → 政府、議員の発言 → 会派の党。
  党規約で意見表明を制限していない党の議員の発言は personal (議員個人の発言) として別に印を付ける
取り出した出来事は、LLM が本文から抜き出した一節 (quote) が実際に本文にあることを確かめてから保存する。

重複を防ぐため、掲載ページと添付 (PDF など) は1組として LLM に渡し、同じ組の中で題名の似た出来事は1件に絞る。
別々の資料 (政党のページと国会の発言など) に載った同じ出来事は、画面に出すときにまとめる (timeline/view.py)。
"""
import json
import re
from datetime import date

from config import BASE_DIR, PATHS, TIMELINE
from database.db import get_connection
from llm.client import chat, endpoint
from rag.answer import speech_treatment

with open(BASE_DIR / "source" / "actors.json", encoding="utf-8") as f:
    _ACTORS = json.load(f)
ACTORS = _ACTORS["actors"]
ACTOR_LABELS = {a["id"]: a["label"] for a in ACTORS}
_PARTY_SOURCES = {s: a["id"] for a in ACTORS for s in a.get("sources", [])}
_GOV_SOURCES = set(_ACTORS["government_sources"])

STATUSES = {"done": "実施済み", "ongoing": "進行中", "planned": "予定・目標"}
KINDS = {
    "法案・制度": "法案の提出・成立、法律や制度の創設・改正",
    "予算・税制": "予算案・補正予算・税制改正・財源",
    "閣議決定・計画": "閣議決定、基本方針、計画・戦略の策定",
    "政策の実施": "給付・補助・規制などの施策の開始・実施",
    "提言・要望": "政府や団体への提言・申し入れ・要望書の提出",
    "声明・談話": "談話・声明・見解の発表、抗議",
    "会議・協議": "党の会議・政党間協議・連立協議・審議会の開催",
    "選挙・公約": "選挙の公約・政策集、候補者の擁立",
    "その他": "上のどれにも当たらない具体的な行動",
}

# 国会の発言者: 役職で政府側か判断する (国家公安委員会委員長のように「大臣」を含む役職は政府)
_NOT_GOV = re.compile(r"議長|事務総長|事務総局|会計検査院|裁判所|参考人|公述人|委員長|日本銀行|人事院|法制局")
_GOV = re.compile(r"大臣|政務官|内閣|官房|省|庁")
# 発言の下読み: 行動・予定を述べていそうな発言だけを LLM に渡す (会議録は量が多いため)
_ACTION_WORDS = re.compile(r"法案|法律案|改正|提出|閣議決定|施行|予定|目指|方針|来年|年度|成立|創設|導入|"
                           r"実施|取りまとめ|策定|公約|提言|申し入れ|開始|決定")

PROMPT = """あなたは日本の政治・行政の資料から、出来事と予定を年表に整理する係です。
次の資料から、「{actor}」自身が行った・行っている・これから行うとしている具体的な行動を、多くても{max_events}件取り出してください。

取り出すもの:
- 法案の提出・成立、閣議決定、予算・税制、施策の開始、提言・申し入れ、談話・声明の発表、会議・協議、公約など
- 「〇〇を来年の通常国会に提出する」「2027年度から実施する」のような、時期や中身が具体的な予定・目標

取り出さないもの:
- 「{actor}」以外 (他党・政府・団体・外国など) の行動。資料が他者の行動を紹介・批判している部分は、{actor}の行動ではない
- 意見・評価・要求だけで、行動を伴わないもの (「〜すべきだ」「〜は問題だ」)
- 国会で質問した・答弁したこと自体 (その中で述べた具体的な行動・予定は取り出す)
- 資料に書かれていないことの推測
当てはまるものがなければ events は空にする。

重複させない:
- 同じ出来事は1件だけにする。言い換えて複数書かない (掲載ページと添付資料に同じことが書かれていても1件)
- 一連の行動 (会議を開いて提言をまとめ、政府に申し入れた など) は、いちばん重要な行動の1件にまとめる

各項目の書き方:
- status: done (資料の時点で実施済み) / ongoing (進行中・審議中) / planned (これから行う予定・目標・公約)
- title: 何をしたか・するかを30字以内で (例: 「議員定数削減法案を衆議院に提出」)。主体名は書かない
- summary: 中身を100字以内で。資料にあることだけを書く
- start / end: 時期。資料の公開日 {published} を基準に「昨日」「来年4月」「来年度」などを日付に直す。
  形式は YYYY-MM-DD、日が分からなければ YYYY-MM、月も分からなければ YYYY。
  年度は4月始まり (「2027年度」は start 2027-04、end 2028-03)。
  done で時期が書かれていなければ公開日。planned で時期が書かれていなければ null。
  end は期間のあるもの (審議期間、計画期間、実施期間) だけ。なければ null
- field: 分野を1つ
- kind: 種類を1つ
- quote: 根拠になる部分を資料の本文からそのまま抜き出す (20〜120字。言い換えない)

【種類】
{kinds}

【分野】
{fields}

主体: {actor}
発行元: {source}
公開日: {published}
題名: {title}
本文:
{text}"""


def _schema(fields):
    item = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": list(STATUSES)},
            "kind": {"type": "string", "enum": list(KINDS)},
            "field": {"type": "string", "enum": fields},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "start": {"type": ["string", "null"]},
            "end": {"type": ["string", "null"]},
            "quote": {"type": "string"},
        },
        "required": ["status", "kind", "field", "title", "summary", "start", "end", "quote"],
    }
    return {"type": "object", "properties": {
        "events": {"type": "array", "maxItems": TIMELINE["max_events_per_document"], "items": item}},
        "required": ["events"]}


def actor_of(doc):
    """文書の主体 (actor id, 議員個人の発言か) を返す。タイムラインの対象外なら None。"""
    if doc["source_id"] in _PARTY_SOURCES:
        return _PARTY_SOURCES[doc["source_id"]], False
    if doc["source_id"] in _GOV_SOURCES:
        return "government", False
    if doc["org_type"] != "diet" or doc["file_type"] != "speech" or not doc["affiliation"]:
        return None
    group = doc["affiliation"]
    if "大臣" in group or (_GOV.search(group) and not _NOT_GOV.search(group)):
        return "government", False
    for a in ACTORS:
        if any(m in group for m in a.get("match", [])):
            personal = (speech_treatment(doc) or "").startswith("議員個人の発言")
            return a["id"], personal
    return None


_DATE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


def normalize_date(value, published):
    """LLM の返した時期を YYYY[-MM[-DD]] にそろえる。不正・あり得ない値は None。"""
    if not value:
        return None
    value = str(value).strip().replace("/", "-")
    m = _DATE.match(value)
    if not m:
        return None
    y, mo, d = (int(x) if x else None for x in m.groups())
    base = int((published or str(date.today()))[:4])
    if not base - 30 <= y <= base + 15 or (mo and not 1 <= mo <= 12):
        return None
    if d:
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return f"{y:04d}-{mo:02d}"
    return f"{y:04d}-{mo:02d}" if mo else f"{y:04d}"


_SPACE = re.compile(r"[\s　「」『』、。・,.()（）]")


def _norm(s):
    return _SPACE.sub("", s or "")


def grounded(quote, text, size=10, ratio=0.6):
    """quote が本文から抜き出されたものか。空白・句読点の違いは無視し、
    10文字ずつの断片の6割以上が本文にあれば根拠ありとする (多少の写し間違いは許す)。"""
    q, t = _norm(quote), _norm(text)
    if len(q) < 8:
        return False
    if q in t:
        return True
    pieces = [q[i:i + size] for i in range(0, max(len(q) - size, 0) + 1, size // 2)]
    return sum(p in t for p in pieces) >= ratio * len(pieces)


def _chunk_for(quote, chunks):
    q = _norm(quote)
    for c in chunks:
        if q[:15] and q[:15] in _norm(c["text"]):
            return c["id"]
    return chunks[0]["id"] if chunks else None


def candidates(conn):
    """まだ取り出していない対象の文書を新しい順に返す。"""
    sources = sorted(_PARTY_SOURCES) + sorted(_GOV_SOURCES)
    rows = conn.execute(f"""
        SELECT d.id, d.parent_id, d.title, d.published_date, d.source_id, d.file_type, d.affiliation, d.n_chars,
               s.name AS source_name, s.org_type
        FROM documents d JOIN sources s ON s.id = d.source_id
        WHERE d.status = 'extracted' AND d.exclude_reason IS NULL
          AND COALESCE(d.file_type, '') != 'meeting'
          AND (d.source_id IN ({','.join('?' * len(sources))}) OR d.file_type = 'speech')
          AND NOT EXISTS (SELECT 1 FROM event_scans e WHERE e.document_id = d.id)
        ORDER BY d.published_date DESC, d.id DESC""", sources).fetchall()
    # 主体ごとに新しい順に並べ、主体を順番に回して1件ずつ取る。単純に新しい順だと、件数の多い政府
    # (大臣・官僚の答弁は会議録の4割以上) が1回の処理数を使い切り、党の資料が後回しになるため
    buckets = {}
    for r in rows:
        if (a := actor_of(r)):
            buckets.setdefault(a[0], []).append(r)
    queues = [iter(b) for b in buckets.values()]
    fair = []
    while queues:
        for q in list(queues):
            if (r := next(q, None)) is None:
                queues.remove(q)
            else:
                fair.append(r)
    return fair


def _document_text(conn, doc_id):
    chunks = conn.execute("""SELECT id, text FROM chunks WHERE document_id = ? AND exclude_reason IS NULL
                             ORDER BY seq""", (doc_id,)).fetchall()
    return chunks, "\n".join(c["text"] for c in chunks)


def family(conn, doc):
    """掲載ページと添付をまとめた組 [(文書, チャンク, 本文)] を返す。会議録の発言は1件ずつ。
    別々に LLM に渡すと、同じ出来事が資料の数だけ重複するため。"""
    if doc["file_type"] == "speech":
        return [(doc, *_document_text(conn, doc["id"]))]
    root = doc["parent_id"] or doc["id"]
    docs = conn.execute("""
        SELECT id, title, published_date FROM documents
        WHERE (id = ? OR parent_id = ?) AND status = 'extracted' AND exclude_reason IS NULL
        ORDER BY parent_id IS NOT NULL, id""", (root, root)).fetchall()
    return [(d, *_document_text(conn, d["id"])) for d in docs] or [(doc, *_document_text(conn, doc["id"]))]


def _family_text(members, limit):
    """組の本文を LLM に渡す長さに収める。添付にも枠が残るよう、1件あたりの上限を設ける。"""
    if len(members) == 1:
        return members[0][2][:limit]
    share = max(limit // len(members), 800)
    return "\n\n".join(f"【{d['title'] or '無題'}】\n{text[:share]}" for d, _, text in members if text)[:limit]


def grams(s):
    """題名の比較用の文字の2-gram。"""
    s = _norm(s)
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def similar(a, b):
    """2-gram の重なり (Dice 係数)。短い題名が長い題名に含まれるだけでは高くならないようにする。"""
    return 2 * len(a & b) / max(1, len(a) + len(b))


DUPLICATE = 0.6   # 同じ組の資料から取り出した出来事どうしで、題名の重なりがこれ以上なら言い換えとみなす


def reassign(conn):
    """取り出し済みの出来事の主体と「議員個人の発言」の印を、今の actors.json・party_rules.json で付け直す。
    党規約の確認が進んだときや党名が変わったときに、LLM で取り出し直さずに反映するため。変えた件数を返す。"""
    rows = conn.execute("""
        SELECT e.id, e.actor, e.personal, d.id AS doc_id, d.source_id, d.file_type, d.affiliation, s.org_type
        FROM events e JOIN documents d ON d.id = e.document_id JOIN sources s ON s.id = d.source_id""").fetchall()
    changed = []
    for r in rows:
        a = actor_of(r)
        if a and (a[0], int(a[1])) != (r["actor"], r["personal"]):
            changed.append((a[0], int(a[1]), r["id"]))
    conn.executemany("UPDATE events SET actor = ?, personal = ? WHERE id = ?", changed)
    conn.commit()
    return len(changed)


def reset(conn):
    """取り出した出来事と記録を消す (下読みで LLM に渡さなかった発言の記録は残す)。"""
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM event_scans WHERE model != 'skip'")
    conn.commit()


def extract_pending(limit=None, redo=False):
    """未処理の文書から出来事を取り出して保存する。(LLM に渡した組の数, 保存した出来事数) を返す。
    redo なら、これまでに取り出したものを消して最初からやり直す。"""
    with open(PATHS["taxonomy"], encoding="utf-8") as f:
        fields = list(json.load(f)["分野"])
    schema = _schema(fields)
    kinds = "\n".join(f"- {k}: {v}" for k, v in KINDS.items())
    conn = get_connection()
    if redo:
        reset(conn)
    if (n := reassign(conn)):
        print(f"主体・議員個人の発言の印を付け直した出来事: {n} 件 (党規約・党名の変更を反映)")
    docs = candidates(conn)
    limit = limit or TIMELINE["max_per_run"]
    _, model, device = endpoint("tag")
    print(f"対象 {len(docs)} 件のうち最大 {limit} 組 / モデル {model} ({device})")

    scanned = saved = 0
    handled = set()
    for doc in docs:
        if scanned >= limit:
            break
        if doc["id"] in handled:
            continue  # 掲載ページ・添付の組として処理済み
        actor, personal = actor_of(doc)
        members = family(conn, doc)
        ids = [d["id"] for d, _, _ in members]
        handled.update(ids)
        text = "\n".join(t for _, _, t in members)
        marks = ",".join("?" * len(ids))
        # 会議録の発言は量が多いので、行動・予定を述べていそうなものだけを LLM に渡す
        if doc["file_type"] == "speech" and not _ACTION_WORDS.search(text):
            conn.execute("INSERT OR REPLACE INTO event_scans (document_id, n_events, model) VALUES (?, 0, 'skip')",
                         (doc["id"],))
            conn.commit()
            continue
        scanned += 1
        root = members[0][0]
        published = root["published_date"] or doc["published_date"]
        name = next(a["name"] for a in ACTORS if a["id"] == actor)
        prompt = PROMPT.format(actor=name, max_events=TIMELINE["max_events_per_document"], kinds=kinds,
                               fields="、".join(fields), source=doc["source_name"],
                               published=published or "不明", title=root["title"] or "無題",
                               text=_family_text(members, TIMELINE["input_chars"]))
        try:
            result = json.loads(chat("tag", [{"role": "user", "content": prompt}], schema))["events"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"  [失敗] {root['id']} {root['title']}: {type(e).__name__}")
            continue
        rows, seen, dropped, merged = [], [], 0, 0
        for ev in result:
            if not isinstance(ev, dict) or ev.get("status") not in STATUSES or ev.get("kind") not in KINDS:
                continue
            if not (ev.get("title") or "").strip():
                continue
            # 根拠の一節がある資料を探す。どれにもなければ捨てる (本文にない一節を根拠にしたもの)
            home = next(((d, chunks) for d, chunks, t in members if grounded(ev.get("quote"), t)), None)
            if home is None:
                dropped += 1
                continue
            g = grams(ev["title"])
            if any(similar(g, other) >= DUPLICATE for other in seen):
                merged += 1
                continue  # 同じ出来事の言い換え
            seen.append(g)
            start = normalize_date(ev.get("start"), published)
            end = normalize_date(ev.get("end"), published)
            if ev["status"] == "done" and not start:
                start = published
            if end and start and end < start:
                end = None
            rows.append((home[0]["id"], _chunk_for(ev["quote"], home[1]), actor, int(personal), ev["status"],
                         ev["kind"], ev.get("field") if ev.get("field") in fields else None,
                         ev["title"].strip()[:60], (ev.get("summary") or "").strip()[:200],
                         start, end, ev["quote"].strip()[:300], model))
        conn.execute(f"DELETE FROM events WHERE document_id IN ({marks})", ids)
        conn.executemany("""INSERT INTO events (document_id, chunk_id, actor, personal, status, kind, field,
                                                title, summary, start_date, end_date, quote, model)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
        conn.executemany("INSERT OR REPLACE INTO event_scans (document_id, n_events, model) VALUES (?, ?, ?)",
                         [(i, sum(r[0] == i for r in rows), model) for i in ids])
        conn.commit()
        saved += len(rows)
        label = ACTOR_LABELS[actor] + ("(個人)" if personal else "")
        notes = [f"添付など {len(ids) - 1} 件と合わせて" if len(ids) > 1 else "",
                 f"根拠のない {dropped} 件を除外" if dropped else "",
                 f"重複 {merged} 件をまとめた" if merged else ""]
        notes = "、".join(n for n in notes if n)
        print(f"  {root['id']} [{label}] {(root['title'] or '')[:30]} → {len(rows)} 件" + (f" ({notes})" if notes else ""))
    conn.close()
    return scanned, saved
