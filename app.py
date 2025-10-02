import os
import time
import threading
import requests
import mimetypes
import json
import subprocess
import signal
import psutil
from urllib.parse import quote, unquote
from flask import Flask, request, jsonify, send_from_directory

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Cartella per i download
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/app/downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Webhook Zapier: ENV con fallback al valore del progetto originale
WEBHOOK_DEST = os.environ.get(
    "WEBHOOK_DEST",
    "https://hooks.zapier.com/hooks/catch/24277770/umrp8cs/"
)

# Gemini API
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_UPLOAD_ENDPOINT = "https://generativelanguage.googleapis.com/upload/v1beta/files"

# Whitelist CPV (prime due cifre valide)
CPV_WHITELIST = {"30","32","48","51","64","71","72","73","79","80","85","90","92","98"}

app = Flask(__name__)

# ----------------------------
# Logging risorse e segnali
# ----------------------------

def log_resources(tag=""):
    """Logga l'uso attuale di memoria e CPU del processo."""
    try:
        proc = psutil.Process(os.getpid())
        mem_info = proc.memory_info()
        cpu_percent = proc.cpu_percent(interval=0.1)
        print(f"[RESOURCES] {tag} PID={proc.pid}, "
              f"RSS={mem_info.rss/1024/1024:.1f} MB, CPU%={cpu_percent}",
              flush=True)
    except Exception as e:
        print(f"[WARN] Impossibile leggere risorse: {e}", flush=True)

def handle_sigterm(signum, frame):
    """Gestione SIGTERM per loggare risorse al momento dello shutdown."""
    log_resources("SIGTERM ricevuto - stato finale")

signal.signal(signal.SIGTERM, handle_sigterm)

# ----------------------------
# Funzioni di supporto
# ----------------------------

def build_driver():
    log_resources("Prima di avviare Chrome")

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

    try:
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": DOWNLOAD_DIR}
        )
    except Exception as e:
        print(f"[WARN] setDownloadBehavior fallito: {e}", flush=True)

    log_resources("Dopo avvio Chrome")
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

# ----------------------------
# Selenium scrape
# ----------------------------

def scrape_page(url: str):
    driver = build_driver()
    results = []

    try:
        print(f"[INFO] Navigo: {url}", flush=True)
        driver.get(url)

        wait = WebDriverWait(driver, 20)
        wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".list-detail-view.sortable")))
        list_items = driver.find_elements(By.CSS_SELECTOR, ".list-detail-view.sortable")

        if not list_items:
            print("[INFO] Nessun allegato trovato su questa pagina.", flush=True)
            results = []
        else:
            print(f"[INFO] Trovati {len(list_items)} possibili allegati.", flush=True)
            for index, item in enumerate(list_items, start=1):
                try:
                    link = item.find_element(By.CSS_SELECTOR, 'a[data-qa="attachment"]')
                    file_label = (link.text or "").strip()
                    if "." in file_label:
                        file_label = file_label.rsplit(".", 1)[0]

                    href = link.get_attribute("href")
                    print(f"[INFO] ({index}) Allegato trovato: {file_label} ({href})", flush=True)

                    before = set(os.listdir(DOWNLOAD_DIR))

                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", link)
                    time.sleep(0.3)
                    try:
                        link.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", link)

                    new_file = None
                    for _ in range(60):
                        time.sleep(0.5)
                        after = set(os.listdir(DOWNLOAD_DIR))
                        delta = list(after - before)
                        if not delta:
                            continue
                        if any(f.endswith(".crdownload") for f in delta):
                            continue
                        ready = [f for f in delta if not f.endswith(".tmp")]
                        if ready:
                            new_file = ready[0]
                            print(f"[INFO] → File scaricato: {new_file}", flush=True)
                            break

                    result = {"index": index, "label": file_label, "href": href}
                    if new_file:
                        result["saved_file"] = new_file
                    else:
                        print(f"[WARN] Nessun file scaricato per allegato {index}: {file_label}", flush=True)
                    results.append(result)

                except Exception as e:
                    print(f"[ERRORE] Problema con allegato {index}: {e}", flush=True)
                    continue

    except Exception as e:
        print(f"[ERRORE] Problema generale con la pagina {url}: {e}", flush=True)
        results = []

    finally:
        driver.quit()

    return results

