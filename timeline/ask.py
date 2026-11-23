"""タイムラインに表示している出来事について質問に答える。

画面で表示中の条件 (分野・主体・期間・議員個人の発言) で出来事を取り直し、質問に近いもの (選択中の出来事を先頭に)
を最大 ASK_TOP_K 件選ぶ。その根拠の原文 (チャンク) を番号付きの資料として LLM に渡し、チャットと同じ規則で
出典付きで答えさせる。年表の項目自体は LLM が資料から取り出した索引なので、根拠にはせず原文を根拠にする。
"""
from rag.answer import RULES, SYSTEM, _TERM, format_source
from timeline.view import timeline

ASK_TOP_K = 12
MIN_MATCHED = 3   # 質問の語に一致する出来事がこれより少なければ、全体から偏りなく足す
# 出来事の中身を表さない語 (質問の言い回し・主体の区別)。出来事選びには使わない
GENERIC = {"期間", "要約", "整理", "説明", "流", "動", "各党", "政党", "政府", "与党", "野党", "今後", "予定",
           "時系列", "全体", "概要", "最近", "直近", "年表", "出来事", "教", "比較", "違", "違い"}
ROLE_LABELS = {"government": "政府", "ruling": "与党", "opposition": "野党"}
STATUS_LABELS = {"done": "実施済み", "ongoing": "進行中", "planned": "予定・目標"}

TIMELINE_RULES = """【年表についての規則】
1. 【年表】は、資料から取り出した出来事の一覧 (索引) である。中身は対応する番号の資料で確かめてから書き、
   資料番号を付ける。資料で確かめられない項目は書かない。
2. 「予定・目標」は、その時点でそう表明されたというだけで、実施されたことを意味しない。実施済みと書かない。
3. 与党・野党・政府の区別、時期の前後関係を取り違えない。時系列で整理するときは古いものから順に書く。
4. 【年表】に含まれない時期・主体・分野について聞かれたら、表示中の範囲には含まれていないと書く。
5. 「※議員個人の発言」の項目は議員個人の見解であり、党の立場として書かない。「〇〇議員 (所属) は〜と述べた」と書く。"""


def _score(ev, terms):
    text = f"{ev['title']} {ev['summary'] or ''} {' '.join(s['quote'] or '' for s in ev['sources'])}"
    return sum(t in text for t in terms)


def pick(events, question, focus=None, k=ASK_TOP_K):
    """質問に近い出来事を選ぶ。選択中の出来事を先頭に、語の一致が多い順、同点なら新しい順。
    一致する語がなければ (「流れを要約して」など)、主体ごとに新しいものから順に偏りなく選ぶ。"""
    terms = [t for t in dict.fromkeys(_TERM.findall(question)) if t not in GENERIC]
    first = [e for e in events if e["id"] == focus]
    rest = [e for e in events if e["id"] != focus]
    by_date = sorted(rest, key=lambda e: (e["start"] is not None, e["start"] or ""), reverse=True)  # 時期未定は後回し
    picked = [e for e in by_date if terms and _score(e, terms)]
    picked.sort(key=lambda e: _score(e, terms), reverse=True)
    picked = picked[:k - len(first)]
    if len(picked) >= MIN_MATCHED:
        return first + picked
    # 一致が少ないときは、主体ごとに新しいものから順に偏りなく足す
    queues, chosen = {}, {e["id"] for e in picked}
    for e in by_date:
        if e["id"] not in chosen:
            queues.setdefault(e["actor"], []).append(e)
    queues = list(queues.values())
    while len(picked) < k - len(first) and any(queues):
        for q in queues:
            if q and len(picked) < k - len(first):
                picked.append(q.pop(0))
    return first + picked


def _source_rows(conn, events):
    """出来事ごとに根拠のチャンクを1つ、rag.answer.retrieve と同じ形の行で返す。"""
    rows = []
    for ev in events:
        r = conn.execute("""
            SELECT c.id, c.document_id, c.page, c.text, d.title, d.url, d.published_date, d.source_id,
                   d.affiliation, s.name AS source_name, s.org_type
            FROM events e
            JOIN documents d ON d.id = e.document_id
            JOIN sources s ON s.id = d.source_id
            JOIN chunks c ON c.id = COALESCE(e.chunk_id,
                 (SELECT id FROM chunks WHERE document_id = e.document_id ORDER BY seq LIMIT 1))
            WHERE e.id = ?""", (ev["id"],)).fetchone()
        rows.append(r)
    return rows


def _when(ev):
    if not ev["start"]:
        return "時期未定"
    return f"{ev['start']}〜{ev['end']}" if ev["end"] else ev["start"]


def prepare(conn, question, history=None, scope=None, focus=None):
    """(生成の段取り, 使った資料のリスト) を返す。形は rag.answer.prepare と同じ。
    scope: {"fields", "actors", "personal", "date_from", "date_to"} (画面で表示中の条件)。"""
    scope = scope or {}
    data = timeline(conn, scope.get("fields"), scope.get("actors"), bool(scope.get("personal")),
                    scope.get("date_from"), scope.get("date_to"))
    events = pick(data["events"], question, focus)
    if not events:
        return None, []
    names = {a["id"]: a["name"] for a in data["actors"]}
    rows = _source_rows(conn, events)
    pairs = [(ev, r) for ev, r in zip(events, rows) if r is not None]
    sources = [r for _, r in pairs]
    table = "\n".join(
        f"- [{n}] {_when(ev)} {names.get(ev['actor'], ev['actor'])} ({ROLE_LABELS[ev['role']]}) "
        f"{STATUS_LABELS[ev['status']]}: {ev['title']}" + (" ※議員個人の発言" if ev["personal"] else "")
        + ("  ← 利用者が選択中" if ev["id"] == focus else "")
        for n, (ev, _) in enumerate(pairs, 1))
    material = "\n\n".join(f"{format_source(n, r)}\n{r['text']}" for n, r in enumerate(sources, 1))
    period = "〜".join(x for x in (scope.get("date_from"), scope.get("date_to")) if x) or "全期間"
    fields = "、".join(scope.get("fields") or []) or "すべて"
    prompt = (f"【表示中の範囲】期間 {period} / 分野 {fields} / 出来事 {len(data['events'])} 件のうち、"
              f"質問に近い {len(pairs)} 件\n\n【年表】\n{table}\n\n【資料】\n{material}\n\n"
              f"{RULES}\n\n{TIMELINE_RULES}\n\n【質問】\n{question}")
    messages = [{"role": "system", "content": SYSTEM}, *(history or []), {"role": "user", "content": prompt}]
    return [("", messages)], sources
