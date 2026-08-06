FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb procps curl ca-certificates \
    libgtk-3-0 libgtk-3-common libdbus-glib-1-2 \
    libxt6 libx11-6 libx11-xcb1 libxcb1 libxcomposite1 \
    libxcursor1 libxdamage1 libxext6 libxfixes3 libxi6 libxrandr2 libxrender1 libxtst6 \
    libasound2 libpango-1.0-0 libpangocairo-1.0-0 libatk1.0-0 libatk-bridge2.0-0 \
    libgdk-pixbuf-2.0-0 libdrm2 libgbm1 libxshmfence1 libnss3 libnspr4 \
    fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN camoufox fetch

COPY main.py .

EXPOSE 10001

CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x720x24 &>/dev/null & export DISPLAY=:99 && uvicorn main:app --host 0.0.0.0 --port 10001"]
