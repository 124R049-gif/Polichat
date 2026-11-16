"""ニュースRSSの見出し収集。

一次資料ではないので分析の根拠には使わず、いま何が話題かを知るために使う。
記事本文は取得しない (見出しとRSSの要約のみ保存する)。
"""
import feedparser

from config import CRAWL, NEWS_RSS
from database.db import get_connection


def run():
    conn = get_connection()
    added = 0
    for url in NEWS_RSS:
        feed = feedparser.parse(url, agent=CRAWL["user_agent"])
        source = feed.feed.get("title", url)
        for entry in feed.entries:
            cur = conn.execute(
                """INSERT OR IGNORE INTO articles (title, content, url, source, published_date)
                   VALUES (?, ?, ?, ?, ?)""",
                (entry.get("title", ""), entry.get("summary", ""), entry.get("link"),
                 source, entry.get("published", "")),
            )
            added += cur.rowcount
        print(f"{source}: {len(feed.entries)} 件")
    conn.commit()
    conn.close()
    print(f"新規 {added} 件")
