"""LLM サーバーへの SSH ポート転送を、アプリと一緒に起動・終了する。

config.yaml の tunnel.command に ssh のコマンドを書いたときだけ動く。
パスワードを聞かれると止まってしまうので、鍵認証 (ssh-agent や ~/.ssh/config) を設定しておく。
"""
import atexit
import shlex
import socket
import subprocess
import time
from urllib.parse import urlparse

from config import LLM, TUNNEL


def _port_open(url):
    u = urlparse(url)
    try:
        with socket.create_connection((u.hostname, u.port or 80), timeout=1):
            return True
    except OSError:
        return False


def start():
    """必要なら ssh を起動し、LLM のポートが開くまで待つ。"""
    command = TUNNEL.get("command")
    if not command:
        return
    url = LLM["gpu"]["url"]
    if _port_open(url):
        print(f"ポート転送は既に有効です ({url})")
        return
    args = command if isinstance(command, list) else shlex.split(command, posix=False)
    # -N: コマンドを実行しない / ExitOnForwardFailure: 転送できなければすぐ終わる
    args = [args[0], "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", *args[1:]]
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL)
    atexit.register(proc.terminate)
    deadline = time.monotonic() + TUNNEL.get("wait_seconds", 20)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(f"注意: ssh が終了しました (終了コード {proc.returncode})。LLM に接続できない可能性があります")
            return
        if _port_open(url):
            print(f"ポート転送を開始しました ({url})")
            return
        time.sleep(0.5)
    print(f"注意: {url} がまだ開いていません。ssh の設定を確認してください")
