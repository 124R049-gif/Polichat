# プロジェクト現状 (引き継ぎメモ)

最終更新: 2026-11-21 / OneDrive 経由で複数の PC から開く前提のメモ。
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
| 収集〜埋め込みの一括 / 定時実行 | `python main.py update [--loop]` | 動作 (`890ef30` でコミット) |
| Discord Bot (`/ask`, `/status`) | `python main.py bot` | 実装済み・実地未確認 |
| Discord Bot の URL 解説 (@メンション + URL、右クリック「URL を解説」、`/explain`) | `python main.py bot` | 実装済み・未コミット・実地未確認 (偽の Discord・LLM で確認) |
| Docker (大学のサーバー・Raspberry Pi 向け) | `docker compose up -d --build` | 新規・未コミット・ビルド未確認 |
| exe 化 (PyInstaller) | `pyinstaller nihon-kanshi.spec` | 動作 (`890ef30` でコミット) |
| **Web と Bot の共通部分 (`core/`)** | — | 新規・未コミット (テスト用コピーで確認済み) |
| **タイムライン** (政府・各党の実績・進行中・予定。Web 画面の「タイムライン」) | `python main.py events` | 新規・未コミット。偽の LLM で取り出しと画面を確認、実際の LLM では未確認 |
| 国会会議録を遡って取る | `python main.py collect -s kokkai --since 2026-01-01 -n 1000` | 新規・未コミット。2026-01-01 以降を取得 (11/23) |
| Slack Bot (@メンション・DM・`/status`、Socket Mode) | `python main.py slack` | 実装済み・未コミット・実地未確認 (API を差し替えたテストで確認) |

## 3. データの量 (2026-11-20 時点の `data/crawler.db`)

- ソース 37 / 文書 2,477 (抽出済み 2,461、要OCR 7、エラー 4) / チャンク 10,472 / 利用者 1
- 最後に収集した日時: 2026-11-16 (それ以降 `collect` は回していない)
- DB サイズ 約 95MB。`data/` は git に入らず、**OneDrive の同期だけで共有されている**

## 4. git の状態

- コミット: `ae39025` (11/16 初回)、`890ef30` (11/21 09:25、Discord Bot・exe 化・定時更新・非公式の論評)
- 未コミットの変更 (11/21、Web と Bot の共通化)
  - 新規: `core/accounts.py` (旧 `server/auth.py` を移動)、`core/answer_service.py`、`bot/common.py`、`server/throttle.py`
  - 削除: `server/auth.py`
  - 変更: `server/app.py`、`bot/discord_bot.py` (共通部分を使う形に)、`main.py` (import 先)、
    `config.yaml` (`discord.max_concurrent` → `llm.max_concurrent`)、`web/index.html` (順番待ちの表示)、ドキュメント類
- 未コミットの変更 (11/21、Slack Bot): 新規 `bot/slack_bot.py`、変更 `main.py` (`slack`)、`config.py`・`config.yaml` (`slack`)、
  `requirements.txt`、README・`docs/architecture.md`・このファイル
- 未コミットの変更 (11/21、Discord の URL 解説・Docker): 新規 `core/url_reader.py`、`rag/explain.py`、`Dockerfile`、
  `compose.yaml`、`.dockerignore`。変更 `bot/discord_bot.py`、`bot/common.py` (`plan_fn`・`explain_plan`)、
  `core/answer_service.py` (`answer(plan_fn=)`)、`config.py` (`URL_READER`、`LLM_*_URL`・`TUNNEL_COMMAND` の環境変数)、
  `config.yaml` (`url_reader`、`discord.message_content_intent`)、`llm/tunnel.py` (Linux での引数分割・BatchMode)、
  `requirements.txt` (`yt-dlp`)、README・このファイル
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
   - `.env` (`DISCORD_BOT_TOKEN=...`、`SLACK_BOT_TOKEN=xoxb-...`、`SLACK_APP_TOKEN=xapp-...`): git には入らないが OneDrive では同期される
5. **同期されるが環境依存なもの**: `__pycache__/`, `dist/`, `.claude/`。
   `dist/nihon-kanshi.exe` (11/20 10:32 ビルド) は最新の変更より古いので、使う前に作り直す
6. `data/update.lock` が残っていると定時更新が「別の更新が実行中」で見送られる (12時間で自動的に無効)。
   他の PC で更新中でないことを確かめてから消してよい

