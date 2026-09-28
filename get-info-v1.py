import sys

# Verificación de módulos requeridos
_modulos_requeridos = {
    "finviz":   "finviz",
    "requests": "requests",
    "colorama": "colorama",
    "lxml":     "lxml",
    "bs4":      "beautifulsoup4",
}

_faltantes = []
for modulo, paquete in _modulos_requeridos.items():
    try:
        __import__(modulo)
    except ImportError:
        _faltantes.append(paquete)


if _faltantes:
    print("\n  ERROR: Faltan los siguientes módulos Python:\n")
    for paquete in _faltantes:
        print(f"    pip install {paquete}")
    print("\n  O instálalos todos de una vez:")
    print(f"\n    pip install {' '.join(_faltantes)}\n")
    sys.exit(1)

import os
import json
import datetime
import tempfile
import finviz
import requests
from collections import defaultdict
from colorama import init, Fore, Style

init(autoreset=True)


# ─── CACHÉ ────────────────────────────────────────────────────────────────────

def get_cache_path():
    return os.path.join(tempfile.gettempdir(), "get_info_target_cache.json")

def load_cache():
    path = get_cache_path()
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_cache(cache):
    try:
        with open(get_cache_path(), "w") as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass


# ─── UTILIDADES ───────────────────────────────────────────────────────────────

def to_float(value):
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except (ValueError, TypeError):
        return None

def recom_color(recom_value):
    recom = to_float(recom_value)
    if recom is None:
        return ""
    if recom <= 2:
        return Fore.GREEN
    elif recom <= 3:
        return ""
    else:
        return Fore.RED

def dist_color(pct):
    if pct is None:
        return ""
    abs_pct = abs(pct)
    if abs_pct > 25:
        return Fore.RED
    elif abs_pct > 15:
        return Fore.YELLOW
    return ""


# ─── TABLA PRINCIPAL ──────────────────────────────────────────────────────────

def print_main_table(tickers_data, target_dates=None, target_status=None):
    columns = ["Ticker", "Index", "Market Cap", "Price", "Target Price", "Dist%", "Target Date", "Status", "Recom"]
    rows = []
    target_colors = []
    dist_colors   = []
    status_colors = []
    recom_colors  = []

    for ticker, data in tickers_data:
        if data is None:
            rows.append([ticker, "ERROR", "", "", "", "", "", "", ""])
            target_colors.append("")
            dist_colors.append("")
            status_colors.append("")
            recom_colors.append("")
            continue

        price_raw  = data.get('Price', 'N/A')
        target_raw = data.get('Target Price', 'N/A')
        recom_raw  = data.get('Recom', 'N/A')

        price  = to_float(price_raw)
        target = to_float(target_raw)

        if price is not None and target is not None:
            t_color  = Fore.GREEN if target > price else Fore.RED if target < price else ""
            pct      = (target - price) / price * 100
            dist_str = f"{pct:+.1f}%"
            d_color  = dist_color(pct)
        else:
            t_color  = ""
            pct      = None
            dist_str = "N/A"
            d_color  = ""

        r_color    = recom_color(recom_raw)
        t_date     = (target_dates or {}).get(ticker, "")
        status_val = (target_status or {}).get(ticker, "SAME")
        s_color    = Fore.YELLOW if status_val == "NEW" else ""

        rows.append([
            ticker,
            data.get('Index', 'N/A'),
            data.get('Market Cap', 'N/A'),
            price_raw,
            target_raw,
            dist_str,
            t_date,
            status_val,
            recom_raw,
        ])
        target_colors.append(t_color)
        dist_colors.append(d_color)
        status_colors.append(s_color)
        recom_colors.append(r_color)

    widths = [len(col) for col in columns]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def format_plain_row(row):
        return " | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row))

    def format_colored_row(row, t_color, d_color, s_color, r_color):
        cells = []
        for i, cell in enumerate(row):
            text = str(cell).ljust(widths[i])
            if   i == 4 and t_color: text = f"{t_color}{text}{Style.RESET_ALL}"
            elif i == 5 and d_color: text = f"{d_color}{text}{Style.RESET_ALL}"
            elif i == 7 and s_color: text = f"{s_color}{text}{Style.RESET_ALL}"
            elif i == 8 and r_color: text = f"{r_color}{text}{Style.RESET_ALL}"
            cells.append(text)
        return " | ".join(cells)

    separator = "-+-".join("-" * w for w in widths)
    print(format_plain_row(columns))
    print(separator)
    for row, t_color, d_color, s_color, r_color in zip(rows, target_colors, dist_colors, status_colors, recom_colors):
        print(format_colored_row(row, t_color, d_color, s_color, r_color))


# ─── RATINGS ──────────────────────────────────────────────────────────────────

