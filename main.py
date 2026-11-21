"""日本社会政治監視機構 コマンドライン入口。

  python main.py collect            全ソースから新着文書を取得
  python main.py collect -s mhlw    特定ソースのみ
  python main.py extract            未処理の文書から本文を抽出
  python main.py status             収集状況を表示
  python main.py search 介護 報酬    本文を検索
  python main.py news               ニュース見出しを取得
  python main.py tag                未分類の文書にLLMでタグ付け
  python main.py embed              未処理のチャンクを埋め込み
  python main.py update             収集〜埋め込みをまとめて実行 (--loop で定時実行)
  python main.py ask "質問"          資料に基づき出典付きで回答
  python main.py serve              チャット画面を起動 (http://127.0.0.1:8000/)
  python main.py bot                Discord Bot を起動 (/ask で質問)
  python main.py user add 名前       利用者を登録 (パスワードは対話で入力)
  python main.py user list           利用者と本日の利用回数
  python main.py user limit 名前 --limit 50   1日の上限を変更 (passwd / disable / enable / delete もある)
"""
import argparse
import sys

from database.db import get_connection, init_db, search_chunks


def cmd_collect(args):
    from data_collection.collector import collect
    collect(source_ids=args.source, limit=args.limit)


def cmd_extract(args):
    from parsing.extract import extract_pending
    init_db()
    from parsing.dates import fill_dates
    for status, n in extract_pending().items():
        print(f"{status}: {n} 件")
    from parsing.quality import mark_excluded
    print(f"公開日を補完: {fill_dates()} 件")
    docs, chunks = mark_excluded()
    print(f"検索対象から除外: 文書 {docs} 件、チャンク {chunks} 件 (名簿・外国語・表など)")


def cmd_status(args):
    conn = get_connection()
    print("ソース別 (文書数 / 抽出済み / 文字数)")
    for r in conn.execute("""
        SELECT s.name, COUNT(d.id) AS n, SUM(d.status = 'extracted') AS ok,
               SUM(d.n_chars) AS chars, s.last_scan
        FROM sources s LEFT JOIN documents d ON d.source_id = s.id
        GROUP BY s.id ORDER BY s.org_type, s.id"""):
        print(f"  {r['name']}: {r['n']} / {r['ok'] or 0} / {r['chars'] or 0}"
              f"  (最終巡回 {r['last_scan'] or '未実施'})")
    print("状態別")
    for r in conn.execute("""SELECT status, file_type, COUNT(*) AS n FROM documents
                             GROUP BY status, file_type ORDER BY status"""):
        print(f"  {r['status']} / {r['file_type'] or '-'}: {r['n']} 件")
    n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    print(f"チャンク総数: {n}")
    conn.close()


def cmd_search(args):
    conn = get_connection()
    for r in search_chunks(conn, " ".join(args.query), args.limit):
        page = f" p.{r['page']}" if r["page"] else ""
        print(f"■ {r['source_name']} / {r['title']}{page} ({r['published_date'] or '日付不明'})")
        print(f"  {r['url']}")
        print("  " + r["text"][:200].replace("\n", " ") + "\n")
    conn.close()


def cmd_news(args):
    from data_collection.rss import run
    run()


def cmd_tag(args):
    from parsing.tagger import tag_pending
    print(f"タグ付け完了: {tag_pending(args.limit)} 件")


def cmd_embed(args):
    from rag.vector_db import embed_pending
    print(f"埋め込み完了: {embed_pending()} チャンク")


def cmd_ask(args):
    from llm.client import endpoint
    from rag.answer import ask, format_source
    _, model, device = endpoint("chat")
    print(f"(モデル {args.model or model} / {device})\n")
    answer, sources = ask(" ".join(args.question), model=args.model)
    print(answer + "\n\n── 参照した資料 ──")
    for n, r in enumerate(sources, 1):
        print(format_source(n, r) + "\n    " + r["url"])


def cmd_update(args):
    import scheduler
    if not args.loop:
        if not scheduler.run_update(args.step):
            sys.exit(1)
        return
    from llm import tunnel
    tunnel.start()
    if scheduler.next_run() is None:
        sys.exit("config.yaml の schedule.times に時刻を書いてください")
    print("定時更新を待機します (Ctrl+C で終了)")
    scheduler.loop()


def cmd_serve(args):
    import uvicorn
    import scheduler
    from llm import tunnel
    from server.app import app
    init_db()
    tunnel.start()
    scheduler.start_background()
    print(f"チャット画面: http://{args.host}:{args.port}/  (Ctrl+C で終了)")
    if args.host != "127.0.0.1":
        print("注意: このPC以外からも接続できます。利用者の登録と、HTTPS での公開を確認してください")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def cmd_bot(args):
    import scheduler
    from bot import discord_bot
    from llm import tunnel
    tunnel.start()
    scheduler.start_background()
    print("Discord Bot を起動します (Ctrl+C で終了)")
    discord_bot.run()


