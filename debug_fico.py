"""
debug_fico.py — Diagnostica por qué FICO no aparece en el screener del script.
Hace la MISMA petición y el MISMO parseo que get-info-v1.py, pero vuelca
la estructura de la fila de FICO para ver exactamente dónde se pierde.

Uso:  python debug_fico.py
"""
import re
import requests
from lxml import html

URL = ("https://finviz.com/screener.ashx?v=111"
       "&f=cap_smallover,targetprice_a50,an_recom_buybetter,ta_sma20_pb20,geo_usa"
       "&o=-marketcap&ft=4&r=1")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://finviz.com/",
}


def extract_ticker(row):
    """Misma lógica que el script: saca el ticker del primer href con t=."""
    for a in row.xpath('.//a[@href]'):
        href = a.get("href", "")
        if "t=" in href:
            m = re.search(r'[?&]t=([A-Za-z.\-]+)', href)  # permite . y - por si acaso
            if m:
                return m.group(1)
    return ""


def main():
    r = requests.Session().get(URL, headers=HEADERS, timeout=20)
    print("HTTP:", r.status_code, "| bytes:", len(r.content))

    tree = html.fromstring(r.content)

    # Mismo xpath que el script (con el mismo fallback)
    rows = tree.xpath('//table[contains(@class,"screener_table")]//tr[position()>1]')
    if not rows:
        rows = tree.xpath('//tr[@class="table-light-row-cp" or @class="table-dark-row-cp"]')
    print("Filas que ve el xpath:", len(rows))
    print("-" * 72)

    # Cabecera real (para ver el mapeo verdadero de columnas de v=111)
    header = tree.xpath('//table[contains(@class,"screener_table")]//tr[1]//td')
    if header:
        print("CABECERA:", [c.text_content().strip() for c in header])
        print("-" * 72)

    # Resumen: ticker + nº de columnas de cada fila (así se ve si FICO descuadra)
    for i, row in enumerate(rows):
        n = len(row.xpath('.//td'))
        t = extract_ticker(row)
        flag = "  <-- FICO" if t.upper() == "FICO" else ""
        print(f"[{i:2}] {t:8} ncols={n}{flag}")

    # Detalle celda a celda de FICO y de un vecino "normal" (GEN)
    for target in ("FICO", "GEN"):
        print("=" * 72)
        print("DETALLE:", target)
        found = False
        for row in rows:
            if extract_ticker(row).upper() == target:
                found = True
                cols = [td.text_content().strip() for td in row.xpath('.//td')]
                for j, c in enumerate(cols):
                    print(f"  cols[{j}] = {c!r}")
                # Simular lo que hace el script:
                if len(cols) < 11:
                    print(f"  >>> len(cols)={len(cols)} < 11  =>  el script hace 'continue' "
                          f"y DESCARTA {target}")
                else:
                    print(f"  >>> market_cap = cols[6] = {cols[6]!r}   "
                          f"price = cols[8] = {cols[8]!r}   volume = cols[10] = {cols[10]!r}")
                break
        if not found:
            print(f"  {target} NO está entre las filas del xpath "
                  f"(su <tr> no coincide con el selector).")


if __name__ == "__main__":
    main()