def format_target(entry):
    t_from = entry.get('target_from')
    t_to   = entry.get('target_to')
    single = entry.get('target')
    if t_from is not None and t_to is not None:
        return f"${t_from} -> ${t_to}"
    if t_to is not None:
        return f"${t_to}"
    if single is not None:
        return f"${single}"
    return ""

def get_latest_target_date(ticker):
    try:
        ratings = finviz.get_analyst_price_targets(ticker, last_ratings=10)
    except Exception:
        return ""
    for entry in ratings:
        if entry.get('target_to') is not None or entry.get('target') is not None:
            return entry.get('date', '')
    return ""

def print_ratings_history(ticker, last_ratings=5):
    try:
        ratings = finviz.get_analyst_price_targets(ticker, last_ratings=last_ratings)
    except Exception as e:
        print(f"  No se pudo obtener el historial de ratings: {e}")
        return

    if not ratings:
        print("  Sin historial de ratings disponible.")
        return

    columns = ["Date", "Action", "Analyst", "Rating", "Target"]
    rows = []
    action_colors = []

    for entry in ratings:
        action = entry.get('category') or entry.get('action') or 'N/A'
        rows.append([
            entry.get('date', 'N/A'),
            action,
            entry.get('analyst', 'N/A'),
            entry.get('rating', 'N/A'),
            format_target(entry),
        ])
        action_lower = str(action).lower()
        if "upgrade" in action_lower:
            action_colors.append(Fore.GREEN)
        elif "downgrade" in action_lower:
            action_colors.append(Fore.RED)
        else:
            action_colors.append("")

    widths = [len(col) for col in columns]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def format_plain_row(row):
        return " | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row))

    def format_colored_row(row, color):
        cells = []
        for i, cell in enumerate(row):
            text = str(cell).ljust(widths[i])
            if i == 1 and color:
                text = f"{color}{text}{Style.RESET_ALL}"
            cells.append(text)
        return " | ".join(cells)

    separator = "-+-".join("-" * w for w in widths)
    print("  " + format_plain_row(columns))
    print("  " + separator)
    for row, color in zip(rows, action_colors):
        print("  " + format_colored_row(row, color))


# ─── INSIDER TRADING ──────────────────────────────────────────────────────────

def fetch_insider_trades(ticker):
    from lxml import html as lxml_html

    url = f"https://finviz.com/quote.ashx?t={ticker.upper()}&p=d"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://finviz.com/",
    }
    try:
        resp = requests.Session().get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        return None, str(e)

    try:
        tree   = lxml_html.fromstring(resp.content)
        tables = tree.xpath('//table[contains(@class,"body-table")]')
        if not tables:
            return [], None

        trades = []
        for row in tables[0].xpath('.//tr')[1:]:
            cols = [td.text_content().strip() for td in row.xpath('.//td')]
            if len(cols) < 4:
                continue
            name       = cols[0]
            trade_date = cols[2]
            trade_type = cols[3]
            try:
                dt = datetime.datetime.strptime(trade_date, "%b %d '%y")
                trade_date = dt.strftime("%Y-%m-%d")
            except Exception:
                pass
            if not name or not trade_type:
                continue
            trades.append({
                "trade_date": trade_date,
                "ticker":     ticker.upper(),
                "name":       name,
                "trade_type": trade_type,
            })
    except Exception as e:
        return None, f"Error parseando HTML: {e}"

    return trades, None

def classify_trade(trade_type):
    t = trade_type.lower()
    if "option" in t or "exercise" in t:
        return "ejercicio", "neutral"
    elif "proposed" in t:
        return "propuesta", "neutral"
    elif "sale" in t or "sell" in t:
        return "venta", "bajista"
    elif "buy" in t or "purchase" in t:
        return "compra", "alcista"
    else:
        return "otro", "neutral"

