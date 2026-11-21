"""利用者の認証と、1日あたりの質問回数の制限。

- 利用者は管理者がコマンドで登録する (`python main.py user add 名前`)。自由な新規登録はない
- パスワードは scrypt (標準ライブラリ) でソルト付きハッシュにして保存する
- ログインするとランダムなトークンを Cookie に入れ、DBにはそのハッシュだけを保存する
- 質問1回ごとに当日の回数を数え、上限を超えたら受け付けない。日付はサーバーの現地時刻で切り替わる
"""
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta

from config import AUTH
from database.db import get_connection

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
# 存在しない利用者でも照合と同じ時間をかけ、登録の有無を応答時間から推測させない
_DUMMY_HASH = None


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        _, salt, digest = stored.split("$")
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
    except ValueError:
        return False
    return hmac.compare_digest(actual.hex(), digest)


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


# ---- 利用者の管理 (コマンドから使う) ----

def validate_password(password):
    """パスワードの条件を満たさなければ理由を返す。"""
    if len(password) < AUTH["min_password_length"]:
        return f"パスワードは {AUTH['min_password_length']} 文字以上にしてください"
    return None


def add_user(username, password, daily_limit=None):
    conn = get_connection()
    conn.execute("INSERT INTO users (username, password_hash, daily_limit) VALUES (?, ?, ?)",
                 (username, hash_password(password), daily_limit))
    conn.commit()
    conn.close()


def external_user(username):
    """Bot などの外部サービス経由の利用者 (例 "discord:123") を返す。なければ作る。

    パスワードのハッシュを持たないので、チャット画面にはログインできない。
    回数の上限・停止は通常の利用者と同じく `python main.py user ...` で変えられる。"""
    conn = get_connection()
    conn.execute("INSERT OR IGNORE INTO users (username, password_hash) VALUES (?, '!')", (username,))
    conn.commit()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return user


def update_user(username, **fields):
    """password / daily_limit / disabled を変更する。変更した利用者がいれば True。"""
    if "password" in fields:
        fields["password_hash"] = hash_password(fields.pop("password"))
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn = get_connection()
    cur = conn.execute(f"UPDATE users SET {sets} WHERE username = ?", [*fields.values(), username])
    if fields.get("disabled") or "password_hash" in fields:
        # 停止・パスワード変更のときは、ログイン中のセッションも無効にする
        conn.execute("DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE username = ?)",
                     (username,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def delete_user(username):
    """利用者を削除する (セッションと利用回数も消える)。削除できれば True。"""
    conn = get_connection()
    cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def list_users():
    conn = get_connection()
    rows = conn.execute("""
        SELECT u.username, u.daily_limit, u.disabled, u.created_at,
               COALESCE((SELECT count FROM usage WHERE user_id = u.id AND day = ?), 0) AS used_today
        FROM users u ORDER BY u.id""", (today(),)).fetchall()
    conn.close()
    return rows


# ---- ログインとセッション ----

def authenticate(username, password):
    """正しければ利用者の行を返す。違えば None。"""
    global _DUMMY_HASH
    conn = get_connection()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if user is None:
        _DUMMY_HASH = _DUMMY_HASH or hash_password(secrets.token_hex(8))
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user["password_hash"]) or user["disabled"]:
        return None
    return user


def create_session(user_id):
    token = secrets.token_urlsafe(32)
    expires = datetime.now() + timedelta(days=AUTH["session_days"])
    conn = get_connection()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (datetime.now().isoformat(),))
    conn.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                 (_token_hash(token), user_id, expires.isoformat()))
    conn.commit()
    conn.close()
    return token


def session_user(token):
    """有効なセッションの利用者を返す。なければ None。"""
    if not token:
        return None
    conn = get_connection()
    user = conn.execute("""
        SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled = 0""",
                        (_token_hash(token), datetime.now().isoformat())).fetchone()
    conn.close()
    return user


def delete_session(token):
    if not token:
        return
    conn = get_connection()
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
    conn.commit()
    conn.close()


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


# ---- 1日あたりの回数制限 ----

def today():
    return date.today().isoformat()


def daily_limit(user):
    return user["daily_limit"] if user["daily_limit"] is not None else AUTH["daily_limit"]


def used_today(user_id):
    conn = get_connection()
    row = conn.execute("SELECT count FROM usage WHERE user_id = ? AND day = ?",
                       (user_id, today())).fetchone()
    conn.close()
    return row["count"] if row else 0


def consume(user):
    """当日の回数を1つ使う。上限に達していれば使わずに False を返す。"""
    conn = get_connection()
    day = today()
    conn.execute("INSERT OR IGNORE INTO usage (user_id, day, count) VALUES (?, ?, 0)", (user["id"], day))
    cur = conn.execute("UPDATE usage SET count = count + 1 WHERE user_id = ? AND day = ? AND count < ?",
                       (user["id"], day, daily_limit(user)))
    conn.commit()
    conn.close()
    return cur.rowcount == 1


def refund(user_id, day):
    """回答を生成できなかった質問の分を戻す。"""
    conn = get_connection()
    conn.execute("UPDATE usage SET count = count - 1 WHERE user_id = ? AND day = ? AND count > 0",
                 (user_id, day))
    conn.commit()
    conn.close()
