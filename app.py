import os
import time
import json
import mimetypes
import subprocess
import threading
from urllib.parse import quote, unquote

import requests
from flask import Flask, request, jsonify, send_from_directory

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# =========================
# Config
# =========================
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/app/downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

WEBHOOK_DEST = os.environ.get("WEBHOOK_DEST")  # <-- workflow n8n di destinazione (OBBLIGATORIO per invio risultati)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")  # opzionale: se assente, salta upload a Gemini
GEMINI_UPLOAD_ENDPOINT = "https://generativelanguage.googleapis.com/upload/v1beta/files"

app = Flask(__name__)


# =========================
# Helpers
# =========================
def build_driver():
    chrome_binary = os.environ.get("CHROME_BINARY", "/usr/bin/chromium")
    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")

    options = Options()
    options.binary_location = chrome_binary
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    service = Service(chromedriver_path)
    driver = webdriver.Chrome(service=service, options=options)

    # abilita download automatici
    try:
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": DOWNLOAD_DIR}
        )
    except Exception as e:
        print(f"[WARN] setDownloadBehavior fallito: {e}", flush=True)

    return driver


def guess_mime(filename: str) -> str:
    mt, _ = mimetypes.guess_type(filename)
    return mt or "application/octet-stream"


def sbusta_p7m(file_path: str) -> str:
    if not file_path.lower().endswith(".p7m"):
        return file_path
    output_path = file_path.rsplit(".p7m", 1)[0]
    try:
        subprocess.run(
            ["openssl", "smime", "-verify",
             "-in", file_path, "-inform", "DER",
             "-noverify", "-out", output_path],
            check=True
        )
        print(f"[INFO] Sbustato {file_path} → {output_path}", flush=True)
        os.remove(file_path)
        return output_path
    except Exception as e:
        print(f"[ERRORE] Sbustamento fallito per {file_path}: {e}", flush=True)
        return file_path


def convert_odt_to_pdf(file_path: str) -> str:
    if not file_path.lower().endswith(".odt"):
        return file_path
    output_dir = os.path.dirname(file_path)
    try:
        subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "pdf", "--outdir", output_dir, file_path],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        pdf_path = file_path.rsplit(".", 1)[0] + ".pdf"
        if os.path.exists(pdf_path):
            os.remove(file_path)
            print(f"[INFO] Convertito {file_path} → {pdf_path}", flush=True)
            return pdf_path
    except Exception as e:
        print(f"[ERRORE] Conversione ODT fallita per {file_path}: {e}", flush=True)
    return file_path


def upload_to_gemini(file_path: str, filename: str, api_key: str) -> dict:
    mime_type = guess_mime(filename)
    url = GEMINI_UPLOAD_ENDPOINT + "?uploadType=multipart"
    headers = {"x-goog-api-key": api_key}
    metadata = {"file": {"display_name": filename, "mime_type": mime_type}}

    with open(file_path, "rb") as f:
        files = {
            "metadata": ("metadata.json", json.dumps(metadata), "application/json"),
            "file": (filename, f, mime_type),
        }
        r = requests.post(url, headers=headers, files=files, timeout=120)
        r.raise_for_status()
        return r.json().get("file", {}) or {}


def scrape_attachments(url: str):
    """Apre la pagina dell'annuncio e scarica tutti gli allegati."""
    driver = build_driver()
    results = []
    try:
        print(f"[INFO] Navigo: {url}", flush=True)
        driver.get(url)

        wait = WebDriverWait(driver, 20)
        wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".list-detail-view.sortable")))
        list_items = driver.find_elements(By.CSS_SELECTOR, ".list-detail-view.sortable")

        if not list_items:
            print("[INFO] Nessun allegato trovato.", flush=True)
            return results

        print(f"[INFO] Trovati {len(list_items)} elementi potenziali allegati.", flush=True)

        for idx, item in enumerate(list_items, start=1):
            try:
                link = item.find_element(By.CSS_SELECTOR, 'a[data-qa="attachment"]')
                label = (link.text or "").strip()
                href = link.get_attribute("href")

                before = set(os.listdir(DOWNLOAD_DIR))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", link)
                time.sleep(0.25)
                try:
                    link.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", link)

                new_file = None
                for _ in range(60):  # fino a ~30s
                    time.sleep(0.5)
                    after = set(os.listdir(DOWNLOAD_DIR))
                    delta = list(after - before)
                    if not delta:
                        continue
                    # ignora .crdownload / .tmp
                    ready = [f for f in delta if not (f.endswith(".crdownload") or f.endswith(".tmp"))]
                    if ready:
                        new_file = ready[0]
                        break

                result = {"index": idx, "label": label, "href": href}
                if new_file:
                    result["saved_file"] = new_file
                    print(f"[INFO] Scaricato: {new_file}", flush=True)
                else:
                    print(f"[WARN] Nessun file scaricato per allegato {idx}", flush=True)

                results.append(result)
            except Exception as e:
                print(f"[WARN] Errore su allegato {idx}: {e}", flush=True)
    except Exception as e:
        print(f"[ERRORE] Scrape fallito: {e}", flush=True)
    finally:
        driver.quit()

    return results


