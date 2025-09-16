import os
import time
import requests
import logging
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

# Configurazione logging
logging.basicConfig(
    level=logging.INFO,  # DEBUG se vuoi più dettagli
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/app/app.log", mode="a")
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

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

    driver.execute_cdp_cmd(
        "Page.setDownloadBehavior",
        {"behavior": "allow", "downloadPath": DOWNLOAD_DIR}
    )
    return driver

def scrape_page(url: str):
    driver = build_driver()
    results = []
    try:
        logger.info(f"Navigo su: {url}")
        driver.get(url)

        wait = WebDriverWait(driver, 20)
        wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".list-detail-view.sortable")))
        list_items = driver.find_elements(By.CSS_SELECTOR, ".list-detail-view.sortable")

        if not list_items:
            logger.warning("Nessun allegato trovato")
            results.append({"message": "Nessun allegato trovato."})
        else:
            logger.info(f"Trovati {len(list_items)} possibili allegati")
            for index, item in enumerate(list_items, start=1):
                try:
                    link = item.find_element(By.CSS_SELECTOR, 'a[data-qa="attachment"]')
                    file_label = (link.text or "").strip()
                    if "." in file_label:
                        file_label = file_label.rsplit(".", 1)[0]
                    href = link.get_attribute("href")

                    before = set(os.listdir(DOWNLOAD_DIR))
                    driver.execute_script("arguments[0].scrollIntoView(true);", link)
                    time.sleep(0.5)
                    driver.execute_script("arguments[0].click();", link)

                    new_file = None
                    for _ in range(20):
                        time.sleep(0.5)
                        after = set(os.listdir(DOWNLOAD_DIR))
                        created = list(after - before)
                        created = [f for f in created if not f.endswith(".crdownload") and not f.endswith(".tmp")]
                        if created:
                            new_file = created[0]
                            break

                    result = {"index": index, "label": file_label, "href": href}
                    if new_file:
                        result["saved_file"] = new_file
                        logger.info(f"Scaricato file: {new_file}")
                    else:
                        logger.warning(f"Nessun file scaricato per {file_label}")
                    results.append(result)

                except Exception as e:
                    logger.error(f"Errore allegato {index}: {e}", exc_info=True)
                    results.append({"index": index, "error": str(e)})
    except Exception as e:
        logger.error(f"Errore globale scraping: {e}", exc_info=True)
        results.append({"error": str(e)})
    finally:
        driver.quit()
        logger.info("Driver chiuso")
    return results

@app.route("/health", methods=["GET"])
def health():
    return "ok", 200

@app.route("/files/<path:filename>", methods=["GET"])
def serve_file(filename):
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)

@app.route("/scrape", methods=["POST"])
def scrape():
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "URL mancante"}), 400

    results = scrape_page(url)
    base = request.host_url.rstrip("/")
    for r in results:
        if r.get("saved_file"):
            r["file_url"] = f"{base}/files/{r['saved_file']}"

    return jsonify({"results": results}), 200

@app.route("/batch_scrape", methods=["POST"])
def batch_scrape():
    """
    Riceve direttamente il payload di BrowseAI.
    Estrae la lista 'Annunci START' da capturedLists.
    Per ogni annuncio:
      - esegue scraping con Selenium
      - aggiunge i metadati
      - invia un JSON a Zapier via webhook
    """
    data = request.get_json(silent=True) or {}
    webhook = data.get("webhook")

    if not webhook:
        return jsonify({"error": "Parametro 'webhook' mancante"}), 400

    announcements = data.get("task", {}).get("capturedLists", {}).get("Annunci START", [])
    if not announcements:
        logger.warning("Nessun annuncio trovato nel payload")
        return jsonify({"status": "no_announcements"}), 200

    base = request.host_url.rstrip("/")

    for annuncio in announcements:
        url = annuncio.get("link ai documenti dell'annuncio")
        if not url:
            logger.warning("Annuncio senza link ai documenti, skip")
            continue

        logger.info(f"[BATCH] Processing annuncio ID {annuncio.get('ID annuncio e anno')} - URL: {url}")
        results = scrape_page(url)

        for r in results:
            if r.get("saved_file"):
                r["file_url"] = f"{base}/files/{r['saved_file']}"

        payload = {
            "annuncio": {
                "id": annuncio.get("ID annuncio e anno"),
                "titolo": annuncio.get("titolo annuncio"),
                "ente": annuncio.get("ente promotore"),
                "stato": annuncio.get("stato gara"),
                "url": url
            },
            "results": results
        }

        try:
            resp = requests.post(webhook, json=payload, timeout=30)
            logger.info(f"[BATCH] Webhook inviato a {webhook}, status {resp.status_code}")
        except Exception as e:
            logger.error(f"[BATCH] Errore invio webhook: {e}", exc_info=True)

    logger.info(f"[BATCH] Completato, processati {len(announcements)} annunci")
    return jsonify({"status": "batch completato", "processed": len(announcements)}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
