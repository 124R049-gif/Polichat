# Discord Bot (と定時更新) を動かすイメージ。大学のサーバー (x86_64) と Raspberry Pi (64bit OS の arm64) の両方で作れる。
# LLM はこのイメージに含めない。学科の GPU サーバーの Ollama に、直接または SSH のポート転送でつなぐ。
FROM python:3.12-slim

# tzdata: 利用回数の日付の切り替えと定時更新 (05:00) を日本時間にするため
# openssh-client: config.yaml の tunnel.command (または環境変数 TUNNEL_COMMAND) で LLM サーバーへ転送するとき
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata openssh-client \
    && rm -rf /var/lib/apt/lists/*
ENV TZ=Asia/Tokyo \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# root で動かさない。ホストの data/ を書けるよう、UID はホストの利用者に合わせる (Raspberry Pi の既定は 1000)
ARG UID=1000
RUN useradd --uid ${UID} --create-home app && mkdir -p data && chown -R app /app/data
USER app

CMD ["python", "main.py", "bot"]
