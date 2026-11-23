"""SQLite スキーマと基本操作。

sources   収集対象 (省庁・政党・労組など)
documents 取得した文書1件 (HTMLページ、または添付のPDF/DOCX)
chunks    検索とRAG用に分割した本文。ページ番号を持つ
tags      固定の分類表 (分野・種類)。document_tags で文書に付与
embeddings チャンクの埋め込みベクトル (float32)
articles  ニュース見出し (一次資料ではない。話題の検知用)
users / sessions / usage  利用者、ログイン、1日ごとの質問回数 (core/accounts.py)
events / event_scans  タイムラインの出来事・予定と、抽出済みの文書 (timeline/events.py)
"""
import json
import sqlite3

from config import PATHS

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    org_type  TEXT NOT NULL,
    url       TEXT NOT NULL,
    last_scan TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      TEXT NOT NULL REFERENCES sources(id),
    parent_id      INTEGER REFERENCES documents(id),
    url            TEXT NOT NULL UNIQUE,
    title          TEXT,
    published_date TEXT,
    file_type      TEXT,
    content_hash   TEXT,
    raw_path       TEXT,
    status         TEXT NOT NULL,
    error          TEXT,
    n_chars        INTEGER,
    exclude_reason TEXT,           -- 検索対象から外す理由 (parsing/quality.py)
    author         TEXT,           -- 発言者 (国会会議録)
    affiliation    TEXT,           -- 発言者の会派または役職 (国会会議録)
    fetched_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    extracted_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    page        INTEGER,
    text        TEXT NOT NULL,
    exclude_reason TEXT            -- 検索対象から外す理由 (parsing/quality.py)
);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    UNIQUE(kind, name)
);
CREATE TABLE IF NOT EXISTS document_tags (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES tags(id),
    method      TEXT,
    PRIMARY KEY (document_id, tag_id)
);

CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    model    TEXT NOT NULL,
    vector   BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    daily_limit   INTEGER,          -- NULL なら config.yaml の auth.daily_limit
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,    -- Cookie のトークンの SHA-256 (トークン自体は保存しない)
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day     TEXT NOT NULL,          -- YYYY-MM-DD (サーバーの現地日付)
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

-- タイムラインの出来事・予定。1件は1つの文書から LLM で取り出し、原文の一節 (quote) で根拠を確かめたもの
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_id    INTEGER REFERENCES chunks(id) ON DELETE SET NULL,  -- quote を含むチャンク
    actor       TEXT NOT NULL,      -- source/actors.json の id (government / jimin など)
    personal    INTEGER NOT NULL DEFAULT 0,  -- 議員個人の発言から取り出したもの (党の立場としては扱わない)
    status      TEXT NOT NULL,      -- done (実施済み) / ongoing (進行中) / planned (予定・目標)
    kind        TEXT NOT NULL,
    field       TEXT,               -- 分野 (taxonomy.json)
    title       TEXT NOT NULL,
    summary     TEXT,
    start_date  TEXT,               -- YYYY-MM-DD / YYYY-MM / YYYY。予定で時期が書かれていなければ NULL
    end_date    TEXT,
    quote       TEXT NOT NULL,
    model       TEXT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_events_actor ON events(actor, start_date);
CREATE INDEX IF NOT EXISTS idx_events_document ON events(document_id);
CREATE TABLE IF NOT EXISTS event_scans (
    document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    n_events    INTEGER NOT NULL,
    model       TEXT,
    scanned_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    content TEXT,
    url TEXT UNIQUE,
    source TEXT,
    published_date TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def get_connection():
    PATHS["db"].parent.mkdir(parents=True, exist_ok=True)
    # 定時更新が書き込み中でも回答・ログインが待って続けられるよう、ロック待ちを長めにする
    conn = sqlite3.connect(PATHS["db"], timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL: 書き込み中も読み出しを止めない (設定は DB ファイルに残る)
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    """テーブルを作成し、ソース台帳と分類表をDBに反映する。"""
    conn = get_connection()
    conn.executescript(SCHEMA)
    # 既存DBへの列追加
    for table, column in (("documents", "exclude_reason"), ("chunks", "exclude_reason"),
                          ("documents", "author"), ("documents", "affiliation")):
        columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

    with open(PATHS["sources"], encoding="utf-8") as f:
        for s in json.load(f):
            conn.execute(
                """INSERT INTO sources (id, name, org_type, url) VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     name = excluded.name, org_type = excluded.org_type, url = excluded.url""",
                (s["id"], s["name"], s["org_type"], s["url"]),
            )

    with open(PATHS["taxonomy"], encoding="utf-8") as f:
        for kind, names in json.load(f).items():
            for name in names:
                conn.execute(
                    "INSERT OR IGNORE INTO tags (kind, name) VALUES (?, ?)", (kind, name)
                )

    conn.commit()
    conn.close()


def search_chunks(conn, query, limit=10):
    """本文の全文検索。空白区切りの語をすべて含むチャンクを返す。

    trigram 索引は3文字未満の語 (「介護」など) を引けないため、
    短い語が含まれる場合は LIKE 検索に切り替える。
    """
    terms = query.split()
    if not terms:
        return []
    select = """
        SELECT c.id, c.page, c.text, d.id AS document_id, d.title, d.url,
               d.published_date, s.name AS source_name, s.org_type
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        JOIN sources s ON s.id = d.source_id
    """
    if all(len(t) >= 3 for t in terms):
        match = " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)
        sql = (select + " JOIN chunks_fts f ON f.rowid = c.id"
               " WHERE chunks_fts MATCH ? ORDER BY f.rank LIMIT ?")
        return conn.execute(sql, (match, limit)).fetchall()
    where = " AND ".join("c.text LIKE ?" for _ in terms)
    sql = select + f" WHERE {where} ORDER BY d.published_date DESC LIMIT ?"
    return conn.execute(sql, [f"%{t}%" for t in terms] + [limit]).fetchall()
