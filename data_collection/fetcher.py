"""相手サイトに負荷をかけないHTTP取得。

- robots.txt を確認し、禁止されたURLは取得しない
- 同一ホストへのアクセスは一定間隔を空ける
- サイズ上限を超えるファイルは途中で打ち切る
"""
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

from config import CRAWL


class FetchError(Exception):
    pass


class Fetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = CRAWL["user_agent"]
        self._last_access = {}
        self._robots = {}

    def _wait(self, host):
        elapsed = time.monotonic() - self._last_access.get(host, 0)
        if elapsed < CRAWL["delay_seconds"]:
            time.sleep(CRAWL["delay_seconds"] - elapsed)
        self._last_access[host] = time.monotonic()

    def _allowed(self, url):
        if not CRAWL["respect_robots"]:
            return True
        parts = urlparse(url)
        host = parts.netloc
        if host not in self._robots:
            parser = RobotFileParser()
            try:
                self._wait(host)
                res = self.session.get(
                    f"{parts.scheme}://{host}/robots.txt", timeout=CRAWL["timeout_seconds"]
                )
                if res.status_code >= 500:
                    parser.disallow_all = True
                elif res.status_code >= 400:
                    parser.allow_all = True
                else:
                    parser.parse(res.text.splitlines())
            except requests.RequestException:
                parser.disallow_all = True
            self._robots[host] = parser
        return self._robots[host].can_fetch(CRAWL["user_agent"], url)

    def get(self, url):
        """URLを取得し (本文bytes, Content-Type, 最終URL) を返す。失敗時は FetchError。"""
        if not self._allowed(url):
            raise FetchError("robots.txt により取得不可")
        self._wait(urlparse(url).netloc)
        try:
            res = self.session.get(url, timeout=CRAWL["timeout_seconds"], stream=True)
            if res.status_code != 200:
                raise FetchError(f"HTTP {res.status_code}")
            body = bytearray()
            for block in res.iter_content(65536):
                body.extend(block)
                if len(body) > CRAWL["max_bytes"]:
                    raise FetchError("サイズ上限超過")
        except requests.RequestException as e:
            raise FetchError(f"{type(e).__name__}: {e}") from e
        finally:
            if "res" in locals():
                res.close()
        content_type = res.headers.get("Content-Type", "").split(";")[0].strip().lower()
        return bytes(body), content_type, res.url