def process_single_announcement(annuncio: dict, base_url: str):
    """Flusso completo: scarica allegati, sbusta/converti, upload a Gemini, POST a n8n."""
    url = annuncio.get("link ai documenti dell'annuncio")
    if not url:
        print("[ERRORE] Annuncio senza link ai documenti, stop.", flush=True)
        return

    print(f"[INFO] 🔍 Analisi annuncio: {annuncio.get('titolo annuncio','(senza titolo)')}", flush=True)
    page_results = scrape_attachments(url)

    out = []
    for r in page_results:
        saved = r.get("saved_file")
        if not saved:
            continue

        file_path = os.path.join(DOWNLOAD_DIR, saved)
        file_path = sbusta_p7m(file_path)
        file_path = convert_odt_to_pdf(file_path)
        final_name = os.path.basename(file_path)
        file_url = f"{base_url}/files/{quote(final_name)}"

        entry = {
            "filename": final_name,
            "url": file_url,
            "href": r.get("href"),
            "label": r.get("label"),
        }

        if GEMINI_API_KEY:
            try:
                gf = upload_to_gemini(file_path, final_name, GEMINI_API_KEY)
                entry.update({
                    "gemini_uri": gf.get("uri"),
                    "mime_type": gf.get("mimeType"),
                    "gemini_state": gf.get("state"),
                })
                print(f"[INFO] Upload Gemini ok: {final_name}", flush=True)
            except Exception as e:
                print(f"[ERRORE] Upload Gemini fallito per {final_name}: {e}", flush=True)
                entry["gemini_upload"] = "failed"

        out.append(entry)

    payload = {
        "announcement": annuncio,
        "attachments": out,
        "has_attachments": bool(out),
        "source": "analizza_annuncio",
    }

    # POST a n8n (fine processo)
    if WEBHOOK_DEST:
        try:
            requests.post(WEBHOOK_DEST, json=payload, timeout=30)
            print(f"[INFO] ✅ Inviato a WEBHOOK_DEST", flush=True)
        except Exception as e:
            print(f"[ERRORE] Invio a WEBHOOK_DEST fallito: {e}", flush=True)
    else:
        print("[WARN] WEBHOOK_DEST non impostata: risultati non inviati.", flush=True)


# =========================
# Endpoints
# =========================
@app.route("/health", methods=["GET"])
def health():
    return "ok", 200


@app.route("/upload_report", methods=["POST"])
def upload_report():
    """
    Riceve un file HTML da n8n e lo salva in /downloads,
    rendendolo accessibile come pagina web reale (/files/report.html).
    """
    content_type = request.headers.get("Content-Type", "")
    file_path = os.path.join(DOWNLOAD_DIR, "report.html")

    # 1) multipart/form-data (field name: file)
    if "multipart/form-data" in content_type:
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "Nessun file caricato"}), 400
        html_content = f.read().decode("utf-8")

    # 2) raw text/html
    elif "text/html" in content_type or "application/html" in content_type:
        html_content = request.data.decode("utf-8")

    # 3) JSON { html: "<!DOCTYPE html>..." }
    elif "application/json" in content_type:
        data = request.get_json(silent=True) or {}
        html_content = data.get("html") or data.get("content") or ""
    else:
        return jsonify({"error": f"Content-Type non supportato: {content_type}"}), 400

    if not html_content.strip():
        return jsonify({"error": "Contenuto HTML vuoto"}), 400
    if "<html" not in html_content.lower():
        return jsonify({"error": "Il contenuto non sembra HTML valido"}), 400

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    url = f"{request.host_url.rstrip('/')}/files/report.html"
    print(f"[INFO] Report HTML salvato: {url}", flush=True)
    return jsonify({"status": "ok", "file_url": url}), 200


@app.route("/analizza_annuncio", methods=["POST"])
def analizza_annuncio():
    """
    Riceve un SINGOLO annuncio, avvia l'analisi in background
    e (al termine) invia i risultati a WEBHOOK_DEST (n8n).
    """
    data = request.get_json(silent=True) or {}
    annuncio = data.get("announcement")
    if not annuncio:
        return jsonify({"error": "Campo 'announcement' mancante"}), 400

    base_url = request.host_url.rstrip("/")

    threading.Thread(
        target=process_single_announcement,
        args=(annuncio, base_url),
        daemon=True
    ).start()

    # Al browser basta sapere che è partito
    return jsonify({"status": "in lavorazione", "queued": True}), 202


@app.route("/files/<path:filename>", methods=["GET"])
def serve_file(filename):
    # serve inline (gli .html si aprono nel browser)
    return send_from_directory(DOWNLOAD_DIR, filename)


@app.route("/list_files", methods=["GET"])
def list_files():
    try:
        files = sorted(os.listdir(DOWNLOAD_DIR))
        return jsonify({"files": files}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/delete_file", methods=["POST"])
def delete_file():
    data = request.get_json(silent=True) or {}
    file_url = data.get("file_url")
    if not file_url or "/files/" not in file_url:
        return jsonify({"error": "file_url mancante o non valido"}), 400

    filename = unquote(file_url.split("/files/")[-1])
    path = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(path):
        return jsonify({"status": "not_found", "file": filename}), 404

    try:
        os.remove(path)
        print(f"[INFO] File eliminato: {filename}", flush=True)
        return jsonify({"status": "deleted", "file": filename}), 200
    except Exception as e:
        return jsonify({"status": "error", "file": filename, "error": str(e)}), 500


# =========================
# Main
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
