import os
import threading
import requests
from urllib.parse import unquote
from flask import Flask, request, jsonify

# ──────────────────────────────────────────────
# 🔧 Configurazioni principali
# ──────────────────────────────────────────────

app = Flask(__name__)

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/app/downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

WEBHOOK_DEST = os.environ.get("WEBHOOK_DEST")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# ──────────────────────────────────────────────
# 🩺 Endpoint di health check
# ──────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


# ──────────────────────────────────────────────
# 📥 Endpoint principale per analisi annuncio
# ──────────────────────────────────────────────

@app.route("/analizza_annuncio", methods=["POST"])
def analizza_annuncio():
    """
    Riceve un annuncio con allegati, li scarica, li carica su Gemini
    e infine invia i risultati a n8n tramite WEBHOOK_DEST.
    """
    data = request.get_json(force=True, silent=True)
    announcement = data.get("announcement", {})
    attachments = []

    links = announcement.get("link ai documenti dell'annuncio")
    if not links:
        return jsonify({"status": "no_links"}), 200

    if isinstance(links, str):
        links = [links]

    # scarica e carica i file su Gemini in thread separato
    thread = threading.Thread(
        target=process_attachments,
        args=(announcement, links)
    )
    thread.start()

    return jsonify({"status": "processing"}), 200


# ──────────────────────────────────────────────
# ⚙️ Funzione asincrona per scaricare e caricare allegati
# ──────────────────────────────────────────────

def process_attachments(announcement, links):
    results = []
    for url in links:
        try:
            filename = os.path.basename(unquote(url.split("/")[-1])) or "file.pdf"
            filepath = os.path.join(DOWNLOAD_DIR, filename)

            # scarica il file
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(filepath, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)

            # carica su Gemini
            with open(filepath, "rb") as f:
                upload_resp = requests.post(
                    "https://generativelanguage.googleapis.com/v1beta/files?key=" + GEMINI_API_KEY,
                    files={"file": (filename, f, "application/pdf")},
                    timeout=60
                )
            upload_data = upload_resp.json()
            gemini_uri = upload_data.get("file", {}).get("uri")

            results.append({
                "filename": filename,
                "url": f"https://analizza-annuncio.onrender.com/files/{filename}",
                "gemini_uri": gemini_uri,
                "mime_type": "application/pdf",
                "gemini_state": upload_data.get("file", {}).get("state")
            })

        except Exception as e:
            print(f"[ERRORE] Durante il processamento di {url}: {e}", flush=True)

    payload = {
        "announcement": announcement,
        "attachments": results,
        "has_attachments": bool(results),
        "source": "analizza_annuncio"
    }

    if WEBHOOK_DEST:
        try:
            requests.post(WEBHOOK_DEST, json=payload, timeout=30)
            print(f"[INFO] Webhook inviato a n8n con {len(results)} allegati", flush=True)
        except Exception as e:
            print(f"[ERRORE] Invio webhook fallito: {e}", flush=True)
    else:
        print("[ATTENZIONE] WEBHOOK_DEST non configurato", flush=True)


# ──────────────────────────────────────────────
# 🗑️ Endpoint per cancellare uno o più file
# ──────────────────────────────────────────────

@app.route("/delete_file", methods=["POST"])
def delete_file():
    """
    Elimina uno o più file dalla cartella di download.
    Accetta:
      - {"filename": "file.pdf"}                → singolo file
      - {"filenames": ["file1.pdf","file2.pdf"]} → lista di file
    Restituisce JSON con elenco dei file eliminati o mancanti.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}

        # normalizza in lista
        filenames = data.get("filenames")
        if not filenames:
            single = data.get("filename")
            if single:
                filenames = [single]
            else:
                return jsonify({"error": "nessun filename o filenames fornito"}), 400

        deleted, not_found = [], []

        for name in filenames:
            path = os.path.join(DOWNLOAD_DIR, name)
            if os.path.exists(path):
                try:
                    os.remove(path)
                    deleted.append(name)
                    print(f"[INFO] 🗑️ File eliminato: {name}", flush=True)
                except Exception as e:
                    print(f"[ERRORE] durante eliminazione di {name}: {e}", flush=True)
            else:
                not_found.append(name)

        return jsonify({
            "status": "ok",
            "deleted": deleted,
            "not_found": not_found
        }), 200

    except Exception as e:
        print(f"[ERRORE] delete_file: {e}", flush=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# ──────────────────────────────────────────────
# 🚀 Avvio server Flask (solo per debug locale)
# ──────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
