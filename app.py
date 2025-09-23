import os
import time
import threading
import requests
import mimetypes
import json
import subprocess
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

# Webhook statico Zapier (destinazione finale)
WEBHOOK_DEST = os.environ.get("WEBHOOK_DEST")
# Gemini API
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_UPLOAD_ENDPOINT = "https://generativelanguage.googleapis.com/upload/v1beta/files"

# Whitelist CPV (prime due cifre valide)
CPV_WHITELIST = {"30","32","48","51","64","71","72","73","79","80","85","90","92","98"}

app = Flask(__name__)

# ----------------------------
# Funzioni di supporto
# ----------------------------

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

    # Consenti download
    try:
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": DOWNLOAD_DIR}
        )
    except Exception as e:
        print(f"[WARN] setDownloadBehavior: {e}", flush=True)

    # ✅ Concedi permesso ai download multipli
    try:
        driver.execute_cdp_cmd(
            "Browser.setPermission",
            {
                "permission": {"name": "automatic-downloads"},
                "origin": "https://start.toscana.it",
                "setting": "granted",
            }
        )
        print("[INFO] Permesso 'automatic-downloads' concesso a start.toscana.it", flush=True)
    except Exception as e:
        print(f"[WARN] setPermission automatic-downloads: {e}", flush=True)

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

                    driver.execute_script("arguments[0].scrollIntoView(true);", link)
                    time.sleep(0.5)
                    try:
                        link.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", link)

                    new_file = None
                    # ✅ Estendi attesa fino a 20s (40 cicli da 0.5s)
                    for _ in range(40):
                        time.sleep(0.5)
                        after = set(os.listdir(DOWNLOAD_DIR))
                        created = list(after - before)
                        created = [f for f in created if not f.endswith(".crdownload") and not f.endswith(".tmp")]
                        if created:
                            new_file = created[0]
                            print(f"[INFO] → File scaricato: {new_file}", flush=True)
                            break

                    result = {"index": index, "label": file_label, "href": href}
                    if new_file:
                        result["saved_file"] = new_file
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
# (resto del file invariato)
