FROM python:3.11-slim

# Evita prompt interattivi
ENV DEBIAN_FRONTEND=noninteractive

# Aggiorno e installo Chromium + Chromedriver + librerie necessarie
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium chromium-driver \
    libnss3 libgdk-pixbuf-2.0-0 libgtk-3-0 libatk-bridge2.0-0 \
    libdrm2 libxkbcommon0 libgbm1 libasound2 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Variabili utili (usate in app.py)
ENV CHROME_BINARY=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver
ENV DOWNLOAD_DIR=/app/downloads
RUN mkdir -p /app/downloads

# Installo dipendenze Python
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copio il codice
COPY . /app

# Avvio gunicorn (usa $PORT fornita da Render), 1 worker basta per iniziare
CMD ["/bin/sh", "-c", "gunicorn app:app -b 0.0.0.0:${PORT:-5000} --workers 1 --threads 4 --timeout 300"]
