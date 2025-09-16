import threading
# ... resto degli import come prima ...

@app.route("/batch_scrape", methods=["POST"])
def batch_scrape():
    """
    Riceve un payload JSON con:
      - urls: stringa con più URL separati da virgole
      - webhook: URL Zapier a cui inviare i risultati
    Risponde SUBITO al client per evitare timeout,
    ed esegue Selenium in background in un thread separato.
    """
    data = request.get_json(silent=True) or {}
    raw_urls = data.get("urls", "")
    webhook = data.get("webhook")

    if not raw_urls or not webhook:
        return jsonify({"error": "Parametri mancanti (urls, webhook)"}), 400

    urls = [u.strip() for u in raw_urls.split(",") if u.strip()]
    base = request.host_url.rstrip("/")

    def process_batch(urls, webhook, base):
        logger.info(f"[BATCH] Avvio processamento in background ({len(urls)} URL)")
        for url in urls:
            try:
                logger.info(f"[BATCH] Processing URL: {url}")
                results = scrape_page(url)

                for r in results:
                    if r.get("saved_file"):
                        r["file_url"] = f"{base}/files/{r['saved_file']}"

                payload = {"url": url, "results": results}

                try:
                    resp = requests.post(webhook, json=payload, timeout=30)
                    logger.info(f"[BATCH] Webhook inviato a {webhook}, status {resp.status_code}")
                except Exception as e:
                    logger.error(f"[BATCH] Errore invio webhook: {e}", exc_info=True)
            except Exception as e:
                logger.error(f"[BATCH] Errore durante lo scraping URL {url}: {e}", exc_info=True)

        logger.info(f"[BATCH] Completato, processati {len(urls)} URL")

    # Lancio thread in background
    thread = threading.Thread(target=process_batch, args=(urls, webhook, base))
    thread.daemon = True
    thread.start()

    # Risposta immediata al client
    return jsonify({"status": "started", "urls": len(urls)}), 200