def _ask_password():
    from getpass import getpass
    from server.auth import validate_password
    while True:
        password = getpass("パスワード: ")
        if problem := validate_password(password):
            print(problem)
        elif password != getpass("もう一度: "):
            print("一致しません")
        else:
            return password


def cmd_user(args):
    import sqlite3
    from server import auth
    init_db()
    if args.action == "list":
        users = auth.list_users()
        if not users:
            print("利用者はまだいません (python main.py user add 名前 で登録)")
        for u in users:
            limit = u["daily_limit"] if u["daily_limit"] is not None else f"{auth.AUTH['daily_limit']} (既定)"
            state = " [停止中]" if u["disabled"] else ""
            print(f"  {u['username']}{state}  本日 {u['used_today']} / {limit} 回  登録 {u['created_at']}")
        return
    if not args.name:
        sys.exit("利用者名を指定してください")
    if args.action == "add":
        try:
            auth.add_user(args.name, _ask_password(), args.limit)
        except sqlite3.IntegrityError:
            sys.exit(f"{args.name} は登録済みです")
        print(f"{args.name} を登録しました")
        return
    if args.action == "passwd":
        changed = auth.update_user(args.name, password=_ask_password())
    elif args.action == "delete":
        changed = auth.delete_user(args.name)
    elif args.action == "limit":
        # 0 以下を指定すると既定値 (config.yaml) に戻す
        changed = auth.update_user(args.name, daily_limit=args.limit if args.limit and args.limit > 0 else None)
    else:  # disable / enable
        changed = auth.update_user(args.name, disabled=int(args.action == "disable"))
    print("変更しました" if changed else f"{args.name} は登録されていません")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="日本社会政治監視機構")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("collect", help="新着文書を取得")
    p.add_argument("-s", "--source", action="append", help="ソースID (複数指定可)")
    p.add_argument("-n", "--limit", type=int, help="1ソースあたりの新規取得数")
    p.set_defaults(func=cmd_collect)

    sub.add_parser("extract", help="本文を抽出").set_defaults(func=cmd_extract)
    sub.add_parser("status", help="収集状況").set_defaults(func=cmd_status)

    p = sub.add_parser("search", help="本文を検索")
    p.add_argument("query", nargs="+")
    p.add_argument("-n", "--limit", type=int, default=5)
    p.set_defaults(func=cmd_search)

    sub.add_parser("news", help="ニュース見出しを取得").set_defaults(func=cmd_news)

    p = sub.add_parser("tag", help="LLMでタグ付け")
    p.add_argument("-n", "--limit", type=int, help="処理する文書数")
    p.set_defaults(func=cmd_tag)

    sub.add_parser("embed", help="埋め込みを作成").set_defaults(func=cmd_embed)

    p = sub.add_parser("update", help="収集〜埋め込みをまとめて実行 (--loop で定時実行)")
    p.add_argument("--loop", action="store_true", help="config.yaml の schedule.times に繰り返し実行")
    p.add_argument("--step", action="append", choices=["collect", "extract", "news", "tag", "embed"],
                   help="この手順だけ実行 (複数指定可。既定は schedule.steps)")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("ask", help="資料に基づいて質問に回答")
    p.add_argument("question", nargs="+")
    p.add_argument("-m", "--model", help="設定と別のモデルで試す")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("serve", help="チャット画面を起動")
    p.add_argument("--host", default="127.0.0.1",
                   help="待ち受けるアドレス (既定はこのPCのみ。他から使うときは 0.0.0.0 など)")
    p.add_argument("-p", "--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    sub.add_parser("bot", help="Discord Bot を起動").set_defaults(func=cmd_bot)

    p = sub.add_parser("user", help="利用者の管理")
    p.add_argument("action", choices=["add", "list", "passwd", "limit", "disable", "enable", "delete"])
    p.add_argument("name", nargs="?", help="利用者名")
    p.add_argument("--limit", type=int, help="1日の質問回数の上限 (add / limit。limit で 0 なら既定値に戻す)")
    p.set_defaults(func=cmd_user)

    # exe をダブルクリックしたとき (引数なし) は Bot を起動する
    frozen = getattr(sys, "frozen", False)
    args = parser.parse_args(sys.argv[1:] or (["bot"] if frozen else None))
    init_db()
    try:
        args.func(args)
    except Exception as e:
        from llm.client import LLMError
        if not isinstance(e, LLMError):
            raise
        sys.exit(f"エラー: {e}")


if __name__ == "__main__":
    if getattr(sys, "frozen", False) and len(sys.argv) == 1:
        # ダブルクリックで起動したとき、エラーを読む前にウィンドウが閉じないようにする
        try:
            main()
        except SystemExit as e:
            if e.code:
                print(e.code)
                input("Enter キーで終了します")
        except Exception:
            import traceback
            traceback.print_exc()
            input("Enter キーで終了します")
    else:
        main()
