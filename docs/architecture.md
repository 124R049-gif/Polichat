# アーキテクチャ

## 全体の流れ

```
source/sources.json (収集対象の台帳)
   ↓ discover   RSS・新着一覧から文書URLを発見 (同じ組織のドメインのみ)
   ↓ fetcher    robots.txt 確認・アクセス間隔を守って取得
   ↓ collector  原本を data/raw/ に保存、documents に記録、添付PDF/DOCXも取得
   ↓ extract    HTML/PDF/DOCX から本文抽出 → chunks (ページ番号付き) + 全文検索索引
   ↓ tagger     LLM による自動タグ付け → document_tags
   ↓ vector_db  チャンクの埋め込み → embeddings
   ↓ answer     キーワード + ベクトルの併用検索 → 出典付き回答
   ↓ server     API (FastAPI) → チャット画面 (web/index.html)
```

## チャット画面

`python main.py serve` で起動し、`http://127.0.0.1:8000/` を開く。

- `POST /api/login` / `POST /api/logout` / `GET /api/me` (利用者名と本日の残り回数)
- `GET /api/status`: 使用中のモデル、GPUを使える時間帯か、文書数
- `POST /api/chat`: `{question, history}` を受け、1行1イベントのJSONを順に返す
  (`sources` → `delta` が複数 → `done`、失敗時は `error`)
- 画面はビルド不要の単一HTML。回答はHTMLエスケープしてから整形する
- `GET /api/options`: 絞り込みの選択肢 (資料がある発行主体の種類と分野)
- `POST /api/chat` の `filters`: `{org_types, fields, date_from, date_to}`。条件に合う文書のチャンクだけを検索対象にする
- 履歴には質問と回答だけを入れ、資料本文は毎回検索し直す。続きの質問は直前の質問を足して検索する
- GPUを使えない時間帯は回答を受け付けない (CPUでは終わらないため)
- 画面以外の API はログインが必要 (`core/accounts.py`)。利用者は `python main.py user add` で登録する
  - パスワードは scrypt のソルト付きハッシュ、Cookie のセッショントークンは DB に SHA-256 だけを保存
  - Cookie は HttpOnly・SameSite=Lax。POST は JSON だけを受け付け、他サイトのフォームからの送信を防ぐ
  - ログイン失敗は利用者名ごとに5回、接続元ごとに20回で15分止める
  - 質問は利用者ごと1日の回数を数え、上限を超えたら 429 を返す。サーバー側の失敗は回数を戻す
- 既定では 127.0.0.1 で待ち受ける。外から使わせるときは `--host` を指定する

## 入口と共通部分 (Web 画面と Bot の分離)

```
入口 (表示と操作だけ)          共通部分 (core/)
  server/  Web 画面   ─┐
  bot/discord_bot      ├─→  accounts        利用者・セッション・1日の回数
  bot/slack_bot        ─┘    answer_service  順番待ち → 資料検索 → 生成 → 回数の精算
        └ bot/common.py  Bot 共通 (受付・スレッド橋渡し・長文分割・出典の値)
```

- 回数を1つ使う (`accounts.consume`) のは入口側。断り方が入口ごとに違うため (Web は 429、Bot は本人だけに見える返信)
- `answer_service.answer()` はイベント (`queued` / `sources` / `delta` / `error` / `done`) を返す。
  サーバー側の失敗 (CPU の時間帯、LLM の障害、想定外の例外) では回数を戻す。利用者の中止では戻さない
- 同時生成数 `llm.max_concurrent` は Web と Bot の合計。別プロセスでも、このPCの一時フォルダのロックファイルで数える
- Bot の利用者は `discord:<ID>` / `slack:<ワークスペースID>:<利用者ID>` のような名前で自動的に作られ、
  `python main.py user limit/disable` で Web の利用者と同じように管理できる

### Slack Bot (`bot/slack_bot.py`)

- Bolt for Python の非同期版 (`AsyncApp`) を Socket Mode で動かす。生成は `common.events()` が別スレッドで回す
- 受け方は @メンション (`app_mention`) と Bot との DM (`message.im`) だけ。`/ask` は `slack.slash_ask` で有効にしたときだけ登録する
- 回答はメンションした投稿のスレッドに投稿し、生成中は `chat.update` で書き換える (間隔 `slack.edit_interval`、
  `common.EDIT_INTERVAL` 未満にはしない。429 は Retry-After を待ってやり直す)。3500 文字で分割し、続きと出典は同じスレッドへ
- LLM の Markdown は mrkdwn に直す (`& < >` をエスケープ、`[題](url)` → `<url|題>`、太字・見出し・箇条書き)
- 続きの質問: スレッド内でメンションされたら `conversations.replies` でやりとりを読み、`history` にする (直近6件)。
  Bot の投稿にはメタデータ (`kanshi_answer` / `kanshi_other`) を付け、回答だけを assistant、出典・通知は除く。
  人の発言は Bot へのメンションを含むものだけ (DM はすべて) を user にする