def print_insider_summary(ticker, trades):
    if not trades:
        print("  Sin datos de insider trading.")
        return

    # Filtrar últimos 30 días
    cutoff = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    trades = [t for t in trades if t["trade_date"] >= cutoff]

    if not trades:
        print("  Sin operaciones de insiders en los últimos 30 días.")
        return

    by_insider   = defaultdict(lambda: defaultdict(lambda: {"count": 0, "last_date": ""}))
    insider_last = {}

    for t in trades:
        name       = t["name"]
        trade_type = t["trade_type"]
        grupo, _   = classify_trade(trade_type)
        by_insider[name][grupo]["count"] += 1
        if not by_insider[name][grupo]["last_date"] or t["trade_date"] > by_insider[name][grupo]["last_date"]:
            by_insider[name][grupo]["last_date"] = t["trade_date"]
        if name not in insider_last or t["trade_date"] > insider_last[name]:
            insider_last[name] = t["trade_date"]

    insiders_sorted = sorted(insider_last.items(), key=lambda x: x[1], reverse=True)
    total_ventas = total_compras = total_ejercicios = 0

    print()
    for name, _ in insiders_sorted:
        grupos     = by_insider[name]
        last       = insider_last[name]
        partes     = []
        ventas     = grupos.get("venta",    {}).get("count", 0)
        compras    = grupos.get("compra",   {}).get("count", 0)
        ejercicios = grupos.get("ejercicio",{}).get("count", 0)
        propuestas = grupos.get("propuesta",{}).get("count", 0)

        total_ventas     += ventas
        total_compras    += compras
        total_ejercicios += ejercicios

        if ventas:     partes.append(f"{Fore.RED}{ventas} venta(s){Style.RESET_ALL}")
        if compras:    partes.append(f"{Fore.GREEN}{compras} compra(s){Style.RESET_ALL}")
        if ejercicios: partes.append(f"{ejercicios} ejercicio(s) de opciones")
        if propuestas: partes.append(f"{propuestas} propuesta(s)")

        if ventas > 0 and compras == 0:
            sentiment = f"{Fore.RED}Bajista{Style.RESET_ALL}"
        elif compras > 0 and ventas == 0:
            sentiment = f"{Fore.GREEN}Alcista{Style.RESET_ALL}"
        elif compras > 0 and ventas > 0:
            sentiment = "Mixto"
        else:
            sentiment = "Neutral"

        print(f"  {name:<26} [{last}]  {' | '.join(partes)}  →  {sentiment}")

    print()
    print("  " + "─" * 60)
    if total_ventas > 0 and total_compras == 0:
        sent_general = f"{Fore.RED}Bajista{Style.RESET_ALL}"
    elif total_compras > 0 and total_ventas == 0:
        sent_general = f"{Fore.GREEN}Alcista{Style.RESET_ALL}"
    elif total_compras > total_ventas:
        sent_general = f"{Fore.GREEN}Predominantemente alcista{Style.RESET_ALL}"
    elif total_ventas > total_compras:
        sent_general = f"{Fore.RED}Predominantemente bajista{Style.RESET_ALL}"
    else:
        sent_general = "Mixto"

    print(f"  Sentimiento neto: {sent_general}  "
          f"({total_ventas} ventas | {total_compras} compras | {total_ejercicios} ejercicios)")


# ─── NOTICIAS ─────────────────────────────────────────────────────────────────

def fetch_news(ticker, count=5):
    from lxml import html as lxml_html

    url = f"https://finviz.com/quote.ashx?t={ticker.upper()}&p=d"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://finviz.com/",
    }
    try:
        resp = requests.Session().get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        return None, str(e)

    try:
        tree       = lxml_html.fromstring(resp.content)
        news_table = tree.xpath('//table[@id="news-table"]')
        if not news_table:
            return [], None

        news         = []
        current_date = ""
        for row in news_table[0].xpath('.//tr'):
            cols = row.xpath('.//td')
            if len(cols) < 2:
                continue
            date_raw = cols[0].text_content().strip()
            headline = cols[1].text_content().strip()
            source_el = cols[1].xpath('.//span')
            source   = source_el[0].text_content().strip() if source_el else ""

            if len(date_raw) > 8:
                parts        = date_raw.split()
                current_date = parts[0] if len(parts) > 1 else current_date
                time_str     = parts[-1] if len(parts) > 1 else date_raw
            else:
                time_str = date_raw

            if source and headline.endswith(source):
                headline = headline[:-len(source)].strip()

            news.append({"date": current_date, "time": time_str, "headline": headline, "source": source})
            if len(news) >= count:
                break
    except Exception as e:
        return None, f"Error parseando noticias: {e}"

    return news, None

def translate_headline(text):
    """Traduce un titular al español usando MyMemory (gratis, sin key)."""
    try:
        resp = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text, "langpair": "en|es"},
            timeout=8,
        )
        data = resp.json()
        translated = data.get("responseData", {}).get("translatedText", "")
        if translated and translated != text and "MYMEMORY WARNING" not in translated:
            return translated
        return text
    except Exception:
        return text


def translate_headlines(headlines):
    """Traduce lista de titulares al español usando MyMemory."""
    return [translate_headline(h) for h in headlines]


def print_news(ticker, news):
    if not news:
        print("  Sin noticias disponibles.")
        return
    headlines    = [item['headline'] for item in news]
    translations = translate_headlines(headlines)
    for item, translated in zip(news, translations):
        date_str = f"{item['date']} {item['time']}".strip()
        source   = f"({item['source']})" if item['source'] else ""
        print(f"  {Fore.CYAN}{date_str:<18}{Style.RESET_ALL} {translated}  {source}")


