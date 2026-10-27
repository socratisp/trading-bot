FROM python:3.12-slim

# Node is only needed for the Claude Code CLI (claude -p).
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && apt-get purge -y npm && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 bot
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
COPY config.yaml .

# Claude login and the trading journal live on volumes, so rebuilds keep them.
ENV CLAUDE_CONFIG_DIR=/home/bot/.claude \
    BOT_CONFIG=/app/config.yaml \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data /home/bot/.claude && chown -R bot:bot /data /home/bot
USER bot

CMD ["python", "-m", "bot.main"]