- 暴発の防止: subtype 付き・編集・Bot の発言には反応しない。同じ投稿 (channel, ts) にはプロセス内で1回だけ答える
  (Socket Mode の再送対策)。同じ利用者の回答の生成中に来た質問は、回数を使う前に断る
- 投稿できない (Bot がチャンネルにいない) ときは回数を戻し、本人にだけ招待の方法を返す。LLM の失敗時は出典を付けない

## タイムライン (`timeline/`)

```
documents (政党・行政機関の資料、国会の発言)
   ↓ timeline/events.py   主体を発行元で決め、LLM で出来事・予定を取り出す → events (根拠の一節つき)
   ↓ timeline/view.py     別の資料に載った同じ主体の同じ出来事を1件にまとめる
   ↓ GET /api/timeline     ?fields=&actors=&personal=&date_from=&date_to=
   ↓ web/index.html        スイムレーン (行=主体、横=時間、今日の線) / 狭い画面では一覧
```

- 主体 (行) は `source/actors.json`。政党の Web サイトの資料はその党、行政機関の資料は政府。
  国会の発言は、大臣・政務官・官僚の答弁を政府、議員の発言を会派から党に割り当てる
- LLM には主体を選ばせず、「その主体自身の行動」だけを取り出させる (他党・政府の行動の取り違えを防ぐ)
- LLM の返す quote (本文からの抜き出し) が本文に見つからない出来事は捨てる (10文字ずつの断片の6割以上が本文にあること)
- 重複の防ぎ方
  - 掲載ページと添付 (PDF など) は1組として LLM に1回で渡す (別々に渡すと資料の数だけ同じ出来事が出る)。
    出来事は quote を含む資料に結び付ける
  - 1回の結果の中で題名の似たもの (文字の2-gram の Dice 係数 0.6 以上) は言い換えとして1件に絞る
  - 画面に出すとき、別の資料の同じ出来事をまとめる: 時期の差45日以内なら Dice 0.5 以上、
    片方が時期未定、または状態が違う (予定→実施済み) なら 0.65 以上。題名・状態・時期は公開日の新しい資料のものを使う
  - 取り出し方を変えたら `python main.py events --redo` でやり直す
- status は done / ongoing / planned。時期は資料の公開日を基準に LLM が日付に直し、YYYY / YYYY-MM / YYYY-MM-DD で保存する。
  年度は4月始まり。done で時期がなければ公開日、planned で時期がなければ NULL (画面では「時期未定」)
- 会議録の発言は量が多いので、行動・予定を表す語 (法案・閣議決定・予定・年度など) を含むものだけを LLM に渡す。
  渡さなかった発言も `event_scans` に記録し、再び調べない
- 議員個人の発言 (`speech_treatment` が「議員個人の発言」) から取り出したものは `personal = 1`。既定では表示しない
- 1回の処理数は `timeline.max_per_run` (新しい資料から順)。定時更新の `events` の手順で少しずつ進む

## ファイル構成

| ファイル | 役割 |
|---|---|
| `main.py` | コマンドライン入口 |
| `config.yaml` / `config.py` | 設定 |
| `database/db.py` | スキーマ、台帳と分類表の反映、全文検索 |
| `data_collection/fetcher.py` | 礼儀正しいHTTP取得 |
| `data_collection/discover.py` | 文書URLと添付の発見 |
| `data_collection/collector.py` | 収集の本体 |
| `data_collection/rss.py` | ニュース見出し (一次資料ではない。話題検知用) |
| `parsing/extract.py` | 本文抽出とチャンク分割 |
| `parsing/tagger.py` | 自動タグ付け |
| `parsing/dates.py` | 公開日の推定と補完 |
| `parsing/quality.py` | 検索対象から外す文書・チャンクの判定 |
| `source/party_rules.json` | 党規約による議員発言の扱い (根拠の条文とURLつき) |
| `data_collection/kokkai.py` | 国会会議録の取得 (国立国会図書館のAPI) |
| `llm/client.py` | Ollama 接続、GPU/CPU の時間帯切替 |
| `rag/vector_db.py` | 埋め込みの作成・保存・類似検索 |
| `rag/answer.py` | 併用検索と出典付き回答 |
| `server/app.py` | API サーバー |
| `core/accounts.py` | 利用者、ログイン、セッション、1日の利用回数 (Web と Bot で共通) |
| `core/answer_service.py` | 質問1件の処理 (順番待ち・検索・生成・回数の精算)。Web と Bot で共通 |
| `server/throttle.py` | Web のログイン失敗の制限 |
| `bot/common.py` | Bot 共通 (利用者の受付、生成スレッドとの橋渡し、長文の分割、出典の値) |
| `web/index.html` | チャット画面 |
| `bot/discord_bot.py` | Discord Bot (`/ask`・`/status`)。Discord での表示と操作だけ |
| `bot/slack_bot.py` | Slack Bot (メンション・DM・`/status`、Socket Mode)。Slack での表示と操作だけ |
| `llm/tunnel.py` | LLM サーバーへの SSH ポート転送を serve / bot と一緒に起動 |
| `timeline/events.py` | 資料からタイムラインの出来事・予定を取り出す (LLM) |
| `timeline/view.py` | タイムラインの API 用データ (同じ出来事をまとめる) |
| `source/actors.json` | タイムラインの主体 (政府と各党) と会議録の会派名の対応 |
| `nihon-kanshi.spec` | exe 化 (PyInstaller) の設定 |

