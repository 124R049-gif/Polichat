import requests
import urllib.parse

from bs4 import BeautifulSoup
import urllib.parse

def normalize_url(raw_url: str, base_url: str) -> str:
    \"\"\"
    相対URLをベースURLに基づいて絶対URLに正規化します。
    \"\"\"
    if not raw_url: 
        return ""
    
    # すでに絶対URLの場合はそのまま返す
    if raw_url.startswith('http'):
        return raw_url
    
    # urllib.parse.urljoin を使用して正規化
    return urllib.parse.urljoin(base_url, raw_url)

import json

with open("../source/nations.json", "r", encoding="utf-8") as f:
    nations = json.load(f)

with open("../source/prefecture.json", "r", encoding="utf-8") as f:
    prefectures = json.load(f)

with open("../source/city.json", "r", encoding="utf-8") as f:
    cities = json.load(f)

DEFAULT_SCAN_DEPTH = {
    "government": 2,
    "prefecture": 1,
    "city": 0
}

def get_scan_depth(location_type):
    return DEFAULT_SCAN_DEPTH.get(
        location_type,
        0
    )

SCAN_POLICY = {
    "quick": 0,
    "normal": 1,
    "deep": 2
}

def crawl(
    url,
    depth,
    max_depth
):

    if depth > max_depth:
        return

    html = download(url)

    links = get_links(html)

    for link in links:

        crawl(
            link,
            depth + 1,
            max_depth
        )

for source in nations:
def normalize_url(raw_url: str, base_url: str) -> str:
    \"\"\"
    相対URLをベースURLに基づいて絶対URLに正規化します。
    \"\"\"
    if not raw_url: # 空のURLを返す
        return ""
    
    # すでに絶対URLの場合はそのまま返す
    if raw_url.startswith('http'):
        return raw_url
    
    # urllib.parse.urljoin を使用して正規化
    return urllib.parse.urljoin(base_url, raw_url)


    depth = get_scan_depth(
        source["type"]
    )

    crawl(
        source["url"],
    for link in links:
        # URLを正規化する
        normalized_link = normalize_url(link, url)
        if normalized_link:
            crawl(
                normalized_link,
                depth + 1,
                max_depth
            )

        0,
        depth
    )

def get_links(html):

    for link in links:
        normalized_link = normalize_url(link, url)
        if normalized_link:
            crawl(
                normalized_link,
                depth + 1,
                max_depth
            )

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    links = []

    for a in soup.find_all("a", href=True):
        links.append(a["href"])

    return links

def download(url):
    res = requests.get(
        url,
        timeout=10,
        headers={
            "User-Agent": "Mozilla/5.0"
        }
    )
    return res.text

def scrape_article(url):

    print("URL:", url)

    try:
        res = requests.get(
            url,
            timeout=10,
            headers={
                "User-Agent": "Mozilla/5.0"
            }
        )

        print("HTTP:", res.status_code)

        soup = BeautifulSoup(
            res.text,
            "html.parser"
        )

        paragraphs = soup.find_all("p")

        print("pタグ数:", len(paragraphs))

        text = "\n".join(
            p.get_text(strip=True)
            for p in paragraphs
        )

        print("本文文字数:", len(text))

        return text

    except Exception as e:
        print("エラー:", e)
        return ""