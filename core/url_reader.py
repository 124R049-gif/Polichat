"""利用者が Bot に貼った URL (記事・X の投稿・動画) の中身を読む。

資料の収集 (data_collection/) とは別物で、利用者が求めた1件をその場で読むだけ (リンクのプレビューと同じ扱い)。
そのため robots.txt は見ないが、読んだ本文は DB に保存せず、回答の材料にしたら捨てる。

利用者が送った URL をサーバーが取りに行くので、内部への接続 (SSRF) を防ぐ。
- http / https の標準ポートだけ。接続先の IP がインターネット上のもの (is_global) でなければ断る
- 転送 (リダイレクト) は自分で1回ずつたどり、転送先も毎回確かめる
- 実際につながった相手の IP も、本文を読む前にもう一度確かめる (DNS の書き換え対策)
- 大きさ・時間に上限を設ける
"""
import ipaddress
import json
import re
import socket
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from config import URL_READER
from parsing.extract import extract_html, extract_pdf

MAX_BYTES = int(URL_READER.get("max_bytes") or 10 * 1024 * 1024)
TIMEOUT = float(URL_READER.get("timeout_seconds") or 20)
MAX_CHARS = int(URL_READER.get("max_chars") or 6000)
USER_AGENT = URL_READER.get("user_agent") or "Mozilla/5.0 (compatible; KanshiLinkReader/1.0)"
MAX_REDIRECTS = 5

# 文末の句読点・括弧は URL に含めない
_URL = re.compile(r"https?://[^\s<>|「」『』（）]+", re.I)
_TRAILING = ".,;:!?。、，．！？)]}>'\""
_X_HOSTS = {"x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com", "www.x.com", "www.twitter.com"}
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}


class ReadError(Exception):
    """URL を読めなかった理由 (利用者に見せる文)。"""


def find_urls(text):
    """文中の URL を順に返す (重複なし)。Discord の <URL> (プレビューなし) の形にも対応する。"""
    urls = []
    for m in _URL.finditer(text or ""):
        url = m.group(0).rstrip(_TRAILING)
        if url not in urls:
            urls.append(url)
    return urls


# ---- 安全な取得 ----