# ----------------------------
# Processo asincrono
# ----------------------------

def process_async(annunci, webhook_url, base_url, gemini_api_key=None):
    for annuncio in annunci:
        log_resources(f"Elaborazione annuncio {annuncio.get('ID annuncio e anno')}")

        url = annuncio.get("link ai documenti dell'annuncio")
        if not url:
            continue

        page_results = scrape_page(url)

        for r in page_results:
            saved = r.get("saved_file")
            if saved:
                file_path = os.path.join(DOWNLOAD_DIR, saved)
                file_path = sbusta_p7m(file_path)
                file_path = convert_odt_to_pdf(file_path)
                saved = os.path.basename(file_path)

                encoded_name = quote(saved)
                r["file_url"] = f"{base_url}/files/{encoded_name}"

                if gemini_api_key:
                    try:
                        file_obj = upload_to_gemini(file_path, saved, gemini_api_key)
                        r["gemini_uri"] = file_obj.get("uri")
                        r["gemini_name"] = file_obj.get("name")
                        r["gemini_mime"] = file_obj.get("mimeType")
                        r["gemini_state"] = file_obj.get("state")
                        print(f"[INFO] Upload Gemini completato per {saved} (uri: {r['gemini_uri']})", flush=True)
                    except Exception as e:
                        print(f"[ERRORE] Upload Gemini fallito per {saved}: {e}", flush=True)
                        r["gemini_upload"] = "failed"

        payload = {
            "url": url,
            "announcement": {
                "Position": annuncio.get("Position"),
                "ente promotore": annuncio.get("ente promotore"),
                "ID annuncio e anno": annuncio.get("ID annuncio e anno"),
                "titolo annuncio": annuncio.get("titolo annuncio"),
                "stato gara": annuncio.get("stato gara"),
            },
            "results": page_results,
            "has_attachments": bool(page_results)
        }

        try:
            print(f"[INFO] Invio risultati a Zapier per {url}", flush=True)
            requests.post(webhook_url, json=payload, timeout=30)
        except Exception as e:
            print(f"[ERRORE] Invio webhook fallito per {url}: {e}", flush=True)

# ----------------------------
# Filtro annunci
# ----------------------------

def filter_annunci(annunci):
    print(f"[DEBUG] 🔍 Filtro annunci attivato. Numero annunci in ingresso: {len(annunci)}", flush=True)

    filtrati = []
    driver = build_driver()

    try:
        for annuncio in annunci:
            url = annuncio.get("link ai documenti dell'annuncio")
            if not url:
                continue

            filtro_url = url.rstrip("/1") + "/2" if url.endswith("/1") else url + "/2"
            print(f"[INFO] Controllo filtro CPV su: {filtro_url}", flush=True)

            try:
                driver.get(filtro_url)

                wait = WebDriverWait(driver, 20)
                elem = wait.until(
                    EC.presence_of_element_located((
                        By.CSS_SELECTOR,
                        "div.classification-user-cat-list[data-qa='primary-category'] "
                        "div.classification-user-cat "
                        "span[data-qa='remove-primary-cat-container']"
                    ))
                )

                text_val = elem.text.strip()
                cpv_code = text_val.split(".")[0].strip() if "." in text_val else text_val.strip()
                first_two = cpv_code[:2]

                if first_two in CPV_WHITELIST:
                    print(f"[INFO] ✅ Annuncio mantenuto (CPV {cpv_code}, codice {first_two} in whitelist)", flush=True)
                    filtrati.append(annuncio)
                else:
                    print(f"[INFO] ❌ Annuncio scartato (CPV {cpv_code}, codice {first_two} non in whitelist)", flush=True)

            except Exception as e:
                print(f"[ERRORE] Problema durante il filtraggio di {filtro_url}: {e}", flush=True)
                continue

    finally:
        driver.quit()

    return filtrati

def filtro_e_processa(annunci, webhook_url, base_url, gemini_api_key=None):
    annunci_filtrati = filter_annunci(annunci)
    print(f"[INFO] Dopo filtro restano {len(annunci_filtrati)} annunci.", flush=True)

    if annunci_filtrati:
        process_async(annunci_filtrati, webhook_url, base_url, gemini_api_key)
    else:
        print("[INFO] Nessun annuncio da processare dopo il filtro.", flush=True)

