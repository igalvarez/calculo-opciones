import requests, csv, time
KEY = "Mpn2_vOr7WER_ieJWytP6tMWp_Ez2wLA"

def bajar(simbolo, desde, hasta):
    url = f"https://api.polygon.io/v2/aggs/ticker/{simbolo}/range/15/minute/{desde}/{hasta}"
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": KEY}
    filas = []
    while url:
        d = requests.get(url, params=params).json()
        if "results" not in d:
            print(d); break
        filas += [(x["t"], x["o"], x["h"], x["l"], x["c"], x["v"]) for x in d["results"]]
        url = d.get("next_url"); params = {"apiKey": KEY}
        time.sleep(13)                      # tope de 5 peticiones por minuto
    with open(f"{simbolo.lower()}-15m.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["t_ms_utc","open","high","low","close","volume"]); w.writerows(filas)
    print(simbolo, len(filas), "velas")

bajar("NVDA", "2024-11-06", "2026-11-04")
bajar("QQQ",  "2024-11-06", "2026-11-04")  # prueba pequeña
# si funciona, cambia el desde a 2024-11-06 y repite con QQQ