def _check_url(url):
    """取りに行ってよい URL か確かめ、だめなら ReadError。"""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ReadError("http または https の URL を送ってください。")
    if parts.username or parts.password:
        raise ReadError("ユーザー名やパスワードを含む URL は読みません。")
    try:
        port = parts.port
    except ValueError:
        raise ReadError("URL の形が正しくありません。") from None
    if port not in (None, 80, 443):
        raise ReadError("標準のポート (80 / 443) 以外の URL は読みません。")
    try:
        infos = socket.getaddrinfo(parts.hostname, port or (443 if parts.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise ReadError("そのサイトが見つかりません (URL を確認してください)。") from None
    for info in infos:
        _check_ip(info[4][0])


def _check_ip(address):
    ip = ipaddress.ip_address(address.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        raise ReadError("内部ネットワークのアドレスは読みません。")


def _peer_ip(res):
    """実際につながった相手の IP (分からなければ None)。"""
    for conn in (getattr(res.raw, "connection", None), getattr(res.raw, "_connection", None)):
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                return sock.getpeername()[0]
            except OSError:
                return None
    return None


def fetch(url, accept=None):
    """URL を取得し (本文 bytes, Content-Type, 最終 URL) を返す。失敗は ReadError。"""
    session = requests.Session()
    session.trust_env = False   # 環境のプロキシ設定を経由させない (確かめた IP と別の所へつながないため)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.5"}
    if accept:
        headers["Accept"] = accept
    for _ in range(MAX_REDIRECTS + 1):
        _check_url(url)
        try:
            res = session.get(url, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=False)
        except requests.RequestException as e:
            raise ReadError(f"サイトに接続できませんでした ({type(e).__name__})。") from e
        try:
            peer = _peer_ip(res)
            if peer:
                _check_ip(peer)
            if res.is_redirect and res.headers.get("Location"):
                url = urljoin(url, res.headers["Location"])
                continue
            if res.status_code in (401, 402, 403):
                raise ReadError(f"サイトに読み込みを断られました (HTTP {res.status_code})。"
                                "会員限定の記事や、自動での閲覧を断っているサイトは読めません。")
            if res.status_code != 200:
                raise ReadError(f"ページを取得できませんでした (HTTP {res.status_code})。")
            body = bytearray()
            for block in res.iter_content(65536):
                body.extend(block)
                if len(body) > MAX_BYTES:
                    raise ReadError(f"ファイルが大きすぎます ({MAX_BYTES // 1024 // 1024}MB まで)。")
            content_type = res.headers.get("Content-Type", "").split(";")[0].strip().lower()
            return bytes(body), content_type, url
        except requests.RequestException as e:
            raise ReadError(f"ページの読み込みが途中で失敗しました ({type(e).__name__})。") from e
        finally:
            res.close()
    raise ReadError("転送 (リダイレクト) が多すぎます。")


def fetch_json(url):
    body, _, _ = fetch(url, accept="application/json")
    try:
        return json.loads(body)
    except ValueError:
        raise ReadError("サイトの応答を読めませんでした。") from None


# ---- 種類ごとの読み方 ----

def _page(url, kind, title=None, site=None, author=None, published=None, text="", note=None):
    text = (text or "").strip()
    truncated = len(text) > MAX_CHARS
    return {"url": url, "kind": kind, "title": (title or "").strip() or None,
            "site": (site or "").strip() or None, "author": (author or "").strip() or None,
            "published": (published or "").strip() or None,
            "text": text[:MAX_CHARS], "truncated": truncated, "note": note}


def _meta(soup, *names):
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def _json_ld_article(soup):
    """JSON-LD の NewsArticle などから (本文, 公開日時, 著者) を探す。"""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            types = item.get("@type")
            types = types if isinstance(types, list) else [types]
            if not any(t in ("NewsArticle", "Article", "ReportageNewsArticle", "BlogPosting") for t in types):
                continue
            author = item.get("author")
            if isinstance(author, list):
                author = author[0] if author else None
            if isinstance(author, dict):
                author = author.get("name")
            return (item.get("articleBody") or "", item.get("datePublished"),
                    author if isinstance(author, str) else None)
    return "", None, None


def read_html(body, url):
    soup = BeautifulSoup(body, "html.parser")
    title = _meta(soup, "og:title", "twitter:title")
    site = _meta(soup, "og:site_name")
    description = _meta(soup, "og:description", "description")
    published = _meta(soup, "article:published_time", "pubdate", "date")
    ld_body, ld_date, ld_author = _json_ld_article(soup)
    text, html_title = extract_html(body)
    # JSON-LD の本文があり十分長ければ、ナビゲーションなどが混ざらないそちらを使う
    if len(ld_body) >= 200:
        text = ld_body
    if len(text) < 200 and description:
        text = f"{description}\n{text}".strip()
    note = None
    if len(text) < 200:
        note = "本文をほとんど読めませんでした (会員限定・画面上で組み立てるサイトなど)。題名と概要だけで解説します"
    elif _PAYWALL.search(text):
        note = "会員限定の記事のため、無料で読める部分だけに基づく"
    return _page(url, "article", title or html_title, site, ld_author, published or ld_date, text, note)


_PAYWALL = re.compile(r"会員限定(?:記事|の記事)|有料会員(?:限定|の方)|この記事は有料|続きは(?:有料|会員)|残り\d+文字")
_YAHOO_ARTICLE = re.compile(r"https://news\.yahoo\.co\.jp/articles/[0-9a-f]+")


def read_x(url):
    """X (Twitter) の投稿。ログインなしで読める公式の埋め込み用 API (oEmbed) を使う。"""
    if not re.search(r"/status(?:es)?/\d+", urlparse(url).path):
        raise ReadError("X は個別の投稿の URL (…/status/数字) だけ読めます。")
    canonical = re.sub(r"^https?://[^/]+", "https://twitter.com", url.split("?")[0])
    try:
        data = fetch_json("https://publish.twitter.com/oembed?omit_script=1&dnt=true&lang=ja&url="
                          + requests.utils.quote(canonical, safe=""))
    except ReadError as e:
        raise ReadError("X の投稿を読めませんでした (削除・非公開・鍵アカウントの投稿は読めません)。") from e
    soup = BeautifulSoup(data.get("html", ""), "html.parser")
    quote = soup.find("blockquote") or soup
    paragraphs = [p.get_text("\n", strip=True) for p in quote.find_all("p")]
    links = quote.find_all("a")
    date = links[-1].get_text(strip=True) if links else None
    text = "\n".join(paragraphs) or quote.get_text("\n", strip=True)
    return _page(url, "post", f"{data.get('author_name', '')} の投稿", "X", data.get("author_name"),
                 date, text, "X の投稿本文のみ (返信・引用元・画像の中の文字は読めません)")


def _youtube_id(url):
    parts = urlparse(url)
    if parts.hostname == "youtu.be":
        return parts.path.lstrip("/").split("/")[0] or None
    if parts.path == "/watch":
        return (parse_qs(parts.query).get("v") or [None])[0]
    m = re.match(r"/(?:shorts|live|embed)/([\w-]+)", parts.path)
    return m.group(1) if m else None


def _subtitle_text(info):
    """yt-dlp の情報から日本語 (なければ英語) の字幕を読む。(本文, 自動生成か) または (None, None)。"""
    for key, auto in (("subtitles", False), ("automatic_captions", True)):
        tracks = info.get(key) or {}
        for lang in ("ja", "ja-JP", "en", "en-US"):
            for fmt in tracks.get(lang) or []:
                if fmt.get("ext") != "json3" or not fmt.get("url"):
                    continue
                try:
                    data = fetch_json(fmt["url"])
                except ReadError:
                    continue
                lines = ("".join(s.get("utf8", "") for s in ev.get("segs") or [])
                         for ev in data.get("events") or [])
                text = re.sub(r"\n{2,}", "\n", "\n".join(l.strip() for l in lines if l.strip()))
                if text:
                    return text, auto
    return None, None


def read_youtube(url):
    """YouTube の動画。字幕があれば字幕を、なければ題名と概要欄を使う。動画そのものはダウンロードしない。"""
    video_id = _youtube_id(url)
    if not video_id:
        raise ReadError("YouTube は個別の動画の URL だけ読めます。")
    watch = f"https://www.youtube.com/watch?v={video_id}"
    try:
        import yt_dlp
    except ImportError:
        yt_dlp = None
    if yt_dlp:
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                                   "noplaylist": True, "socket_timeout": TIMEOUT}) as ydl:
                info = ydl.extract_info(watch, download=False)
        except Exception as e:   # yt-dlp は失敗の種類が多い。最後は oEmbed で題名だけでも読む
            print(f"yt-dlp で読めませんでした ({watch}): {type(e).__name__}: {str(e)[:200]}")
            info = None
        if info:
            subtitle, auto = _subtitle_text(info)
            date = info.get("upload_date") or ""
            if len(date) == 8:
                date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
            description = (info.get("description") or "").strip()
            if subtitle:
                text = f"【概要欄】\n{description[:1000]}\n\n【字幕】\n{subtitle}"
                note = "自動生成の字幕に基づく (聞き取りの誤りを含むことがある)" if auto else "字幕に基づく"
            else:
                text = f"【概要欄】\n{description}"
                note = "字幕がないため、題名と概要欄だけに基づく (動画の中で話している内容は分からない)"
            return _page(watch, "video", info.get("title"), "YouTube", info.get("channel") or info.get("uploader"),
                         date, text, note)
    data = fetch_json("https://www.youtube.com/oembed?format=json&url=" + requests.utils.quote(watch, safe=""))
    return _page(watch, "video", data.get("title"), "YouTube", data.get("author_name"), None, "",
                 "題名しか読めなかったため、題名だけに基づく (動画の内容は分からない)")


def read(url):
    """URL を読み、次の dict を返す。読めなければ ReadError。

    url, kind (article / pdf / post / video), title, site, author, published,
    text (MAX_CHARS 文字まで), truncated (切り詰めたか), note (読み方の注記。なければ None)"""
    host = (urlparse(url).hostname or "").lower()
    if host in _X_HOSTS:
        return read_x(url)
    if host in _YOUTUBE_HOSTS:
        return read_youtube(url)
    body, content_type, final_url = fetch(url)
    if content_type == "application/pdf" or body[:5] == b"%PDF-":
        try:
            pages = extract_pdf(body)
        except Exception as e:
            raise ReadError(f"PDF を読めませんでした ({type(e).__name__})。") from e
        text = "\n".join(pages)
        note = "PDF の文字を読めませんでした (スキャン画像の PDF)" if len(text) < 50 else None
        return _page(final_url, "pdf", None, urlparse(final_url).hostname, None, None, text, note)
    if content_type in ("text/html", "application/xhtml+xml", "") or body.lstrip()[:1] == b"<":
        # Yahoo!ニュースの「ピックアップ」は要約だけなので、リンク先の記事本体を読む
        if re.match(r"https://news\.yahoo\.co\.jp/pickup/", final_url):
            m = _YAHOO_ARTICLE.search(body.decode("utf-8", "replace"))
            if m:
                try:
                    return read_html(fetch(m.group(0))[0], m.group(0))
                except ReadError:
                    pass
        return read_html(body, final_url)
    if content_type.startswith("text/"):
        return _page(final_url, "article", None, urlparse(final_url).hostname, None, None,
                     body.decode("utf-8", "replace"))
    raise ReadError(f"この形式 ({content_type}) は読めません。記事のページか PDF の URL を送ってください。")
