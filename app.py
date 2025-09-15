import os
import time
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

    # Abilito i download in headless verso DOWNLOAD_DIR (CDP)
    try:
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": DOWNLOAD_DIR}
        )
    except Exception as e:
        print(f"[WARN] setDownloadBehavior fallita: {e}")

    return driver

def scrape_and_download(url: str):
    driver = build_driver()
    results = []
    try:
        print("[INFO] Navigo:", url)
        driver.get(url)

        wait = WebDriverWait(driver, 25)
        wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".list-detail-view.sortable")))
        list_items = driver.find_elements(By.CSS_SELECTOR, ".list-detail-view.sortable")

        if not list_items:
            results.append({"message": "Nessun allegato trovato."})
        else:
            for index, item in enumerate(list_items, start=1):
                try:
                    link = item.find_element(By.CSS_SELECTOR, 'a[data-qa="attachment"]')
                    file_label = (link.text or "").strip()

                    # Stato iniziale dei file presenti
                    before = set(os.listdir(DOWNLOAD_DIR))

                    # Scroll + click
                    driver.execute_script("arguments[0].scrollIntoView(true);", link)
                    time.sleep(0.3)
                    link.click()

                    # Attendo che compaia un nuovo file (max 30s)
                    new_file = None
                    for _ in range(60):  # 60 * 0.5s = 30s
                        time.sleep(0.5)
                        after = set(os.listdir(DOWNLOAD_DIR))
                        created = list(after - before)
                        # Escludo eventuali .crdownload ancora in corso
                        created = [f for f in created if not f.endswith(".crdownload") and not f.endswith(".tmp")]
                        if created:
                            # Se arrivano più file, li elenco tutti (di solito è 1)
                            new_file = created[0]
                            break

                    if new_file:
                        results.append({
                            "index": index,
                            "label": file_label,
                            "saved_file": new_file
                        })
                    else:
                        results.append({
                            "index": index,
                            "label": file_label,
                            "warning": "Nessun nuovo file rilevato dopo il click (potrebbe essere redirect protetto o tempo non sufficiente)."
                        })

                except Exception as e:
                    results.append({"index": index, "error": str(e)})

    except Exception as e:
        results.append({"error": str(e)})

    finally:
        driver.quit()

    return results

@app.route("/health", methods=["GET"])
def health():
    return "ok", 200

@app.route("/files/<path:filename>", methods=["GET"])
def serve_file(filename):
    # Rende scaricabile il file dal server (così Zapier può prenderlo)
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)

@app.route("/scrape", methods=["POST"])
def scrape():
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "URL mancante"}), 400

    results = scrape_and_download(url)

    # Costruisco URL pubblici dei file per Zapier
    base = request.host_url.rstrip("/")
    enriched = []
    for r in results:
        if r.get("saved_file"):
            r["file_url"] = f"{base}/files/{r['saved_file']}"
        enriched.append(r)

    return jsonify({"results": enriched}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
