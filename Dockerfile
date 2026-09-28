FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY btc5m_bot ./btc5m_bot
COPY config ./config

RUN useradd --create-home --uid 1000 bot && mkdir -p /app/runtime && chown bot /app/runtime
USER bot

ENTRYPOINT ["python", "-m", "btc5m_bot"]
CMD ["run", "--profile", "conservative"]
