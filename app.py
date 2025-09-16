import os
import time
import threading
import requests
from urllib.parse import quote
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

    # Dico a Chrome headless dove salvare i file
    driver.execute_cdp_cmd(
        "Page.setDownloadBehavior",
        {"behavior": "allow", "downloadPath": DOWNLOAD_DIR}
    )

    return driver

def scrape_page(url: str):
    """Funziona come prima: scarica gli allegati da un singolo annuncio."""
    driver = build_driver()
    results = []

    try:
        print("[INFO] Navigo:", url)
        driver.get(url)

        wait = WebDriverWait(driver, 20)
        wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".list-detail-view.sortable")))
        list_items = driver.find_elements(By.CSS_SELECTOR, ".list-detail-view.sortable")

        if not list_items:
            results.append({"message": "Nessun allegato trovato."})
        else:
            for index, item in enumerate(list_items, start=1):
                try:
                    link = item.find_element(By.CSS_SELECTOR, 'a[data-qa="attachment"]')
                    file_label = (link.text or "").strip()
                    if "." in file_label:
                        file_label = file_label.rsplit(".", 1)[0]  # prendi tutto prima dell’ultimo punto

                    href = link.get_attribute("href")

                    # Stato iniziale cartella
                    before = set(os.listdir(DOWNLOAD_DIR))

                    # Scroll e click
                    driver.execute_script("arguments[0].scrollIntoView(true);", link)
                    time.sleep(0.5)
                    driver.execute_script("arguments[0].click();", link)

                    # Attendo fino a 10s per nuovo file
                    new_file = None
                    for _ in range(20):
                        time.sleep(0.5)
                        after = set(os.listdir(DOWNLOAD_DIR))
                        created = list(after - before)
                        created = [f for f in created if not f.endswith(".crdownload") and not f.endswith(".tmp")]
                        if created:
                            new_file = created[0]
                            break

                    result = {
                        "index": index,
                        "label": file_label,
                        "href": href
                    }
                    if new_file:
                        result["saved_file"] = new_file
                    results.append(result)

                except Exception as e:
                    results.append({"index": index, "error": str(e)})

    except Exception as e:
        results.append({"error": str(e)})

    finally:
        driver.quit()

    return results

def process_async(urls, webhook_url, base_url):
    """Processa gli URL uno alla volta e invia i risultati a Zapier via webhook."""
    for u in urls:
        page_results = scrape_page(u)
        # arricchisco con i link pubblici
        for r in page_results:
            if r.get("saved_file"):
                encoded_name = quote(r["saved_file"])
                r["file_url"] = f"{base_url}/files/{encoded_name}"

        payload = {
            "url": u,
            "results": page_results
        }

        try:
            print(f"[INFO] Invio risultati a Zapier per {u}")
            requests.post(webhook_url, json=payload, timeout=10)
        except Exception as e:
            print(f"[ERRORE] Invio webhook fallito per {u}: {e}")

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

    # arricchisco con i link pubblici
    base = request.host_url.rstrip("/")
    for r in results:
        if r.get("saved_file"):
            encoded_name = quote(r["saved_file"])
            r["file_url"] = f"{base}/files/{encoded_name}"

    return jsonify({"results": results}), 200

@app.route("/scrape_async", methods=["POST"])
def scrape_async():
    """
    Riceve una stringa di URL separati da virgole e un webhook URL.
    Avvia il lavoro in background e risponde subito con 'in lavorazione'.
    """
    data = request.get_json(silent=True) or {}
    urls_str = data.get("urls")
    webhook_url = data.get("webhook_url")

    if not urls_str or not webhook_url:
        return jsonify({"error": "urls e webhook_url sono richiesti"}), 400

    urls = [u.strip() for u in urls_str.split(",") if u.strip()]
    base_url = request.host_url.rstrip("/")

    # Avvio thread in background
    threading.Thread(target=process_async, args=(urls, webhook_url, base_url), daemon=True).start()

    return jsonify({"status": "in lavorazione", "urls": urls}), 202

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
