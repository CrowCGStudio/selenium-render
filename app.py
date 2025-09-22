import os
import time
import logging
from flask import Flask, request, jsonify
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException
import requests

app = Flask(__name__)

# Configura logging
logging.basicConfig(level=logging.INFO)

# Divisioni CPV ammesse
DIVISIONI_AMMESSE = {
    "30", "32", "48", "51", "64", "72", "73",
    "79", "80", "85", "90", "92", "98"
}

# Funzione di supporto: controllo divisione CPV
def cpv_divisione_ammessa(driver, annuncio_id):
    """
    Apre la pagina /2 di un annuncio, estrae il codice CPV principale
    e restituisce True se la divisione (prime due cifre) è ammessa.
    """
    url = f"https://start.toscana.it/tendering/tenders/{annuncio_id}/view/detail/2"
    driver.get(url)

    try:
        # Trova il codice CPV principale
        span = driver.find_element(By.CSS_SELECTOR, "span[data-qa='remove-primary-cat-container']")
        testo = span.text.strip()
        # Esempio: "45454000-4. Lavori di ristrutturazione"

        codice = testo.split(".")[0].strip()  # → "45454000-4"
        divisione = codice[:2]  # → "45"

        if divisione in DIVISIONI_AMMESSE:
            logging.info(f"Annuncio {annuncio_id} ammesso (divisione {divisione})")
            return True
        else:
            logging.info(f"Annuncio {annuncio_id} scartato (divisione {divisione} non ammessa)")
            return False

    except NoSuchElementException:
        logging.warning(f"Annuncio {annuncio_id}: nessun codice CPV trovato, scartato")
        return False


@app.route("/ricevi_annunci", methods=["POST"])
def ricevi_annunci():
    data = request.json
    if not data or "annunci" not in data:
        return jsonify({"error": "Nessun annuncio ricevuto"}), 400

    annunci = data["annunci"]

    # Configura Selenium
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=chrome_options)

    risultati = []

    try:
        for annuncio_id in annunci:
            # 1. Controllo CPV su /2
            if not cpv_divisione_ammessa(driver, annuncio_id):
                continue  # scarta e passa al prossimo annuncio

            # 2. Procedi come prima su /1
            url = f"https://start.toscana.it/tendering/tenders/{annuncio_id}/view/detail/1"
            driver.get(url)

            time.sleep(2)  # attesa minima per caricamento pagina

            # Recupera link degli allegati (esempio, mantieni tua logica esistente)
            allegati = driver.find_elements(By.CSS_SELECTOR, "a[data-qa='download-attachment']")
            file_urls = [a.get_attribute("href") for a in allegati]

            for file_url in file_urls:
                try:
                    resp = requests.get(file_url)
                    if resp.status_code == 200:
                        filename = os.path.join("files", os.path.basename(file_url))
                        with open(filename, "wb") as f:
                            f.write(resp.content)
                        risultati.append(filename)
                        logging.info(f"Scaricato file {filename} per annuncio {annuncio_id}")
                except Exception as e:
                    logging.error(f"Errore scaricando {file_url}: {e}")

    finally:
        driver.quit()

    # Non ritorniamo errori diversi: webhook riceve solo conferma generica
    return jsonify({"status": "ok", "files": risultati})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
