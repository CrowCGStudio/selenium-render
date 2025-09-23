# Usa Python slim come base
FROM python:3.11-slim

# Variabili di ambiente
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Aggiorna e installa dipendenze di sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    fonts-liberation \
    libnss3 \
    libxss1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    wget \
    curl \
    unzip \
    libreoffice \
 && rm -rf /var/lib/apt/lists/*

# Crea cartella app
WORKDIR /app

# Copia requirements e installa dipendenze Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia il resto del codice
COPY . .

# Espone la porta
EXPOSE 5000

# Comando di avvio (Gunicorn)
CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app", "--workers=2", "--threads=4", "--timeout=120"]