# ─── SCREENER BELOW20 ─────────────────────────────────────────────────────────

def parse_market_cap_value(cap_str):
    """Convierte '1.23B' o '487.51M' a float en millones para comparar."""
    if not cap_str or cap_str in ("-", "N/A", ""):
        return 0.0
    s = cap_str.strip().upper().replace(",", "")
    try:
        if s.endswith("B"):
            return float(s[:-1]) * 1000
        elif s.endswith("M"):
            return float(s[:-1])
        else:
            return float(s) / 1_000_000
    except ValueError:
        return 0.0

def fetch_stock_details(ticker):
    """
    Obtiene desde la página de quote de Finviz los campos:
      Recom, Book/sh, Sales, Income, Index
    Devuelve dict o None si falla.
    """
    from lxml import html as lxml_html
    import time

    url = f"https://finviz.com/quote.ashx?t={ticker.upper()}&p=d"
    url_alt = f"https://finviz.com/stock?t={ticker.upper()}&p=d"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer":         "https://finviz.com/screener.ashx",
    }
    try:
        session = requests.Session()
        resp = session.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            resp = session.get(url_alt, headers=headers, timeout=15)
        resp.raise_for_status()
        tree  = lxml_html.fromstring(resp.content)
        # Snapshot table: celdas alternas clave/valor dentro de cada fila <tr>
        data = {}
        rows = tree.xpath('//table[contains(@class,"snapshot-table2")]//tr')
        for tr in rows:
            tds = tr.xpath('.//td')
            for i in range(0, len(tds) - 1, 2):
                key = tds[i].text_content().strip()
                val = tds[i + 1].text_content().strip()
                if key:
                    data[key] = val
        return {
            "Recom":   data.get("Recom", ""),
            "Book/sh": data.get("Book/sh", ""),
            "Sales":   data.get("Sales", ""),
            "Income":  data.get("Income", ""),
            "Index":   data.get("Index", ""),
            "IPO":     data.get("IPO", ""),
        }
    except Exception as e:
        print(f"    ERROR en fetch_stock_details({ticker}): {e}")
        return None

def parse_finviz_number(val):
    """
    Convierte valores Finviz como '1.23B', '-45.6M', '789K' a float.
    Devuelve None si no parseable.
    """
    if not val or val in ("-", "N/A", ""):
        return None
    s = val.strip().upper().replace(",", "").replace("$", "")
    try:
        if s.endswith("B"):
            return float(s[:-1]) * 1_000_000_000
        elif s.endswith("M"):
            return float(s[:-1]) * 1_000_000
        elif s.endswith("K"):
            return float(s[:-1]) * 1_000
        else:
            return float(s)
    except ValueError:
        return None

