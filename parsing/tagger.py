"""LLM による自動タグ付け。分類表 (source/taxonomy.json) にあるタグだけを付ける。"""
import json

from config import PATHS, RAG
from database.db import get_connection
from data_collection.kokkai import inherit_tags
from llm.client import chat, endpoint

PROMPT = """あなたは日本の行政・政治文書を分類する係です。
次の文書に、分類表から「分野」と「種類」を選んで付けてください。

分野の選び方:
- 文書の中心的な主題を表すものを1個選ぶ。ほとんどの文書は1個で足りる。
- 2個目・3個目は、その分野も同じ重みで主題になっている場合に限って加える。
- 少し触れているだけの分野、発行元の所管というだけの分野は選ばない。
  例: 省庁のメール障害のお知らせ → 「行政・デジタル」のみ
  例: 介護報酬改定についての審議会資料 → 「介護」のみ
  例: 子育て世帯向けの減税方針 → 「子育て・教育」「財政・税制」

種類の選び方:
- 文書そのものの形式で選ぶ。行政機関のお知らせ・発表は「報道発表」、
  組織の立場や要求を表明する談話・見解・提言は「意見・声明」。

【分野の定義】
{fields}

【種類の定義】
{kinds}

発行元: {source}
タイトル: {title}
本文の冒頭:
{text}"""


def _schema(taxonomy):
    return {
        "type": "object",
        "properties": {
            "分野": {"type": "array", "minItems": 1, "maxItems": 3,
                   "items": {"type": "string", "enum": list(taxonomy["分野"])}},
            "種類": {"type": "string", "enum": list(taxonomy["種類"])},
        },
        "required": ["分野", "種類"],
    }


def tag_pending(limit=None):
    """本文抽出済みでタグのない文書にタグを付ける。付けた件数を返す。"""
    with open(PATHS["taxonomy"], encoding="utf-8") as f:
        taxonomy = json.load(f)
    schema = _schema(taxonomy)
    definitions = {kind: "\n".join(f"- {name}: {desc}" for name, desc in tags.items())
                   for kind, tags in taxonomy.items()}
    conn = get_connection()
    tag_ids = {(r["kind"], r["name"]): r["id"] for r in conn.execute("SELECT * FROM tags")}
    docs = conn.execute("""
        SELECT d.id, d.title, s.name AS source, p.title AS parent_title FROM documents d
        JOIN sources s ON s.id = d.source_id
        LEFT JOIN documents p ON p.id = d.parent_id
        WHERE d.status = 'extracted' AND COALESCE(d.file_type, '') != 'speech'
          AND NOT EXISTS (SELECT 1 FROM document_tags t WHERE t.document_id = d.id)
        ORDER BY d.id""").fetchall()
    if limit:
        docs = docs[:limit]
    _, model, device = endpoint("tag")
    print(f"対象 {len(docs)} 件 / モデル {model} ({device})")

    done = 0
    for doc in docs:
        # 国会の会議は本文を発言 (子文書) に持つので、そちらを読む
        rows = conn.execute("""
            SELECT c.text FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE d.id = ? OR (d.parent_id = ? AND d.file_type = 'speech')
            ORDER BY d.id, c.seq""", (doc["id"], doc["id"]))
        text = "\n".join(r["text"] for r in rows)[:RAG["tag_input_chars"]]
        title = doc["title"] or "不明"
        if doc["parent_title"]:  # 添付は「別紙」などの題名が多いので掲載ページの題名を添える
            title = f"{title} (「{doc['parent_title']}」の添付資料)"
        prompt = PROMPT.format(fields=definitions["分野"], kinds=definitions["種類"],
                               source=doc["source"], title=title, text=text)
        try:
            result = json.loads(chat("tag", [{"role": "user", "content": prompt}], schema))
            result["分野"] = list(dict.fromkeys(result["分野"]))  # 重複を除く
            names = [("分野", n) for n in result["分野"]] + [("種類", result["種類"])]
            ids = [tag_ids[n] for n in names]  # 分類表にないタグは KeyError で弾く
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"  [失敗] {doc['id']} {doc['title']}: {type(e).__name__}")
            continue
        conn.executemany(
            "INSERT OR IGNORE INTO document_tags (document_id, tag_id, method) VALUES (?, ?, ?)",
            [(doc["id"], i, model) for i in ids])
        conn.commit()
        done += 1
        print(f"  {doc['id']} {(doc['title'] or '')[:36]} → "
              f"{'・'.join(result['分野'])} / {result['種類']}")
    inherit_tags(conn)  # 発言には会議のタグを引き継ぐ
    conn.close()
    return done
