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
python main.py update           # collect → extract → news → tag → embed をまとめて実行
```

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

- 利用回数は Discord の利用者ごとに数える (`user list` では `discord:<ID>` と表示)。上限変更・停止も `user limit` / `user disable` で行う
- 使えるサーバーの限定、DM の可否、同時生成数は `config.yaml` の `discord` で設定する
- `config.yaml` の `tunnel.command` に ssh のコマンドを書くと、LLM サーバーへのポート転送も一緒に起動する (鍵認証が必要)

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
