"""
debug_yahoo_news.py — Ver qué devuelve yfinance.news para un ticker.

El formato de .news ha cambiado entre versiones de yfinance, así que esto
nos enseña la estructura REAL en tu instalación, para integrarlo bien en el v3.

Uso:  python debug_yahoo_news.py STC
      python debug_yahoo_news.py          (STC por defecto)
"""
import sys
import json

try:
    import yfinance as yf
except ImportError:
    print("Falta yfinance:  pip install yfinance")
    sys.exit(1)

ticker = (sys.argv[1] if len(sys.argv) > 1 else "STC").upper()

print("yfinance version:", getattr(yf, "__version__", "desconocida"))
print(f"Pidiendo noticias de {ticker} a Yahoo...\n")

t = yf.Ticker(ticker)

# 1) Intentar el atributo clásico .news
news = None
try:
    news = t.news
except Exception as e:
    print("t.news falló:", e)

# 2) Algunas versiones nuevas usan get_news()
if not news:
    try:
        news = t.get_news()
    except Exception as e:
        print("t.get_news() falló:", e)

if not news:
    print("No devolvió noticias (lista vacía o None).")
    sys.exit(0)

print("Nº de noticias devueltas:", len(news))
print("=" * 78)

# Volcar la PRIMERA noticia completa (estructura cruda) para ver las claves
print("ESTRUCTURA de la primera noticia (JSON crudo):")
print(json.dumps(news[0], indent=2, default=str)[:2000])
print("=" * 78)

# Intento de extracción "inteligente": el formato nuevo mete todo bajo 'content'
print("\nResumen de las primeras 8 (intento de extracción):")
import datetime
for i, item in enumerate(news[:8]):
    # Formato viejo: claves planas.  Formato nuevo: dentro de item['content']
    c = item.get("content", item)
    title = c.get("title") or item.get("title") or "(sin título)"
    # fecha: puede venir como pubDate (nuevo) o providerPublishTime epoch (viejo)
    pub = c.get("pubDate") or c.get("displayTime")
    if not pub and item.get("providerPublishTime"):
        pub = datetime.datetime.fromtimestamp(item["providerPublishTime"]).isoformat()
    # fuente
    prov = ""
    if isinstance(c.get("provider"), dict):
        prov = c["provider"].get("displayName", "")
    prov = prov or item.get("publisher", "")
    print(f"[{i}] {str(pub):25} | {prov:20} | {str(title)[:70]}")

print("=" * 78)
print("\nQué mirar:")
print("  - ¿La fecha de [0] es de hoy/ayer? -> Yahoo SÍ trae noticias recientes.")
print("  - Fíjate si los datos están planos o dentro de 'content' (define cómo parsear).")