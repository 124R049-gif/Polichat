"""Ollama への接続。時刻に応じて GPU 用と CPU 用を使い分ける。"""
import json
import os
from datetime import datetime, time

import requests

from config import LLM


class LLMError(Exception):
    pass


def gpu_allowed(now=None):
    """共用サーバーの GPU を使ってよい時間帯か。

    環境変数 FORCE_GPU=1 で時間帯に関係なく GPU を使う。gpu.sh などで GPU が空いていると
    確かめたときだけ使う。"""
    if os.environ.get("FORCE_GPU") == "1" or LLM["gpu_hours"].get("always"):
        return True
    now = now or datetime.now()
    if now.strftime("%a").lower() in LLM["gpu_hours"].get("free_days", []):
        return True
    now = now.time()
    start = time.fromisoformat(LLM["gpu_hours"]["start"])
    end = time.fromisoformat(LLM["gpu_hours"]["end"])
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # 日付をまたぐ時間帯


def endpoint(role):
    """role ('chat' / 'tag') に使う (URL, モデル名, 'gpu'|'cpu') を返す。"""
    device = "gpu" if gpu_allowed() else "cpu"
    return LLM[device]["url"], LLM[device][f"{role}_model"], device


def _post(url, path, payload):
    try:
        res = requests.post(url + path, json=payload, timeout=LLM["timeout_seconds"])
    except requests.RequestException as e:
        raise LLMError(f"Ollama に接続できない ({url}): {type(e).__name__}。"
                       "SSHのポート転送を確認") from e
    if res.status_code != 200:
        raise LLMError(f"Ollama エラー {res.status_code}: {res.text[:300]}")
    return res.json()


def _chat_request(role, messages, model, stream):
    url, default_model, _ = endpoint(role)
    return url, {
        "model": model or default_model,
        "messages": messages,
        "stream": stream,
        "options": {"temperature": 0, "num_ctx": LLM["num_ctx"]},
    }


def chat(role, messages, schema=None, model=None):
    """応答本文を返す。schema (JSON Schema) を渡すとその形式のJSON文字列になる。"""
    url, payload = _chat_request(role, messages, model, stream=False)
    if schema:
        payload["format"] = schema
    return _post(url, "/api/chat", payload)["message"]["content"]


def chat_stream(role, messages, model=None):
    """応答本文を、生成された順に少しずつ返す (画面に逐次表示するため)。"""
    url, payload = _chat_request(role, messages, model, stream=True)
    try:
        with requests.post(url + "/api/chat", json=payload, stream=True,
                           timeout=LLM["timeout_seconds"]) as res:
            if res.status_code != 200:
                raise LLMError(f"Ollama エラー {res.status_code}: {res.text[:300]}")
            for line in res.iter_lines():
                if line:
                    text = json.loads(line).get("message", {}).get("content")
                    if text:
                        yield text
    except requests.RequestException as e:
        raise LLMError(f"Ollama に接続できない ({url}): {type(e).__name__}。"
                       "SSHのポート転送を確認") from e


def embed(texts):
    """テキストのリストを埋め込みベクトルのリストにする。"""
    data = _post(LLM["embed"]["url"], "/api/embed",
                 {"model": LLM["embed"]["model"], "input": texts})
    return data["embeddings"]
