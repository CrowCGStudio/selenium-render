# Usa una base leggera con Python 3.11
FROM python:3.11-slim

# Evita prompt interattivi di debconf (fix per Term/ReadLine.pm mancante)
ENV DEBIAN_FRONTEND=noninteractive

# Imposta variabili di ambiente di default (non sensibili)
ENV PYTHONUNBUFFERED=1 \
    DOWNLOAD_DIR=/app/downloads \
    PORT=5000

# Installa dipendenze di sistema (Chromium + Chromedriver + OpenSSL)
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    fonts-liberation \
    openssl \
    && rm -rf /var/lib/apt/lists/*

# Imposta la working dir
WORKDIR /app

# Copia prima requirements.txt per sfruttare la cache
COPY requirements.txt .

# Installa pacchetti Python
RUN pip install --no-cache-dir -r requirements.txt

# Copia il resto del progetto
COPY . .

# Espone la porta del servizio
EXPOSE ${PORT}

# Comando di avvio con Gunicorn (2 worker, 4 thread ciascuno → buono per IO-bound come Selenium)
CMD ["gunicorn", "--workers=2", "--threads=4", "-b", "0.0.0.0:5000", "app:app"]