## データベース

- `sources`: 収集対象。`org_type` で発行主体の種類を区別する。`commentary` は非公式の論評 (note など)
  - note のアカウントは RSS をそのまま入口にできる: `{"id": "note-xxx", "name": "書き手の名前 (note)", "org_type": "commentary", "url": "https://note.com/xxx", "entries": [{"type": "feed", "url": "https://note.com/xxx/rss"}]}`
- `documents`: 文書1件。`parent_id` は添付元のページ。`status` は下表
- `chunks` / `chunks_fts`: 本文断片と全文検索索引 (trigram)
- `tags` / `document_tags`: 分類表 (分野・種類) と文書への付与
- `documents.exclude_reason` / `chunks.exclude_reason`: 検索対象から外す理由 (名簿・外国語・表など)。原本と抽出結果は残す
- 国会会議録は会議を親文書 (`file_type = meeting`)、発言を子文書 (`speech`) とし、`author` に発言者、`affiliation` に役職または会派を入れる
- `users` / `sessions` / `usage`: 利用者、ログイン中のセッション、利用者ごと1日ごとの質問回数
- `events` / `event_scans`: タイムラインの出来事・予定 (主体、状態、種類、分野、時期、根拠の一節) と、取り出し済みの文書
- `articles`: ニュース見出し

| status | 意味 |
|---|---|
| `fetched` | 取得済み・未抽出 |
| `extracted` | 本文抽出済み |
| `needs_ocr` | テキスト層のないPDF (OCR待ち) |
| `empty` | 本文を抽出できなかった |
| `duplicate` | 別URLと同一内容 |
| `skipped` | 対象外の形式 |
| `error` | 取得・抽出の失敗 (次回の collect で再試行) |

## 設計方針

- **一次資料を根拠にし、非公式の論評は分けて扱う。** ニュース記事は分析の根拠にしない。note などの個人の論評 (`org_type: commentary`) は公式資料とは別枠で検索し (`rag.unofficial_top_k` 件)、回答では「非公式の論評 (参考)」の節に書き手の主張として分ける。公式資料の枠は減らさない
- **発行主体を常に持ち回る。** 省庁の統計と政党・団体の主張を回答時に区別するため
- **出典を辿れること。** 原本を保存し、チャンクにページ番号を持たせる
- **タグは固定の分類表から選ぶ。** 自由生成にすると表記が揺れて絞り込みに使えない
- **量の多い発行元に引きずられない。** 名簿や外国語版は検索から外し、回答に使う資料は1文書3件・1機関 (国会は1会派・役職) 2件までにする
- **議員の発言を党の立場と取り違えない。** 党規約で党の決定に反する意見の表明を制限している党の議員発言は党の方針に沿うものとして、それ以外は議員個人の非公式見解・参考意見として扱い、回答では別の節に分けて生成する。首相・大臣の答弁は政府の説明として扱う
- **相手サイトに負荷をかけない。** robots.txt 遵守、ホストごとに2秒間隔、取得拒否 (403) は回避しない

## LLM

学科共用サーバーの Ollama を SSH のポート転送経由で使う。

```
ssh -J <踏み台> -L 11434:127.0.0.1:11434 -L 11435:127.0.0.1:11435 <LLMサーバー>
```

| 用途 | 21:00〜9:00 と日曜終日 | それ以外 |
|---|---|---|
| 回答生成 | GPU 用 (11434) gemma4。1問 約55秒 | CPU 用 (11435) gemma4。30分でも終わらず実用外 |
| タグ付け | GPU 用 gemma4。1件 約3秒 | CPU 用 gemma4。1件 約90秒 |
| 埋め込み | CPU 用 embeddinggemma (常時。1チャンク 約0.4秒) | 同左 |

モデル選定の記録 (2026-11-15、同じ質問・同じ資料で比較):
- gemma4:e4b は、資料内で引用された他者の主張と発行元自身の主張を正しく書き分けた
- gpt-oss:latest は、全労連の談話に引用された経団連の主張を全労連の主張として書いた。
  回答規則を足しても直らなかったため、回答生成には使わない

時間帯とモデルは `config.yaml` の `llm` で変更する。接続部分は `llm/client.py` に
まとめてあり、外部APIへ切り替える場合はこのファイルだけを差し替える。
