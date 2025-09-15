import os
import time
from flask import Flask, request, jsonify

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

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
    return driver

def scrape_page(url: str):
    driver = build_driver()
    results = []

    try:
        print("[INFO] Navigo:", url)
        driver.get(url)

        # Attesa della lista allegati
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

                    # Scroll al link
                    driver.execute_script("arguments[0].scrollIntoView(true);", link)
                    time.sleep(0.5)

                    # Attendi che sia cliccabile
                    wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, 'a[data-qa="attachment"]')))

                    # Click via JavaScript (più robusto in headless)
                    driver.execute_script("arguments[0].click();", link)

                    # Pausa per non sovrapporre i click
                    time.sleep(2)

                    results.append({
                        "index": index,
                        "label": file_label,
                        "status": "cliccato"
                    })

                except Exception as e:
                    results.append({
                        "index": index,
                        "error": str(e)
                    })

    except Exception as e:
        results.append({"error": str(e)})

    finally:
        driver.quit()

    return results

@app.route("/health", methods=["GET"])
def health():
    return "ok", 200

@app.route("/scrape", methods=["POST"])
def scrape():
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "URL mancante"}), 400

    results = scrape_page(url)
    return jsonify({"results": results}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
