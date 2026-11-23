# 日本社会政治監視機構

省庁・政党・労働組合・経済団体が公開する一次資料を収集・構造化し、
それを根拠に LLM が情勢分析・文書解説・議論を行うためのプラットフォーム。
副機能として、トピックごとに少人数 (3〜6人) が非同期で意見交換する場を提供する。

## 必要なもの

- Python 3.12 (`pip install -r requirements.txt`)
- [Ollama](https://ollama.com/) と次のモデル。`config.yaml` の `llm` で接続先とモデルを変えられる
  - 回答・タグ付け: `gemma4:e4b` (GPU 推奨。CPU では回答生成が実用にならない)
  - 埋め込み: `embeddinggemma`
- 収集した資料のデータベース (`data/`) はリポジトリに含まない。下の手順で各自収集する

## はじめかた

```
pip install -r requirements.txt
python main.py collect          # 資料を収集 (相手サイトの robots.txt を守り、2秒間隔で取得)
python main.py extract          # 本文抽出
python main.py tag              # タグ付け
python main.py embed            # 埋め込み
python main.py user add 名前    # 画面を使う利用者を登録
python main.py serve            # http://127.0.0.1:8000/ を開いてログイン
```

## 使い方

```
pip install -r requirements.txt
python main.py collect          # 全ソースから新着文書を取得 (1ソース最大20件)
python main.py collect -s mhlw -n 5
python main.py extract          # 取得済み文書から本文を抽出しチャンク化
python main.py status           # 収集状況
python main.py search 介護 報酬  # 本文検索
python main.py news             # ニュース見出し (話題検知用)
python main.py tag              # LLMでタグ付け (要 Ollama 接続)
python main.py embed            # 埋め込み作成 (要 Ollama 接続)
python main.py ask "質問"       # 資料に基づき出典付きで回答 (要 Ollama 接続)
python main.py serve            # チャット画面を起動 → http://127.0.0.1:8000/
python main.py events           # タイムライン用に、資料から政府・各党の出来事と予定を取り出す (要 Ollama 接続。--redo でやり直し)
python main.py events --reassign   # 党規約 (source/party_rules.json) や党名を変えたとき、取り出し済みの出来事の扱いだけ付け直す
python main.py update           # collect → extract → news → tag → events → embed をまとめて実行
python main.py collect -s kokkai --since 2026-01-01 -n 1000   # 国会会議録を遡って取る (1会議2秒。1年分で約1時間)
```

## タイムライン

画面上部の「タイムライン」で、政府と各党が **これまで何をしてきたか・今何をしているか・これから何をしようとしているか** を
同じ時間軸で見られる。行が政府と各党、横軸が時間で、赤い「今日」の線の左が実績、右が予定・目標。

- 印の形で立場、塗りで状態を表す。形: ○ 与党 / □ 野党 / ◇ 政府 (出来事の時点で与党だったかで決める)。
  塗り: 塗りつぶし=実施済み / 半分=進行中 / 白抜き=予定・目標。期間のあるもの (審議中・計画期間など) は帯、予定の帯は点線
- 党ごとに色を付ける (自民=青、共産=赤、参政=オレンジ など。`source/actors.json` の `color` で変えられる)。
  与党の期間は `source/actors.json` の `ruling`
- 時期の書かれていない予定は右端の「時期未定の予定」に並ぶ。予定の時期を過ぎたものは点線の下線で示す (実施されたかは未確認)
- 分野 (資料のタグ付けと同じ分類表) で絞り込むと、同じテーマについて政府と各党の動きを並べて比べられる。
  分野は「分野」ボタンで開くパネルに、社会保障・くらし・経済・国の運営のグループごとに件数付きで並ぶ。
  複数選ぶとどれかに当てはまるものを出し、選んだ分野はボタンの横にタグで出る (× で外せる)
- 各行の名前の下の「非表示」で行を隠せる。隠した行は年表の一番下に細くまとまり、「表示」で元の位置に戻る
  (隠した行はブラウザに覚えておく。一覧表示では上の「表示する行」ボタンで切り替える)
- 項目を押すと、根拠の原文の一節と出典 URL を表示する
- 初めは今日の前後だけを表示する。左へスクロールすると、前の時期を6か月ずつ読み足してさかのぼれる
  (収集した出来事のいちばん古い時期まで。過去の資料が増えれば、そのまま明治までさかのぼれる)
- 右のサイドパネルは、右端のアイコン (詳細・質問・履歴) で開く。開いているアイコンをもう一度押すと閉じる。項目を押すと詳細と根拠の原文を、
  「質問」では表示中の範囲 (期間・分野・行・選択中の項目) の出来事について質問できる。やりとりはパネルの中でスクロールし、
  閉じても残る。狭い画面では下からせり上がるパネルになる。
- 項目をクリックすると、根拠の原文を該当部分 (黄色) とその前後で表示する (「前を読む」「続きを読む」で広げられる)。
  ダブルクリックすると、LLM がその出来事の資料を読んで説明する (質問の回数を1回使う)
- 会話の履歴: チャットとタイムラインの質問は、利用者ごとにサーバーに保存される (最後まで回答できたものだけ)。
  画面上部の「履歴」またはサイドパネルの「履歴」タブから開き直して続きを質問でき、削除もできる
  質問に近い出来事を選び、その根拠の原文を資料として、チャットと同じく出典付きで答える (回数はチャットと共通)。
  項目の「この件について質問する」で、その出来事についての質問を用意する
- 幅の狭い画面 (スマホ) では、月ごとの一覧に切り替わる
- 出来事は `python main.py events` (定時更新にも含む) で、資料から LLM が取り出す。主体は資料の発行元で決め、
  LLM が抜き出した原文の一節が本文にないものは捨てる
- 国会会議録の議員発言のうち、党規約で意見表明を制限していない党のものは「議員個人の発言」とし、既定では表示しない
  (「議員個人の発言も表示」で出せる)
- 主体の一覧と、会議録の会派名との対応は `source/actors.json`で変える

## 定時更新

`config.yaml` の `schedule.times` (既定は毎日 05:00) に、資料の収集から埋め込みまでを自動で実行する。

- `serve` / `bot` の起動中は、裏で自動的に動く (`times: []` にすると止まる)
- それらを起動しないときは `python main.py update --loop` で待機させるか、Windows のタスクスケジューラに `python main.py update` を登録する
- LLM に接続できないときはタグ付け・埋め込みだけ見送り、次回に回す。1つの手順が失敗しても残りは続ける
- 同時に2つは実行しない (`data/update.lock`)。結果の要約は `data/update.log` に追記する
- `python main.py update --step collect --step extract` のように手順を選んで実行もできる

## Discord Bot として公開する

Bot は Discord へ外向きに接続するだけなので、ポート開放・VPN・HTTPS の用意は要らない。

1. [Discord Developer Portal](https://discord.com/developers/applications) でアプリを作り、Bot のトークンを発行する
2. トークンを `.env` に書く (リポジトリには含まれない)
   ```
   DISCORD_BOT_TOKEN=発行したトークン
   ```
3. `python main.py bot` で起動し、表示される招待URLから Bot をサーバーに追加する
4. Discord で `/ask 質問` と送ると、資料に基づく出典付きの回答が返る。`/status` で残り回数を確認できる

### URL の解説

記事・X の投稿・YouTube の URL を渡すと、要点、誰が何を言っているか、収集した一次資料との照らし合わせを出典付きで返す。

- `@Bot名 URL` (質問を添えるなら `@Bot名 URL 質問`)。メンションした投稿に返信する。URL のない `@Bot名 質問` は `/ask` と同じ
- 投稿を右クリック → アプリ → **URL を解説** (他の人が貼った URL にも使える)
- `/explain URL [質問]`
- 読めるもの: 一般の記事 (Yahoo!ニュース・毎日・読売・日経などの無料部分。会員限定記事は読める部分だけと明記する)、PDF、
  X の個別の投稿 (本文のみ)、YouTube (字幕があれば字幕、なければ題名と概要欄。`yt-dlp` が要る)
- 利用者が求めた1件をその場で読むだけで、本文は保存しない (robots.txt は見ない)。内部ネットワークのアドレスや標準以外のポートは読まない
- 解説も1回と数える。URL を読めなかったときは数えない
- URL を貼った投稿に **返信して** `@Bot名` と送ったときに返信先の URL を読むには、Developer Portal の Bot 設定で
  **Message Content Intent** を有効にし、`config.yaml` の `discord.message_content_intent` を `true` にする (他の使い方には不要)

- 利用回数は Discord の利用者ごとに数える (`user list` では `discord:<ID>` と表示)。上限変更・停止も `user limit` / `user disable` で行う
- 使えるサーバーの限定、DM の可否、同時生成数は `config.yaml` の `discord` で設定する
- `config.yaml` の `tunnel.command` に ssh のコマンドを書くと、LLM サーバーへのポート転送も一緒に起動する (鍵認証が必要)

### Docker で動かす (大学のサーバー・Raspberry Pi)

Bot 自体は軽く、LLM は学科の GPU サーバーの Ollama を使うので、性能の低い機械でよい。
Raspberry Pi は **64bit の OS** (arm64) にする (32bit では PyMuPDF が入らない)。

1. このフォルダ (`data/` 以外) をサーバーに置き、同じ場所に `.env` を作る
   ```
   DISCORD_BOT_TOKEN=発行したトークン
   # LLM へ直接届くなら
   LLM_GPU_URL=http://<GPUサーバー>:11434
   LLM_CPU_URL=http://<GPUサーバー>:11435
   LLM_EMBED_URL=http://<GPUサーバー>:11435
   # 届かないなら代わりに SSH の転送をコンテナ内で張る (compose.yaml の ~/.ssh の行を有効にする)
   # TUNNEL_COMMAND=ssh -L 11434:127.0.0.1:11434 -L 11435:127.0.0.1:11435 <LLMサーバー>
   ```
2. 資料の DB を `data/crawler.db` にコピーする (または起動後に `docker compose run --rm discord-bot python main.py update`)
3. `docker compose up -d --build` で起動。`docker compose logs -f` で招待 URL とログを見る

- 毎日 05:00 (日本時間) の定時更新もコンテナの中で動く。**同じ DB を手元の PC と同時に動かさない**
- `config.yaml` はコンテナに読み取り専用で渡しているので、変えたら `docker compose restart`
- ホストの利用者の UID が 1000 でなければ `UID=$(id -u) docker compose up -d --build` で作る (`data/` に書けるように)
- YouTube 側の変更で字幕が読めなくなったら、`docker compose build --no-cache` で `yt-dlp` を新しくする

## Slack Bot として使う

Socket Mode で Slack へ外向きに接続するだけなので、公開 URL・ポート開放・HTTPS の用意は要らない。
質問の受け方は **@メンションと DM だけ** (意図しない回答を避けるため。`/ask` は既定で無効)。

1. [Slack API](https://api.slack.com/apps) で **Create New App → From an app manifest** を選び、ワークスペースを選んで次を貼る
   ```yaml
   display_information:
     name: 監視機構
   features:
     app_home:
       messages_tab_enabled: true              # DM を使うとき
       messages_tab_read_only_enabled: false
     bot_user:
       display_name: 監視機構
       always_online: true
     slash_commands:
       - command: /status
         description: モデル・資料数・本日の残り回数
         should_escape: false
   oauth_config:
     scopes:
       bot:
         - app_mentions:read    # メンションを受け取る
         - chat:write           # 回答の投稿と書き換え
         - commands             # /status
         - channels:history     # 公開チャンネルのスレッドで、続きの質問の前のやりとりを読む
         - groups:history       # 同じく非公開チャンネル
         - im:history           # DM を受け取る・読む
   settings:
     event_subscriptions:
       bot_events:
         - app_mention
         - message.im
     interactivity:
       is_enabled: false
     socket_mode_enabled: true
   ```
   手で作る場合も同じ: **Socket Mode** を有効にし、**Event Subscriptions** で bot events に `app_mention` と `message.im`、
   **OAuth & Permissions** で上の scope、**Slash Commands** に `/status` (Socket Mode なので Request URL は不要)、
   DM を使うなら **App Home** の Messages Tab と「Allow users to send … messages from the messages tab」を有効にする
2. **Basic Information → App-Level Tokens** で scope `connections:write` のトークンを作る (`xapp-` で始まる)
3. **Install App** でワークスペースに入れ、**Bot User OAuth Token** (`xoxb-` で始まる) を控える
4. 2つのトークンを `.env` に書く (リポジトリにも `config.yaml` にも書かない)
   ```
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_APP_TOKEN=xapp-...
   ```
5. `python main.py slack` で起動し、使うチャンネルで `/invite @監視機構` と Bot を追加する
6. `@監視機構 介護報酬の改定で何が変わったか` と投稿すると、その投稿のスレッドに回答が返る (生成中は書き換えで表示)。
   回答の下に番号付きの出典と本日の残り回数が付く。続きの質問は同じスレッドで再び `@監視機構 …` と書く
   (スレッド内のやりとりを踏まえて答える。メンションのない返信には反応しない)。`/status` は本人にだけ表示される

- 利用回数は Slack の利用者ごとに数える (`user list` では `slack:<ワークスペースID>:<利用者ID>` と表示)。上限変更・停止は `user limit` / `user disable`
- 断るとき (上限・停止中・使えない場所・生成中の連投) は本人にだけ見える返事 (ephemeral) にし、回数は使わない
- 使えるワークスペース・チャンネルの限定、DM の可否 (既定は不可)、`/ask` の有効化は `config.yaml` の `slack` で設定する。
  `slash_ask: true` にするときは Slack 側にも `/ask` を登録し、DM で使うなら scope `im:write` も足す
- 同時生成数は `llm.max_concurrent` (Web・Discord と合計)。超えた分は「順番待ちです」と表示して待つ

## exe にまとめる

```
pip install -r requirements.txt pyinstaller
pyinstaller nihon-kanshi.spec      # → dist/nihon-kanshi.exe
```

- exe をダブルクリックすると Bot が起動する。他のコマンドは `nihon-kanshi.exe collect` のように使う
- 初回起動時に `config.yaml`・`source/` を exe の隣に書き出す (以後は上書きしない。新しい exe で増えたファイルだけ追加する)。画面は exe に同梱したものを使う。資料の DB は exe の隣の `data/` に置く
- exe を更新するときは exe だけ差し替える。`config.yaml` の新しい設定項目は README や同梱版を見て手で足す
- `.env` も exe の隣に置く

## 利用者と利用回数

画面を使うにはログインが必要。利用者は管理者がコマンドで登録する (自由な新規登録はない)。

```
python main.py user add 名前               # 登録 (パスワードは対話で入力。10文字以上)
python main.py user add 名前 --limit 50    # 1日の上限を個別に指定して登録
python main.py user list                   # 利用者と本日の利用回数
python main.py user limit 名前 --limit 50  # 1日の上限を変更 (0 で既定値に戻す)
python main.py user passwd 名前            # パスワード変更 (ログイン中のセッションは無効になる)
python main.py user disable 名前           # 利用停止 (enable で再開、delete で削除)
```

- 1日の質問回数の既定値は `config.yaml` の `auth.daily_limit` (20回)。日付はサーバーの現地時刻で切り替わる
- サーバー側の失敗 (LLMに接続できないなど) で回答できなかった質問は回数に数えない。利用者が途中で中止した質問は数える
- 利用できる時間帯の制限はない (`llm.gpu_hours.always: true`)
- 他のPCから使わせるときは `python main.py serve --host 0.0.0.0` で起動する。インターネット経由で公開するときは HTTPS にし、`auth.cookie_secure` を `true` にする

設定は `config.yaml`、収集対象は `source/sources.json`、タグの分類表は `source/taxonomy.json`。

## ソースの追加

`source/sources.json` に1件追加する。

```json
{
    "id": "mhlw",
    "name": "厚生労働省",
    "org_type": "government",
    "url": "https://www.mhlw.go.jp/",
    "entries": [{"type": "feed", "url": "https://www.mhlw.go.jp/stf/news.rdf"}]
}
```

- `org_type`: `government` / `public` / `party` / `union` / `business` / `association` / `local` / `diet`
- `entries[].type`: `feed` (RSS/Atom) または `html` (新着一覧ページ)
- `html` の場合は `include` に文書URLの正規表現を書く (例: `"/policy/\\d{4}/\\d+\\.html$"`)
- `"enabled": false` で巡回から外す

`national.json` / `prefecture.json` / `city.json` は機関の一覧 (今後の追加候補) で、収集には使わない。

詳細は [docs/architecture.md](docs/architecture.md)、進捗は [docs/TODO.md](docs/TODO.md)。

## ライセンス

MIT License ([LICENSE](LICENSE))。収集した資料の著作権は各発行元にある。