def fetch_below20_screener():
    """
    Consulta el screener de Finviz con los filtros:
      - Market Cap: Small+ (>300M)
      - Target Price: 50% Above Price
      - Analyst Recom: Buy or Better
      - SMA20: Price 20% Below SMA20
      - Country: USA
    Luego filtra en local: Market Cap >500M y aplica 5 filtros adicionales
    por ticker (Recom <2.5, Book/sh > Price, Sales >0, Income >0, Index != "").
    """
    from lxml import html as lxml_html
    import time

    url = (
        "https://finviz.com/screener.ashx"
        "?v=111"
        "&f=cap_smallover,targetprice_a50,an_recom_buybetter,ta_sma20_pb20,geo_usa"
        "&o=-marketcap"
        "&ft=4"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer":         "https://finviz.com/",
    }

    stocks = []
    page   = 1

    while True:
        paged_url = url + f"&r={1 + (page - 1) * 20}"
        try:
            resp = requests.Session().get(paged_url, headers=headers, timeout=20)
            resp.raise_for_status()
        except Exception as e:
            print(f"  {Fore.RED}Error al conectar con Finviz: {e}{Style.RESET_ALL}")
            break

        tree = lxml_html.fromstring(resp.content)
        rows = tree.xpath('//table[contains(@class,"screener_table")]//tr[position()>1]')
        if not rows:
            rows = tree.xpath('//tr[@class="table-light-row-cp" or @class="table-dark-row-cp"]')
        if not rows:
            break

        for row in rows:
            cols = [td.text_content().strip() for td in row.xpath('.//td')]
            # Extraer ticker del href: buscar cualquier enlace con "t=" en la URL
            ticker = None
            all_links = row.xpath('.//a[@href]')
            for link in all_links:
                href = link.get("href", "")
                # href puede ser "quote.ashx?t=ACAD&..." o "stock?t=ACAD&..."
                if "t=" in href:
                    import re
                    m = re.search(r'[?&]t=([A-Z]+)', href)
                    if m:
                        ticker = m.group(1)
                        break
            if not ticker and len(cols) >= 2:
                # Fallback: limpiar posible letra duplicada
                ticker = cols[1]
            if not ticker:
                continue

            if len(cols) < 11:
                continue

            company    = cols[2]
            sector     = cols[3]
            market_cap = cols[6]
            price      = cols[8]
            change     = cols[9]
            volume     = cols[10]

            stocks.append({
                "Ticker":     ticker,
                "Company":    company,
                "Sector":     sector,
                "Market Cap": market_cap,
                "Price":      price,
                "Change%":    change,
                "Volume":     volume,
            })

        next_links = tree.xpath('//a[contains(@class,"screener-pages") and contains(text(),"next")]')
        if not next_links:
            next_links = tree.xpath('//a[@id="screener-next"]')
        if not next_links:
            break
        page += 1

    # Filtro 1: Market Cap >500M
    stocks = [s for s in stocks if parse_market_cap_value(s["Market Cap"]) >= 500]

    # Filtros adicionales por ticker (llamada extra a cada quote, silencioso)
    total = len(stocks)
    for i, s in enumerate(stocks, 1):
        ticker = s["Ticker"]
        print(f"  Verificando {ticker} ({i}/{total})...", end="\r")
        details = fetch_stock_details(ticker)
        time.sleep(0.4)  # respetar rate limit Finviz

        if details is None:
            s["Recom"]    = "-"
            s["Book/sh"]  = "-"
            s["Sales"]    = "-"
            s["Income"]   = "-"
            s["Index"]    = "-"
            s["IPO"]      = "-"
            s["ok_pb"]    = False
            s["ok_sales"] = False
            s["ok_inc"]   = False
            s["ok_idx"]   = False
            s["ok_ipo"]   = False
            continue

        price_f   = to_float(s["Price"])
        booksh_f  = parse_finviz_number(details["Book/sh"])
        sales_f   = parse_finviz_number(details["Sales"])
        income_f  = parse_finviz_number(details["Income"])
        index_val = details["Index"].strip()
        ipo_raw   = details["IPO"].strip()

        pb = (price_f / booksh_f) if (booksh_f and booksh_f > 0 and price_f) else None

        # Evaluar IPO >= 2 años
        ipo_ok = False
        if ipo_raw and ipo_raw not in ("-", "N/A", ""):
            try:
                ipo_date = datetime.datetime.strptime(ipo_raw, "%b %d, %Y").date()
                cutoff   = datetime.date.today() - datetime.timedelta(days=730)
                ipo_ok   = ipo_date <= cutoff
            except ValueError:
                pass

        s["Recom"]    = details["Recom"]
        s["Book/sh"]  = details["Book/sh"]
        s["P/B"]      = f"{pb:.2f}" if pb else "-"
        s["Sales"]    = details["Sales"]
        s["Income"]   = details["Income"]
        s["Index"]    = index_val if (index_val and index_val not in ("-", "N/A")) else "-"
        s["IPO"]      = ipo_raw if ipo_raw and ipo_raw not in ("-", "N/A") else "-"
        s["ok_pb"]    = pb is not None and pb < 3
        s["ok_sales"] = sales_f is not None and sales_f > 0
        s["ok_inc"]   = income_f is not None and income_f > 0
        s["ok_idx"]   = bool(index_val) and index_val not in ("-", "N/A")
        s["ok_ipo"]   = ipo_ok

    print(" " * 50, end="\r")  # limpiar línea de progreso
    return stocks

def market_status_info():
    """Devuelve (timestamp_str, status_str, color) según hora ET del mercado NYSE."""
    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    # ET = UTC-4 en verano (EDT), UTC-5 en invierno (EST)
    # Aproximación: EDT de 2º domingo marzo al 1º domingo noviembre
    year = now_utc.year
    # 2º domingo de marzo
    mar1 = datetime.date(year, 3, 1)
    dst_start = mar1 + datetime.timedelta(days=(6 - mar1.weekday()) % 7 + 7)
    # 1º domingo de noviembre
    nov1 = datetime.date(year, 11, 1)
    dst_end = nov1 + datetime.timedelta(days=(6 - nov1.weekday()) % 7)

    today = now_utc.date()
    offset = -4 if dst_start <= today < dst_end else -5
    now_et = now_utc + datetime.timedelta(hours=offset)
    tz_label = "EDT" if offset == -4 else "EST"

    ts_str = now_et.strftime(f"%Y-%m-%d %H:%M {tz_label}") + f"  (local: {datetime.datetime.now().strftime('%H:%M')})"

    weekday = now_et.weekday()  # 0=lunes, 6=domingo
    hour    = now_et.hour
    minute  = now_et.minute
    t_min   = hour * 60 + minute

    if weekday >= 5:
        status = "MERCADO CERRADO (fin de semana) — datos del último cierre"
        color  = Fore.YELLOW
    elif t_min < 9 * 60 + 30:
        status = "PRE-MARKET — mercado aún no ha abierto"
        color  = Fore.YELLOW
    elif t_min <= 16 * 60:
        status = "MERCADO ABIERTO — datos delayed ~15-20 min (Finviz gratuito)"
        color  = Fore.GREEN
    else:
        status = "MERCADO CERRADO — datos del cierre de hoy"
        color  = Fore.YELLOW

    return ts_str, status, color

