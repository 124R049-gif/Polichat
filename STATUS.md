# プロジェクト現状 (引き継ぎメモ)

最終更新: 2026-11-20 / OneDrive 経由で複数の PC から開く前提のメモ。
作業を始める前にこのファイルを読み、終えたら「作業ログ」と該当箇所を更新する。

- 概要・使い方: [README.md](README.md)
- 設計: [docs/architecture.md](docs/architecture.md)
- 細かい進捗: [docs/TODO.md](docs/TODO.md)

## 1. ひとことで

省庁・政党・労組・経済団体・国会会議録の一次資料を集めて構造化し、Ollama 上の LLM (gemma4) が
**出典付きで回答する RAG チャット**。Web 画面 (FastAPI) と Discord Bot の2つの入口がある。

## 2. 今できること

| 機能 | コマンド | 状態 |
|---|---|---|
| 資料収集 (RSS / 新着一覧 / 国会会議録 API、robots.txt 遵守) | `python main.py collect` | 動作 |
| 本文抽出 (HTML / PDF / DOCX)・チャンク化・公開日推定・低品質除外 | `python main.py extract` | 動作 |
| ニュース見出し (話題検知のみ、根拠には使わない) | `python main.py news` | 動作 |
| LLM タグ付け (固定の分類表) | `python main.py tag` | 動作 (要 Ollama) |
| 埋め込み | `python main.py embed` | 動作 (要 Ollama) |
| 出典付き回答 (キーワード + ベクトル検索、RRF 統合) | `python main.py ask "質問"` | 動作 (要 Ollama) |
| Web チャット (ログイン・1日の回数制限) | `python main.py serve` | 動作 |
| 利用者管理 | `python main.py user add/list/limit/passwd/disable/enable/delete` | 動作 |
| **収集〜埋め込みの一括 / 定時実行** | `python main.py update [--loop]` | 新規・未コミット |
| **Discord Bot (`/ask`, `/status`)** | `python main.py bot` | 新規・未コミット |
| **exe 化 (PyInstaller)** | `pyinstaller nihon-kanshi.spec` | 新規・未コミット |

## 3. データの量 (2026-11-20 時点の `data/crawler.db`)

- ソース 37 / 文書 2,477 (抽出済み 2,461、要OCR 7、エラー 4) / チャンク 10,472 / 利用者 1
- 最後に収集した日時: 2026-11-16 (それ以降 `collect` は回していない)
- DB サイズ 約 95MB。`data/` は git に入らず、**OneDrive の同期だけで共有されている**

## 4. git の状態

- コミットは初回の1件のみ (`ae39025`, 2026-11-16)。**それ以降の作業はすべて未コミット**
- 未コミットの変更 (主なもの)
  - 新規: `bot/discord_bot.py`, `scheduler.py`, `llm/tunnel.py`, `nihon-kanshi.spec`
  - 変更: `main.py` (`update` / `bot` コマンド、exe ダブルクリック時に Bot 起動)、
    `config.py` (exe 対応、`.env` 読み込みの `secret()`)、`config.yaml` (`discord` / `schedule` / `tunnel` / `rag.unofficial_top_k`)、
    `database/db.py` (WAL・ロック待ち 60 秒)、`rag/answer.py` (非公式の論評 `commentary` を別枠検索・別の節で回答)、
    `parsing/extract.py` (note の本文セレクタ)、`server/auth.py` (Discord 利用者 `discord:<ID>`)、`web/index.html`、ドキュメント類
  - 依存追加: `discord.py`
- 早めにコミットしておくと、OneDrive の同期事故があっても戻せる

## 5. 複数環境で使うときの注意 (重要)

1. **2台で同時に動かさない。** SQLite (`data/crawler.db`) を OneDrive 上で2台から同時に書くと壊れる。
   特に `serve` / `bot` は起動中に定時更新 (毎日 05:00) も走らせるので、片方の PC だけで起動する
2. **PC を切り替える前にプロセスを止め、同期が終わるのを待つ。** DB は WAL モードなので、
   `crawler.db-wal` / `crawler.db-shm` が残っている間は本体だけ同期されても中身が揃わない。
   止めればこれらは本体に書き戻されて消える
3. **`.git/` も OneDrive で同期されている。** 2台で同時に git 操作しない。競合コピー
   (`ファイル名-PC名.ext` のようなもの) ができたら同期の衝突を疑う
4. **環境ごとに用意するもの** (同期されない、または PC ごとに違う)
   - Python 3.12 と `pip install -r requirements.txt`
   - LLM への接続: `config.yaml` の `llm` は `localhost:11434` (GPU) / `11435` (CPU・埋め込み) 前提。
     学科の GPU サーバーへの SSH ポート転送を張るか、`tunnel.command` に ssh コマンドを書く (鍵認証が必要。鍵は PC ごと)
   - `.env` (`DISCORD_BOT_TOKEN=...`): git には入らないが OneDrive では同期される。現在は未作成
5. **同期されるが環境依存なもの**: `__pycache__/`, `dist/`, `.claude/`。
   `dist/nihon-kanshi.exe` (11/20 10:32 ビルド) は最新の変更より古いので、使う前に作り直す
6. `data/update.lock` が残っていると定時更新が「別の更新が実行中」で見送られる (12時間で自動的に無効)。
   他の PC で更新中でないことを確かめてから消してよい

## 6. 未確認・未完了 (直近)

- 定時更新・Discord Bot・exe は実装済みだが、**Bot を実際の Discord サーバーで動かした確認は未記録** (トークン未設定)
- `docs/TODO.md` の「定時実行」はまだ未チェックのまま (実装は `scheduler.py` で済み → 動作確認後にチェック)
- 非公式の論評 (`org_type: commentary`) の受け皿はあるが、**対象の書き手は未登録** (選び方の基準を先に決める)
- 収集が 11/16 で止まっている → `python main.py update` で最新化する

## 7. 次にやること候補 (TODO.md から抜粋)

- 未コミットの変更をコミット
- Discord Bot の実地確認 (トークン発行 → `.env` → `python main.py bot`)
- キーワード検索の語の切り出し改善 (長い漢字列が部分一致しない)
- 党規約の確認が残っている党 (立憲・国民・参政・れいわ ほか) の `source/party_rules.json` への反映
- 回答精度の評価用の質問セット、タグの正解データ
- 会話の保存、HTTPS 公開、PostgreSQL 移行 (公開に向けて)

## 8. 作業ログ

新しいものを上に追記する。どの PC で何をしたか書いておくと同期の問題を追いやすい。

- 2026-11-20: 定時更新 (`scheduler.py`)、Discord Bot、SSH トンネル自動起動、exe 化、非公式の論評の別枠検索を追加 (未コミット)。このメモを作成
- 2026-11-17: ログイン・回数制限、README 整備
- 2026-11-16: 初回コミット (収集・抽出・タグ付け・埋め込み・出典付き回答・チャット画面)
