"""ソースの入口 (RSSフィード、新着一覧ページ) から文書のURLを見つける。"""
import re
import time
from urllib.parse import urldefrag, urljoin, urlparse

import feedparser
from bs4 import BeautifulSoup

ATTACHMENT_EXTS = (".pdf", ".docx")
_SECOND_LEVEL = {"go", "or", "co", "ne", "ac", "gr", "lg", "ed"}


def site_key(url):
    """組織を表すドメイン部分を返す (www.mhlw.go.jp -> mhlw.go.jp)。"""
    labels = urlparse(url).netloc.lower().split(":")[0].split(".")
    if len(labels) >= 3 and labels[-1] == "jp" and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _clean(url, base):
    return urldefrag(urljoin(base, url.strip()))[0]


def from_feed(body, entry_url):
    """RSS/Atom から (url, title, published_date) を取り出す。"""
    items = []
    for e in feedparser.parse(body).entries:
        link = e.get("link")
        if not link:
            continue
        parsed = e.get("published_parsed") or e.get("updated_parsed")
        date = time.strftime("%Y-%m-%d", parsed) if parsed else None
        items.append((_clean(link, entry_url), e.get("title", "").strip(), date))
    return items


def from_html(body, entry_url, include):
    """一覧ページから include (正規表現) に合うリンクを取り出す。"""
    pattern = re.compile(include)
    soup = BeautifulSoup(body, "html.parser")
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = _clean(a["href"], entry_url)
        if url in seen or not pattern.search(url):
            continue
        seen.add(url)
        items.append((url, a.get_text(" ", strip=True), None))
    return items


def discover(fetcher, source):
    """ソースの全入口を調べ、同じ組織のドメインにある文書URLだけを返す。"""
    key = site_key(source["url"])
    items, seen = [], set()
    for entry in source["entries"]:
        body, _, final_url = fetcher.get(entry["url"])
        if entry["type"] == "feed":
            found = from_feed(body, final_url)
        else:
            found = from_html(body, final_url, entry["include"])
        for url, title, date in found:
            if url.startswith("http") and site_key(url) == key and url not in seen:
                seen.add(url)
                items.append((url, title, date))
    return items


def find_attachments(body, page_url):
    """HTMLページ内の PDF/DOCX へのリンクを (url, リンク文字列) で返す。"""
    key = site_key(page_url)
    soup = BeautifulSoup(body, "html.parser")
    for tag in soup(["nav", "header", "footer", "aside"]):  # 全ページ共通のリンクを除く
        tag.decompose()
    found, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = _clean(a["href"], page_url)
        if (url in seen or site_key(url) != key
                or not urlparse(url).path.lower().endswith(ATTACHMENT_EXTS)):
            continue
        seen.add(url)
        found.append((url, a.get_text(" ", strip=True)))
    return found