## 6. 未確認・未完了 (直近)

- 定時更新・Discord Bot・exe は実装済みだが、**Bot を実際の Discord サーバーで動かした確認は未記録** (トークン未設定)
- Slack Bot も実ワークスペースでは未確認。README「Slack Bot として使う」の手順でアプリを作り、
  メンション → スレッドで回答・出典、スレッド内の続きの質問が前のやりとりを踏まえるか (メタデータが読めているか)、
  DM (`allow_dm: true` にして)、`/status`、Bot を招待していないチャンネルでの案内を確かめる
- URL 解説: 実際の LLM (gemma4:e4b) で解説の質 (要点・発言者の区別・資料番号の付け方) を未確認。
  照らし合わせの資料はベクトル検索なしの状態でしか試していない (Ollama に届かなかったため)。
  YouTube の字幕は `yt-dlp` を入れた環境 (Docker イメージ) で未確認 (この PC では未インストールで、題名だけの代替経路のみ確認)
- Docker: イメージのビルド・Raspberry Pi (arm64) での動作・コンテナ内からの SSH 転送は未確認
- exe (`nihon-kanshi.spec`) に `slack_bolt` が入るかは未確認 (動的 import があれば hiddenimports が要る)
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

- 2026-11-23: タイムライン (`timeline/events.py`・`timeline/view.py`、`source/actors.json`、`events`/`event_scans` テーブル、
  `GET /api/timeline`、画面の「タイムライン」、`python main.py events`、定時更新の手順に `events`、`config.yaml` の `timeline`)。
  国会会議録の遡り取得 (`collect --since/--until`) で 2026-01-01 以降を取得。Ollama に届かなかったため、
  DB のコピーと偽の LLM で取り出し・まとめ・API・画面 (年表・一覧・詳細・絞り込み) を確認。未コミット
- 2026-11-23: 実際の LLM (gemma4:e4b) で events を試し、掲載ページと添付 PDF から同じ出来事が重複するのを確認。
  組で1回に抽出・言い換えの除去・画面で状態違い (予定→実施済み) もまとめる形に修正し、`events --redo` を追加。
  既存の取り出し結果 108 件は画面で 93 件にまとまることを確認。新しい取り出し方での実際の LLM の結果は未確認

- 2026-11-21: Discord Bot に URL の解説 (`core/url_reader.py` で記事・PDF・X の投稿 (oEmbed)・YouTube (yt-dlp の字幕) を読み、
  `rag/explain.py` で要点・発言者の区別・一次資料との照らし合わせを生成)。内部アドレス・標準外ポートへの接続を防ぐ確認付き。
  Yahoo!ニュース (pickup から記事本体へ)・毎日・読売・日経 (会員限定は注記)・X の実際の URL で読み込みを確認。
  Docker 一式 (大学のサーバー・Raspberry Pi 向け、LLM の接続先は環境変数で上書き)。未コミット

- 2026-11-21: Slack Bot (`bot/slack_bot.py`、`python main.py slack`、`config.yaml` の `slack`、`requirements.txt` に
  `slack_bolt` と `aiohttp`、`config.py` に `SLACK`)。受け方は @メンションと DM だけ (`/ask` は `slash_ask` で有効化、既定は無効)。
  再送・編集・Bot の発言・同じ利用者の連投では回答しない。テスト用コピーで Slack API と LLM を差し替え、
  受付→順番待ち→本文の書き換え→出典→残り回数、LLM 失敗時の回数返却、上限・投稿不可・許可外の場所、
  スレッドの履歴の組み立てを確認 (未コミット、実ワークスペースは未確認)

- 2026-11-21: Web と Bot の共通化 (`core/`、`bot/common.py`)。Slack Bot の準備。同時生成数の上限を Web と Bot の合計に、
  Bot でも CPU の時間帯は断るように (未コミット。テスト用コピーで Web・Bot 共通部分・回数の精算・順番待ちを確認)
- 2026-11-20: 定時更新 (`scheduler.py`)、Discord Bot、SSH トンネル自動起動、exe 化、非公式の論評の別枠検索を追加 (未コミット)。このメモを作成
- 2026-11-17: ログイン・回数制限、README 整備
- 2026-11-16: 初回コミット (収集・抽出・タグ付け・埋め込み・出典付き回答・チャット画面)