def print_below20_table(stocks):
    ts_str, status, s_color = market_status_info()
    print(f"\n{Fore.CYAN}Screener: Market Cap >500M | Target +50% | Analyst Buy+ | Price 20% below SMA20 | USA{Style.RESET_ALL}")
    print(f"  Consulta : {ts_str}")
    print(f"  Mercado  : {s_color}{status}{Style.RESET_ALL}")
    print(f"\n  {Fore.YELLOW}Filtros post-screener:{Style.RESET_ALL}")
    print(f"    P/B<3      Precio no mayor de 3x el valor en libros")
    print(f"    Sales>0    Ventas positivas")
    print(f"    Income>0   Beneficio neto positivo")
    print(f"    Index      Cotiza en algún índice (S&P500, RUT, NDX...)")
    print(f"    IPO>=2y    IPO hace mínimo 2 años")

    if not stocks:
        print(f"\n  {Fore.YELLOW}No se encontraron resultados.{Style.RESET_ALL}")
        return

    # Contar cuántos pasan todos los filtros
    all_pass = [s for s in stocks if s.get("ok_pb") and s.get("ok_sales") and s.get("ok_inc") and s.get("ok_idx") and s.get("ok_ipo")]
    print(f"\n  {len(stocks)} resultado(s) del screener  |  {Fore.GREEN}{len(all_pass)} cumple(n) todos los filtros{Style.RESET_ALL}\n")

    # Columnas: datos + 5 validaciones
    columns = [
        "#", "Ticker", "Company", "Sector", "Market Cap", "Price", "Change%",
        "P/B<3", "Sales>0", "Inc>0", "Index", "IPO",
    ]

    # Construir filas con flag all_ok para ordenar
    entries = []
    for s in stocks:
        change_str = s.get("Change%", "")
        try:
            chg = float(change_str.replace("%", "").replace("+", ""))
            chg_color = Fore.GREEN if chg > 0 else Fore.RED if chg < 0 else ""
        except ValueError:
            chg_color = ""

        ok_pb    = s.get("ok_pb", False)
        ok_sales = s.get("ok_sales", False)
        ok_inc   = s.get("ok_inc", False)
        ok_idx   = s.get("ok_idx", False)
        ok_ipo   = s.get("ok_ipo", False)
        all_ok   = ok_pb and ok_sales and ok_inc and ok_idx and ok_ipo

        pb_val    = s.get("P/B", "-")
        idx_val   = s.get("Index", "-")
        ipo_val   = s.get("IPO", "-")

        pb_text    = pb_val if pb_val != "-" else "-"
        sales_text = s.get("Sales", "-")
        inc_text   = s.get("Income", "-")
        idx_text   = idx_val if idx_val != "-" else "-"
        ipo_text   = ipo_val if ipo_val != "-" else "-"

        row = [
            "",  # placeholder para #, se rellena después de ordenar
            s["Ticker"],
            s["Company"][:30],
            s["Sector"][:15],
            s["Market Cap"],
            s["Price"],
            change_str,
            pb_text,
            sales_text,
            inc_text,
            idx_text,
            ipo_text,
        ]
        colors = {
            "chg":   chg_color,
            "pb":    Fore.GREEN if ok_pb    else Fore.RED,
            "sales": Fore.GREEN if ok_sales else Fore.RED,
            "inc":   Fore.GREEN if ok_inc   else Fore.RED,
            "idx":   Fore.GREEN if ok_idx   else Fore.RED,
            "ipo":   Fore.GREEN if ok_ipo   else Fore.RED,
            "all_ok": all_ok,
        }
        entries.append((all_ok, row, colors))

    # Ordenar: las que pasan todos los filtros primero
    entries.sort(key=lambda x: (not x[0], x[1][1]))  # all_ok desc, luego ticker asc

    # Renumerar y asignar colores finales
    rows       = []
    row_colors = []
    for i, (all_ok, row, colors) in enumerate(entries, 1):
        row[0] = str(i)
        rows.append(row)
        row_colors.append(colors)

    # Calcular anchos de columna
    widths = [len(col) for col in columns]
    for row in rows:
        for j, cell in enumerate(row):
            widths[j] = max(widths[j], len(str(cell)))

    # Índices de las columnas de validación
    IDX_CHG   = 6
    IDX_PB    = 7
    IDX_SALES = 8
    IDX_INC   = 9
    IDX_IDX   = 10
    IDX_IPO   = 11

    VAL_COLS = {IDX_PB, IDX_SALES, IDX_INC, IDX_IDX, IDX_IPO}
    GREY = Fore.LIGHTBLACK_EX  # gris para filas que no pasan

    def fmt_header(row):
        return " | ".join(f"{Fore.CYAN}{str(c).center(widths[j])}{Style.RESET_ALL}" for j, c in enumerate(row))

    def fmt_row(row, colors):
        all_ok = colors["all_ok"]
        cells  = []
        for j, c in enumerate(row):
            text = str(c).center(widths[j]) if j in VAL_COLS else str(c).ljust(widths[j])
            if all_ok:
                # Fila que pasa: colores normales en validaciones, ticker en verde
                if   j == IDX_CHG   and colors["chg"]:   text = f"{colors['chg']}{text}{Style.RESET_ALL}"
                elif j in VAL_COLS:                       text = f"{Fore.GREEN}{text}{Style.RESET_ALL}"
                elif j == 1:                              text = f"{Fore.GREEN}{text}{Style.RESET_ALL}"
            else:
                # Fila que no pasa: columnas base en gris, validaciones rojo/verde
                if j in VAL_COLS:
                    color_key = {IDX_PB: "pb", IDX_SALES: "sales", IDX_INC: "inc", IDX_IDX: "idx", IDX_IPO: "ipo"}[j]
                    text = f"{colors[color_key]}{text}{Style.RESET_ALL}"
                elif j == IDX_CHG and colors["chg"]:
                    text = f"{GREY}{text}{Style.RESET_ALL}"
                else:
                    text = f"{GREY}{text}{Style.RESET_ALL}"
            cells.append(text)
        return " | ".join(cells)

    sep = "-+-".join("-" * w for w in widths)
    print(fmt_header(columns))
    print(sep)

    printed_sep = False
    for row, colors in zip(rows, row_colors):
        # Separador visual entre las que pasan y las que no
        if not colors["all_ok"] and not printed_sep:
            print(sep)
            printed_sep = True
        print(fmt_row(row, colors))