# ----------------------------
# Endpoint Flask
# ----------------------------

@app.route("/health", methods=["GET"])
def health():
    return "ok", 200

@app.route("/list_files", methods=["GET"])
def list_files():
    try:
        files = os.listdir(DOWNLOAD_DIR)
        if files:
            msg = f"[INFO] File presenti ({len(files)}): {files}"
        else:
            msg = "[INFO] Nessun file presente nella cartella di download."
        print(msg, flush=True)
        return msg, 200
    except Exception as e:
        err = f"[ERRORE] list_files: {e}"
        print(err, flush=True)
        return err, 500

@app.route("/files/<path:filename>", methods=["GET"])
def serve_file(filename):
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)

@app.route("/delete_file", methods=["POST"])
def delete_file():
    data = request.get_json(silent=True) or {}
    file_url = data.get("file_url")
    if not file_url:
        return jsonify({"error": "file_url mancante"}), 400

    filename = file_url.split("/files/")[-1]
    filename = unquote(filename)
    file_path = os.path.join(DOWNLOAD_DIR, filename)

    if not os.path.exists(file_path):
        print(f"[ERRORE] delete_file: {filename} non trovato in {DOWNLOAD_DIR}", flush=True)
        return jsonify({"status": "not_found", "file": filename}), 404

    try:
        os.remove(file_path)
        print(f"[INFO] File eliminato: {filename}", flush=True)
        return jsonify({"status": "deleted", "file": filename}), 200
    except Exception as e:
        print(f"[ERRORE] Eliminazione fallita per {filename}: {e}", flush=True)
        return jsonify({"status": "error", "file": filename, "error": str(e)}), 500

@app.route("/scrape_async", methods=["POST"])
def scrape_async():
    data = request.get_json(silent=True) or {}
    urls_str = data.get("urls")
    webhook_url = data.get("webhook_url")
    gemini_api_key = GEMINI_API_KEY

    if not urls_str or not webhook_url:
        return jsonify({"error": "urls e webhook_url sono richiesti"}), 400

    urls = [u.strip() for u in urls_str.split(",") if u.strip()]
    base_url = request.host_url.rstrip("/")

    annunci = [{"link ai documenti dell'annuncio": u} for u in urls]

    threading.Thread(
        target=process_async,
        args=(annunci, webhook_url, base_url, gemini_api_key),
        daemon=True
    ).start()

    return jsonify({"status": "in lavorazione", "urls": urls, "gemini_upload": bool(gemini_api_key)}), 202

@app.route("/ricevi_annunci", methods=["POST"])
def ricevi_annunci():
    data = request.get_json(silent=True) or {}
    print("[INFO] Payload ricevuto:", data, flush=True)

    captured_lists = data.get("task", {}).get("capturedLists", {})
    annunci = []
    if captured_lists:
        annunci = next(iter(captured_lists.values()), [])

    if not annunci:
        return jsonify({"error": "Nessun annuncio trovato nel payload"}), 400

    for a in annunci:
        id_annuncio = a.get("ID annuncio e anno")
        if id_annuncio:
            a["link ai documenti dell'annuncio"] = (
                f"https://start.toscana.it/tendering/tenders/{id_annuncio.replace('/', '-')}/view/detail/1"
            )

    print(f"[INFO] Estratti {len(annunci)} annunci da processare.", flush=True)

    base_url = request.host_url.rstrip("/")
    gemini_api_key = GEMINI_API_KEY

    urls = [a.get("link ai documenti dell'annuncio") for a in annunci if a.get("link ai documenti dell'annuncio")]
    response = {"status": "in lavorazione", "urls": urls, "gemini_upload": bool(gemini_api_key)}

    threading.Thread(
        target=filtro_e_processa,
        args=(annunci, WEBHOOK_DEST, base_url, gemini_api_key),
        daemon=True
    ).start()

    return jsonify(response), 202

# ----------------------------
# Main
# ----------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log_resources("Avvio applicazione")
    app.run(host="0.0.0.0", port=port)
