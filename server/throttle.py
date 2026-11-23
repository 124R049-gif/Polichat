"""Web 画面のログイン失敗の制限 (Bot には不要なので server/ に置く)。"""
import time
from collections import defaultdict, deque


class LoginThrottle:
    """同じ利用者名・同じ接続元からのログイン失敗が続いたら、しばらく受け付けない。

    キーごとに上限を変えられる ({"user:...": 5, "ip:...": 20} のように渡す)。
    プロキシ経由では全員が同じ接続元に見えるため、接続元の上限は大きめにする。"""

    def __init__(self, window_seconds):
        self.window = window_seconds
        self.failures = defaultdict(deque)

    def _recent(self, key):
        q = self.failures[key]
        while q and q[0] < time.monotonic() - self.window:
            q.popleft()
        return q

    def blocked(self, limits):
        return any(len(self._recent(k)) >= n for k, n in limits.items())

    def fail(self, *keys):
        for k in keys:
            self._recent(k).append(time.monotonic())

    def reset(self, *keys):
        for k in keys:
            self.failures.pop(k, None)