# ─── AYUDA ────────────────────────────────────────────────────────────────────

def print_help():
    print(f"""
{Fore.CYAN}{'='*60}
  get-info.py — Herramienta de análisis de acciones
{'='*60}{Style.RESET_ALL}

{Fore.YELLOW}USO:{Style.RESET_ALL}
  python get-info.py [FLAGS] TICKER1 [TICKER2 ... TICKER15]

{Fore.YELLOW}FLAGS:{Style.RESET_ALL}
  --target    Muestra solo la tabla resumen de precios y targets.
              No muestra ratings ni insider trading.

  --news      Muestra la tabla resumen + las últimas 5 noticias
              por ticker extraídas de Finviz.

  --below20   Ejecuta el screener de Finviz con los filtros:
                Market Cap >500M, Target Price +50%, Analyst Buy+,
                Price 20% below SMA20, Country USA.
              No requiere tickers como argumento.

  --help      Muestra esta ayuda.

  (sin flag)  Output completo:
                - Tabla resumen (precio, target, dist%, recom)
                - Historial de ratings de analistas (últimos 5)
                - Insider trading de los últimos 30 días

{Fore.YELLOW}COLUMNAS DE LA TABLA:{Style.RESET_ALL}
  Ticker       Símbolo de la acción
  Index        Índices a los que pertenece (S&P 500, NDX, DJIA)
  Market Cap   Capitalización de mercado
  Price        Precio actual
  Target Price Precio objetivo consenso de analistas
               {Fore.GREEN}Verde{Style.RESET_ALL} = target por encima del precio
               {Fore.RED}Rojo{Style.RESET_ALL}  = target por debajo del precio
  Dist%        Distancia % entre Price y Target Price
               {Fore.YELLOW}Amarillo{Style.RESET_ALL} = distancia >15%
               {Fore.RED}Rojo{Style.RESET_ALL}     = distancia >25%
  Target Date  Fecha en que se detectó el último cambio de target
  Status       SAME = sin cambios desde última ejecución
               {Fore.YELLOW}NEW{Style.RESET_ALL}  = target cambió desde última ejecución
  Recom        Recomendación media de analistas (1=Strong Buy, 5=Sell)
               {Fore.GREEN}Verde{Style.RESET_ALL} = ≤2.0 (Buy/Strong Buy)
               {Fore.RED}Rojo{Style.RESET_ALL}  = >3.0 (Hold/Sell)

{Fore.YELLOW}EJEMPLOS:{Style.RESET_ALL}
  python get-info.py NVDA AMD TSLA
  python get-info.py --target MU NVDA ORCL AMD TSLA GOOGL
  python get-info.py --news ORCL AMD
  python get-info.py --below20
  python get-info.py --help

{Fore.YELLOW}MÓDULOS PYTHON REQUERIDOS:{Style.RESET_ALL}
  finviz          pip install finviz
  requests        pip install requests
  colorama        pip install colorama
  lxml            pip install lxml
  beautifulsoup4  pip install beautifulsoup4

  Instalación rápida de todos:
  {Fore.CYAN}pip install finviz requests colorama lxml beautifulsoup4{Style.RESET_ALL}

{Fore.YELLOW}NOTAS:{Style.RESET_ALL}
  - Máximo 15 tickers por ejecución
  - Tickers duplicados se procesan una sola vez
  - Caché en: %TEMP%\\get_info_target_cache.json
  - Datos obtenidos de Finviz (finviz.com)
  - Insider trading: solo últimos 30 días
""")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if "--help" in args:
        print_help()
        sys.exit(0)

    target_only = "--target"  in args
    news_only   = "--news"    in args
    below20     = "--below20" in args
    args = [a for a in args if a not in ("--target", "--news", "--below20")]

    # Modo --below20: screener independiente, no necesita tickers
    if below20:
        print(f"{Fore.CYAN}Consultando screener Finviz...{Style.RESET_ALL}")
        stocks = fetch_below20_screener()
        print_below20_table(stocks)
        sys.exit(0)

    tickers = args

    if not tickers:
        print("Uso: python get-info.py [--target] [--news] [--below20] [--help] TICKER1 [TICKER2 ... TICKER15]")
        print("  Usa --help para ver la ayuda completa.")
        sys.exit(1)

    if len(tickers) > 15:
        print(f"Solo se permiten hasta 15 tickers. Has pasado {len(tickers)}.")
        sys.exit(1)

    # Deduplicar manteniendo orden
    seen, tickers_dedup = set(), []
    for t in tickers:
        if t.upper() not in seen:
            seen.add(t.upper())
            tickers_dedup.append(t.upper())
    tickers = tickers_dedup

    # Obtener datos
    tickers_data = []
    target_dates = {}
    for ticker in tickers:
        try:
            data = finviz.get_stock(ticker)
        except Exception:
            data = None
        tickers_data.append((ticker, data))
        target_dates[ticker] = get_latest_target_date(ticker)

    # Status SAME/NEW por fecha de target
    cache        = load_cache()
    target_status = {}
    for ticker, data in tickers_data:
        if data is None:
            target_status[ticker] = "SAME"
            continue
        current_date = target_dates.get(ticker, "")
        prev_date    = cache.get(ticker, {}).get("date", "")
        if prev_date and current_date and current_date != prev_date:
            target_status[ticker] = "NEW"
        else:
            target_status[ticker] = "SAME"

    # Actualizar caché
    new_cache = dict(cache)
    for ticker, data in tickers_data:
        if data is None:
            continue
        new_cache[ticker] = {
            "target": data.get('Target Price', ''),
            "date":   target_dates.get(ticker, ''),
        }
    save_cache(new_cache)

    # Tabla principal (siempre)
    print_main_table(tickers_data, target_dates=target_dates, target_status=target_status)

    # Modo --target: solo tabla
    if target_only:
        return

    # Modo --news: tabla + noticias
    if news_only:
        for ticker, data in tickers_data:
            if data is None:
                continue
            print()
            print(f"Últimas noticias - {ticker}")
            news, err = fetch_news(ticker, count=5)
            if err:
                print(f"  Error: {err}")
            else:
                print_news(ticker, news)
        return

    # Modo completo: ratings + insider
    for ticker, data in tickers_data:
        if data is None:
            continue
        print()
        print(f"Historial de ratings - {ticker}")
        print_ratings_history(ticker, last_ratings=5)

    for ticker, data in tickers_data:
        if data is None:
            continue
        print()
        print(f"Insider Trading (resumen) - {ticker}")
        trades, err = fetch_insider_trades(ticker)
        if err:
            print(f"  Error: {err}")
        else:
            print_insider_summary(ticker, trades)

    for ticker, data in tickers_data:
        if data is None:
            continue
        print()
        print(f"Últimas noticias - {ticker}")
        news, err = fetch_news(ticker, count=15)
        if err:
            print(f"  Error: {err}")
        else:
            print_news(ticker, news)

if __name__ == "__main__":
    main()