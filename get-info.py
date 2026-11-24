import sys

# Verificación de módulos requeridos
_modulos_requeridos = {
    "finviz":   "finviz",
    "requests": "requests",
    "colorama": "colorama",
    "lxml":     "lxml",
    "bs4":      "beautifulsoup4",
    "yfinance": "yfinance",
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

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN --below20 / --exp20   (EDITA AQUÍ LOS FILTROS)
# ══════════════════════════════════════════════════════════════════════════════
# ── 1) Filtros que aplica FINVIZ en la URL del screener ──────────────────────
#    Códigos de Finviz. Si un código no existe, Finviz lo ignora sin dar error.
B20_FINVIZ_FILTERS = [
    "cap_smallover",       # Market Cap > 300M (cap_microover = >50M)
    "targetprice_a50",     # Target Price >= 50% por encima del precio
    "an_recom_buybetter",  # Recomendación analistas: Buy o mejor
    "ta_sma20_pb20",       # Precio >= 20% por debajo del SMA20
    "geo_usa",             # País: USA
]
B20_FINVIZ_ORDER = "-marketcap"     # Orden del screener (mayor Market Cap primero)

# ── 2) Filtros locales EXCLUYENTES (la acción desaparece de la tabla) ────────
B20_MIN_MARKET_CAP_M = 500          # Market Cap mínimo, en millones (>=)
B20_SECTORS = {                     # Sectores permitidos (minúsculas).
    "technology",                   # set() vacío = TODOS los sectores.
    "energy",
    "communication services",
    "consumer cyclical",
}

# ── 3) Filtros locales de VALIDACIÓN (si falla -> grupo gris, no se elimina) ─
B20_MAX_PB        = 3.0             # P/B  <  valor
B20_MIN_SALES     = 0               # Sales  >  valor
B20_MIN_INCOME    = 0               # Income >  valor
B20_REQUIRE_INDEX = True            # True = debe cotizar en algún índice
B20_INDEX_EXCLUDE = True            # True  = ELIMINA del listado las que no
                                    #         cotizan en ningún índice
                                    # False = se muestran (en gris si no cumplen)
B20_MIN_IPO_YEARS = 2               # IPO hace al menos N años (>=)

# ── 4) Diagnóstico ───────────────────────────────────────────────────────────
B20_DEBUG = False                   # True = muestra URL, HTTP, filas por página

# ── 5) Filtro de TENDENCIA en temporalidad DIARIA (D) ────────────────────────
#    Se evalúa en la vela diaria ANTERIOR al día de referencia.
B20_TREND_FILTER   = True           # True = activar filtro de tendencia D
B20_TREND_EXCLUDE  = False           # True = elimina las que no cumplen
                                    # False = solo las marca (columna "Tend D")
B20_TREND_REF      = "caida"        # "caida"  = día de mayor caída (Close vs Close
                                    #            anterior) en los últimos N días
                                    # "ultimo" = última vela diaria cerrada
B20_TREND_LOOKBACK = 10             # N días hábiles para buscar el día de caída
# Regla de tendencia (tu metodología: comparar DENTRO de cada par)
B20_TREND_PAIR_20_40   = True       # exige MA20 > MA40
B20_TREND_PAIR_100_200 = True       # exige MA100 > MA200
B20_TREND_FULL_ORDER   = False      # True = exige además 20>40>100>200
B20_TREND_SLOPE_DAYS   = 0          # >0 = exige que las 4 medias SUBAN respecto
                                    # a N días antes (0 = no se exige pendiente)
# Ventana de tendencia ANTES de la caída (temporalidad D)
B20_TREND_WINDOW_DAYS = 21          # velas D a revisar: 1 = solo la vela previa
                                    # 21 ≈ 1 mes | 42 ≈ 2 meses
B20_TREND_MIN_PCT     = 100         # % de velas de la ventana que deben cumplir
                                    # 100 = TODAS | 80 = tolera algún día suelto

# Colores columna "Tend D": verde = cumple | amarillo = n_ok >= este valor
# (sin llegar a cumplir) | gris = por debajo
B20_TREND_YELLOW_MIN  = 10

# ── 6) Columna VolCaida (INFORMATIVA, no filtra) ─────────────────────────────
#    Día de caída = mismo que "Tend D" (mayor caída Close vs Close anterior en
#    los últimos B20_TREND_LOOKBACK días). Cumple si la vela es roja
#    (Close < Close anterior) y su volumen > media de los N días ANTERIORES x factor.
B20_VOLCAIDA_BASE   = 20            # días previos para la media de volumen
B20_VOLCAIDA_FACTOR = 1.0           # 1.0 = superior a la media | 1.5 | 2.0
# ══════════════════════════════════════════════════════════════════════════════


# ─── CACHÉ ────────────────────────────────────────────────────────────────────

def get_cache_path():
    return os.path.join(tempfile.gettempdir(), "get_info_target_cache.json")

def get_screener_cache_path():
    return os.path.join(tempfile.gettempdir(), "get_info_screener_tickers.json")

def load_screener_cache():
    path = get_screener_cache_path()
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_screener_cache(tickers_set):
    try:
        with open(get_screener_cache_path(), "w") as f:
            json.dump(list(tickers_set), f, indent=2)
    except Exception:
        pass

def get_caida_vol_pct(ticker):
    """
    Usa yfinance para obtener el histórico de los últimos 30 días.
    Identifica el día de mayor caída (%) en los últimos 10 días de trading.
    Calcula el volumen de ese día vs la media de los 20 días anteriores.
    Devuelve (pct, fecha_str) o (None, None) si no hay datos.
    """
    try:
        import yfinance as yf
        df = yf.download(ticker, period="40d", interval="1d", progress=False, auto_adjust=True)
        if df is None or len(df) < 5:
            return None, None

        # Aplanar columnas MultiIndex si las hay
        if hasattr(df.columns, 'levels'):
            df.columns = df.columns.get_level_values(0)

        df = df.dropna(subset=["Close", "Open", "Volume"])
        if len(df) < 5:
            return None, None

        # Últimos 10 días de trading
        recientes = df.iloc[-10:].copy()

        # Día de MAYOR VOLUMEN en los últimos 10 días → ese es el día del evento
        idx_caida = recientes["Volume"].idxmax()
        row_caida = recientes.loc[idx_caida]

        vol_caida = float(row_caida["Volume"])
        fecha_caida = idx_caida.strftime("%d/%m") if hasattr(idx_caida, 'strftime') else str(idx_caida)[:10]

        # Media de volumen de los 20 días ANTERIORES al día de caída
        pos = df.index.get_loc(idx_caida)
        inicio = max(0, pos - 20)
        previos = df.iloc[inicio:pos]
        if len(previos) < 3:
            return None, None

        avg_vol = float(previos["Volume"].mean())
        if avg_vol == 0:
            return None, None

        pct = ((vol_caida - avg_vol) / avg_vol) * 100
        return pct, fecha_caida

    except Exception:
        return None, None

def get_sma_distances(ticker):
    """
    Con UNA sola descarga de yfinance calcula la distancia del precio al SMA20 y
    al SMA200. Devuelve un dict:
        {"dist20": float|None, "dist200": float|None, "n_dias": int}
    - dist20 / dist200 en % (negativo = precio por DEBAJO de la media).
    - dist200 es None si la acción tiene menos de 200 días de cotización
      (IPO reciente) -> en la tabla se marcará en gris.
    """
    out = {"dist20": None, "dist200": None, "n_dias": 0}
    try:
        import yfinance as yf
        # 300 días naturales -> ~200+ días hábiles, suficiente para el SMA200
        df = yf.download(ticker, period="300d", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or len(df) == 0:
            return out

        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Close"])

        n = len(df)
        out["n_dias"] = n
        if n < 20:
            return out

        price = float(df["Close"].iloc[-1])
        if price == 0:
            return out

        sma20 = float(df["Close"].iloc[-20:].mean())
        if sma20:
            out["dist20"] = ((price - sma20) / sma20) * 100

        # SMA200 solo si hay al menos 200 cierres (si no, queda None -> gris)
        if n >= 200:
            sma200 = float(df["Close"].iloc[-200:].mean())
            if sma200:
                out["dist200"] = ((price - sma200) / sma200) * 100

        return out
    except Exception:
        return out


def get_daily_trend(ticker):
    """
    Tendencia en temporalidad DIARIA con MA20/40/100/200 (simples, sobre Close)
    evaluada en la vela ANTERIOR al día de referencia (B20_TREND_REF).
    Devuelve dict: {"ok": bool|None, "ref": "dd/mm", "eval": "dd/mm", "txt": str}
    ok=None -> sin datos suficientes (menos de 200 velas antes del día).
    """
    out = {"ok": None, "ref": "", "eval": "", "txt": "n/d"}
    try:
        import yfinance as yf
        df = yf.download(ticker, period="2y", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or len(df) == 0:
            return out
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Close"])
        close = df["Close"]
        n = len(close)
        if n < 3:
            return out

        # Día de referencia
        if B20_TREND_REF == "ultimo":
            ref_pos = n - 1
        else:
            chg = close.pct_change()
            ventana = chg.iloc[-B20_TREND_LOOKBACK:]
            ref_pos = close.index.get_loc(ventana.idxmin())
        eval_pos = ref_pos - 1                     # vela anterior al día
        out["ref"]  = close.index[ref_pos].strftime("%d/%m")
        out["eval"] = close.index[eval_pos].strftime("%d/%m")

        win  = max(1, int(B20_TREND_WINDOW_DAYS))
        need = 200 + max(0, B20_TREND_SLOPE_DAYS) + (win - 1)
        if eval_pos + 1 < need:
            out["txt"] = f"n/d (<{need}v)"
            return out

        ma = {p: close.rolling(p).mean() for p in (20, 40, 100, 200)}

        def cumple(pos):
            v = {p: float(ma[p].iloc[pos]) for p in ma}
            checks = []
            if B20_TREND_PAIR_20_40:
                checks.append(v[20] > v[40])
            if B20_TREND_PAIR_100_200:
                checks.append(v[100] > v[200])
            if B20_TREND_FULL_ORDER:
                checks.append(v[20] > v[40] > v[100] > v[200])
            if B20_TREND_SLOPE_DAYS > 0:
                k = B20_TREND_SLOPE_DAYS
                checks.append(all(v[p] > float(ma[p].iloc[pos - k]) for p in ma))
            return all(checks) if checks else True

        # Revisar todas las velas D de la ventana: [eval_pos - win + 1, eval_pos]
        ini = eval_pos - win + 1
        oks = [cumple(pos) for pos in range(ini, eval_pos + 1)]
        n_ok = sum(oks)
        pct  = 100.0 * n_ok / win
        ok   = pct >= B20_TREND_MIN_PCT and oks[-1]   # la vela previa SIEMPRE debe cumplir
        out["ok"]   = ok
        out["n_ok"] = n_ok
        out["desde"] = close.index[ini].strftime("%d/%m")
        out["txt"]  = (("✓ " if ok else "✗ ")
                       + f"{n_ok}/{win} ({out['desde']}-{out['eval']})")
        return out
    except Exception:
        return out


def get_vol_caida(ticker):
    """
    Volumen del DÍA DE CAÍDA (mismo día que usa Tend D) frente a la media de
    volumen de los B20_VOLCAIDA_BASE días anteriores (sin incluir ese día).
    Devuelve dict: {"ok": bool|None, "txt": str}  (ok=None -> sin datos)
    """
    out = {"ok": None, "txt": "n/d"}
    try:
        import yfinance as yf
        df = yf.download(ticker, period="6mo", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or len(df) == 0:
            return out
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Close", "Volume"])
        close, vol = df["Close"], df["Volume"]
        if len(close) < 3:
            return out
        chg = close.pct_change()
        ventana = chg.iloc[-B20_TREND_LOOKBACK:]
        pos = close.index.get_loc(ventana.idxmin())
        fecha = close.index[pos].strftime("%d/%m")
        previos = vol.iloc[max(0, pos - B20_VOLCAIDA_BASE):pos]
        if len(previos) < 3:
            return out
        avg = float(previos.mean())
        if avg == 0:
            return out
        v = float(vol.iloc[pos])
        roja = float(close.iloc[pos]) < float(close.iloc[pos - 1])
        pct = (v - avg) / avg * 100
        ok = roja and v > avg * B20_VOLCAIDA_FACTOR
        out["ok"] = ok
        out["txt"] = ("✓ " if ok else "✗ ") + f"{pct:+.0f}% ({fecha})" + ("" if roja else " verde")
        return out
    except Exception:
        return out


def get_dist_sma20(ticker):
    """Compatibilidad: devuelve solo la distancia al SMA20 (usa get_sma_distances)."""
    return get_sma_distances(ticker)["dist20"]

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

# ─── CLASIFICACIÓN DE TITULARES (rebote técnico vs daño fundamental) ───────────
# Palabras clave en INGLÉS (los titulares de Finviz vienen en inglés antes de traducir).
TRAMPA_KW = [   # 🔴 daño fundamental -> NO rebota fácil
    "regulation", "regulatory", "fhfa", "lawsuit", "sued", "sues", "investigation",
    "sec charges", "fraud", "probe", "subpoena", "downgrade", "cuts rating",
    "lowered", "misses", "miss", "guidance cut", "cuts guidance", "lowers guidance",
    "profit warning", "recall", "trial fail", "failed trial", "fails", "halt",
    "delisting", "bankruptcy", "going concern", "restates", "restatement",
    "loses contract", "patent", "competition", "market share", "dilution", "offering",
]
REBOTE_KW = [   # 🟢 técnico / positivo -> rebote probable
    "beats", "beat", "tops", "raises guidance", "raised", "upgrade", "upgraded",
    "initiated", "buy rating", "outperform", "overweight", "price target raised",
    "profit-taking", "profit taking", "oversold", "undervalued", "dividend",
    "buyback", "repurchase", "record revenue", "strong quarter", "expands", "launches",
]
DEPENDE_KW = [  # 🟡 depende -> revisar
    "acquisition", "acquire", "acquires", "merger", "takeover", "to buy", "deal",
    "stake", "explores", "talks",
]

def classify_headline(headline):
    """Devuelve 'trampa', 'rebote', 'depende' o '' según palabras clave del titular."""
    h = (headline or "").lower()
    for kw in TRAMPA_KW:
        if kw in h:
            return "trampa"
    for kw in REBOTE_KW:
        if kw in h:
            return "rebote"
    for kw in DEPENDE_KW:
        if kw in h:
            return "depende"
    return ""

# Fuentes que son comunicados de la propia empresa (autobombo), no prensa/analistas.
PR_SOURCES = {
    "PR Newswire", "Business Wire", "Businesswire", "GlobeNewswire",
    "Globe Newswire", "Accesswire", "ACCESSWIRE", "Newsfile", "GuruFocus",
    "PRNewswire", "EIN Presswire", "Globenewswire",
}

def _parse_news_datetime(date_str, time_str):
    """Convierte 'Sep-28-26' + '05:11PM' a datetime para poder ordenar. Hoy si falla."""
    import datetime
    raw = f"{date_str} {time_str}".strip()
    for fmt in ("%b-%d-%y %I:%M%p", "%b-%d-%Y %I:%M%p"):
        try:
            return datetime.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    # Solo hora (misma fecha que la última conocida ya viene resuelta en date_str)
    return datetime.datetime.min

def fetch_news(ticker, count=5):
    """
    Descarga las noticias de Finviz para el ticker.
    MEJORAS v3:
      - Lee TODA la tabla (no corta a las 5 primeras) y luego ordena por fecha real,
        así no se pierde lo más reciente por venir más abajo en el HTML.
      - Parsea la fecha a datetime para ordenar de más nuevo a más viejo.
      - Marca is_pr=True si la fuente es un comunicado de empresa (Business Wire, etc.).
    """
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
        # Recorremos TODAS las filas, sin cortar (el corte se hace tras ordenar)
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

            # El source viene con paréntesis de Finviz, ej. "(Business Wire)".
            # Los quitamos para comparar y mostrar limpio.
            source = source.strip().lstrip("(").rstrip(")").strip()

            dt = _parse_news_datetime(current_date, time_str)
            is_pr = source in PR_SOURCES
            news.append({
                "date": current_date, "time": time_str,
                "headline": headline, "source": source,
                "dt": dt, "is_pr": is_pr,
                "tag": classify_headline(headline),
                "origin": "Finviz",
            })

        # Ordenar de más reciente a más antiguo (el corte final se hace tras fusionar)
        news.sort(key=lambda x: x["dt"], reverse=True)
    except Exception as e:
        return None, f"Error parseando noticias: {e}"

    return news, None

def fetch_news_yahoo(ticker, count=15):
    """
    Noticias desde Yahoo Finance vía yfinance (gratis, sin key).
    Complementa a Finviz: trae fuentes más analíticas (StockStory, Simply Wall St,
    Zacks...) y a veces más recientes. Devuelve lista en el mismo formato que fetch_news.
    yfinance 1.7+ usa formato nuevo: todo dentro de item['content'].
    """
    import datetime
    try:
        import yfinance as yf
    except ImportError:
        return []

    try:
        t = yf.Ticker(ticker)
        raw = None
        try:
            raw = t.news
        except Exception:
            pass
        if not raw:
            try:
                raw = t.get_news()
            except Exception:
                raw = []
        if not raw:
            return []
    except Exception:
        return []

    news = []
    for item in raw[:count]:
        c = item.get("content", item)  # formato nuevo -> content; viejo -> plano
        title = c.get("title") or item.get("title") or ""
        if not title:
            continue

        # Fecha: pubDate ISO (nuevo) o providerPublishTime epoch (viejo)
        dt = datetime.datetime.min
        pub = c.get("pubDate") or c.get("displayTime")
        if pub:
            try:
                dt = datetime.datetime.fromisoformat(pub.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                pass
        elif item.get("providerPublishTime"):
            try:
                dt = datetime.datetime.fromtimestamp(item["providerPublishTime"])
            except Exception:
                pass

        # Fuente
        source = ""
        if isinstance(c.get("provider"), dict):
            source = c["provider"].get("displayName", "")
        source = source or item.get("publisher", "")

        news.append({
            "date": dt.strftime("%b-%d-%y") if dt != datetime.datetime.min else "",
            "time": dt.strftime("%I:%M%p")  if dt != datetime.datetime.min else "",
            "headline": title.strip(),
            "source": source.strip(),
            "dt": dt,
            "is_pr": source.strip() in PR_SOURCES,
            "tag": classify_headline(title),
            "origin": "Yahoo",
        })
    return news


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


def print_news_links(ticker):
    """Imprime enlaces con el ticker anclado en la URL, para noticias de HOY
    (intradía) que las fuentes automáticas no capturan. Cada ticker -> sus enlaces."""
    from urllib.parse import quote_plus
    t = ticker.upper()
    google = f"https://news.google.com/search?q={quote_plus(t + ' stock')}&hl=en-US&gl=US"
    mbeat  = f"https://www.marketbeat.com/stocks/NASDAQ/{t}/news/"
    invest = f"https://www.investing.com/search/?q={quote_plus(t)}"
    yahoo  = f"https://finance.yahoo.com/quote/{t}/news"
    print(f"  {Fore.CYAN}🔗 Noticias de HOY (abrir en navegador):{Style.RESET_ALL}")
    print(f"     Google News: {google}")
    print(f"     MarketBeat : {mbeat}")
    print(f"     Investing  : {invest}")
    print(f"     Yahoo      : {yahoo}")


def _norm_headline(h):
    """Normaliza un titular para detectar duplicados entre fuentes."""
    import re
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())[:50]

def get_merged_news(ticker, count=8):
    """
    Fusiona noticias de Finviz + Yahoo, deduplica por titular, ordena por fecha
    (más reciente primero) y devuelve (lista, err_finviz).
    """
    finviz_news, err = fetch_news(ticker, count=40)
    finviz_news = finviz_news or []
    yahoo_news  = fetch_news_yahoo(ticker, count=20)

    merged = []
    seen   = set()
    for item in (finviz_news + yahoo_news):
        key = _norm_headline(item["headline"])
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)

    import datetime
    merged.sort(key=lambda x: x.get("dt") or datetime.datetime.min, reverse=True)
    return merged[:count], err


def print_news(ticker, news):
    if not news:
        print("  Sin noticias en fuentes automáticas (Finviz/Yahoo).")
        print()
        print_news_links(ticker)
        return

    # Aviso de antigüedad: si la noticia más reciente es de hace días, avisar.
    import datetime
    newest = news[0].get("dt")
    if newest and newest != datetime.datetime.min:
        dias = (datetime.datetime.now() - newest).days
        if dias >= 3:
            print(f"  {Fore.YELLOW}⚠️  La noticia más reciente es de hace {dias} días "
                  f"— puede faltar lo de hoy (busca en MarketBeat/Google para intradía).{Style.RESET_ALL}")
        else:
            print(f"  {Fore.GREEN}✓ Feed al día (última noticia hace {dias} día(s)).{Style.RESET_ALL}")
        print()

    # Etiquetas de clasificación con color
    TAG_STYLE = {
        "trampa":  (Fore.RED,    "🔴 TRAMPA"),
        "rebote":  (Fore.GREEN,  "🟢 TÉCNICA"),
        "depende": (Fore.YELLOW, "🟡 DEPENDE"),
        "":        ("",          "          "),
    }

    headlines    = [item['headline'] for item in news]
    translations = translate_headlines(headlines)
    for item, translated in zip(news, translations):
        date_str = f"{item['date']} {item['time']}".strip()
        pr_mark  = f"{Fore.MAGENTA}[PR]{Style.RESET_ALL} " if item.get("is_pr") else "    "
        color, label = TAG_STYLE.get(item.get("tag", ""), ("", "          "))
        tag_txt  = f"{color}{label}{Style.RESET_ALL}"
        origin   = item.get("origin", "")
        origin_txt = f"{Fore.BLUE}{origin[:1]}{Style.RESET_ALL}" if origin else " "  # F o Y
        source   = f"({item['source']})" if item['source'] else ""
        print(f"  {Fore.CYAN}{date_str:<16}{Style.RESET_ALL} {origin_txt} {tag_txt}  {pr_mark}{translated}  {source}")

    # Leyenda
    print()
    print(f"  {Fore.BLUE}F{Style.RESET_ALL}=Finviz  {Fore.BLUE}Y{Style.RESET_ALL}=Yahoo  ·  "
          f"{Fore.MAGENTA}[PR]{Style.RESET_ALL} = comunicado de la empresa (autobombo)")
    print(f"  {Fore.GREEN}🟢 TÉCNICA{Style.RESET_ALL} posible rebote  ·  "
          f"{Fore.RED}🔴 TRAMPA{Style.RESET_ALL} daño de fondo  ·  "
          f"{Fore.YELLOW}🟡 DEPENDE{Style.RESET_ALL} revisar")
    print()
    print_news_links(ticker)


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
            "Recom":      data.get("Recom", ""),
            "Book/sh":    data.get("Book/sh", ""),
            "Sales":      data.get("Sales", ""),
            "Income":     data.get("Income", ""),
            "Index":      data.get("Index", ""),
            "IPO":        data.get("IPO", ""),
            "Avg Volume": data.get("Avg Volume", ""),
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

def fetch_quote_snapshot(ticker):
    """Devuelve la tabla snapshot completa de la página quote de Finviz (dict)."""
    from lxml import html as lxml_html
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://finviz.com/",
    }
    data = {}
    try:
        r = requests.get(f"https://finviz.com/quote.ashx?t={ticker.upper()}&p=d",
                         headers=headers, timeout=15)
        tree = lxml_html.fromstring(r.content)
        for tr in tree.xpath('//table[contains(@class,"snapshot-table2")]//tr'):
            tds = tr.xpath('.//td')
            for i in range(0, len(tds) - 1, 2):
                k = tds[i].text_content().strip()
                if k:
                    data[k] = tds[i + 1].text_content().strip()
    except Exception:
        pass
    return data


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
    import re

    query = ("?v=111"
             "&f=" + ",".join(B20_FINVIZ_FILTERS) +
             "&o=" + B20_FINVIZ_ORDER +
             "&ft=4")
    # Finviz ha migrado a /screener/stocks; se mantiene el antiguo como respaldo.
    url_candidates = [
        "https://finviz.com/screener/stocks" + query,
        "https://finviz.com/screener.ashx" + query,
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer":         "https://finviz.com/",
    }

    # Orden de columnas de la vista v=111 (fijo, lo define Finviz).
    # Centralizado aquí para no usar índices "mágicos" sueltos por el código.
    # OJO: esta tabla NO trae fila de cabecera; el primer <tr> ya es un dato.
    COL = {
        "no": 0, "ticker": 1, "company": 2, "sector": 3, "industry": 4,
        "country": 5, "market_cap": 6, "pe": 7, "price": 8, "change": 9, "volume": 10,
    }

    def ticker_from_row(tr):
        """Ticker limpio sacado del href (?t=XXX), o None si la fila no es de datos."""
        for a in tr.xpath('.//a[@href]'):
            href = a.get("href", "")
            if "t=" in href:
                m = re.search(r'[?&]t=([A-Za-z0-9.\-]+)', href)
                if m:
                    return m.group(1).upper()
        return None

    def header_map(tree):
        """Localiza la fila de cabecera (No./Ticker/.../Volume) y devuelve
        {nombre_columna: índice}. Si no la encuentra, usa el orden fijo COL."""
        for tr in tree.xpath('//tr'):
            cells = [c.text_content().strip().lower() for c in tr.xpath('./th|./td')]
            if "ticker" in cells and "market cap" in cells and "price" in cells:
                def idx(name, default):
                    return cells.index(name) if name in cells else default
                return {
                    "ticker": idx("ticker", COL["ticker"]),
                    "company": idx("company", COL["company"]),
                    "sector": idx("sector", COL["sector"]),
                    "market_cap": idx("market cap", COL["market_cap"]),
                    "price": idx("price", COL["price"]),
                    "change": idx("change", COL["change"]),
                    "volume": idx("volume", COL["volume"]),
                }
        return dict(COL)

    def total_from_page(tree):
        """Lee '#1 / 37 Total' de la página. None si no aparece."""
        txt = " ".join(tree.xpath('//*[@id="screener-total"]//text()'))
        if not txt:
            txt = " ".join(tree.xpath('//*[contains(text(),"Total")]/text()'))
        m = re.search(r'/\s*([\d,]+)\s*Total', txt)
        return int(m.group(1).replace(",", "")) if m else None

    # ── Parser del screener nuevo de Finviz (React/SSR) ─────────────────────
    # La página ya no trae <tr>: las filas vienen como objetos JSON con
    # "ticker":"XXX" dentro de un <script>. Se extrae cada objeto completo y se
    # mapean sus claves por nombre. Si falta algún campo se completa después.
    import json as _json

    def _enclosing_object(text, pos):
        """Devuelve el objeto JSON {...} más interno que contiene la posición pos."""
        depth, i = 0, pos
        while i >= 0:                      # retroceder hasta la '{' que abre
            ch = text[i]
            if ch == '}':
                depth += 1
            elif ch == '{':
                if depth == 0:
                    break
                depth -= 1
            i -= 1
        if i < 0:
            return None
        start, depth, in_str, esc = i, 0, False, False
        for j in range(start, len(text)):  # avanzar hasta la '}' que cierra
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return _json.loads(text[start:j + 1])
                    except Exception:
                        return None
        return None

    def _pick(obj, *names):
        """Busca una clave por nombre, sin distinguir mayúsculas ni '_'/' '."""
        norm = {re.sub(r'[^a-z0-9]', '', str(k).lower()): v for k, v in obj.items()}
        for n in names:
            v = norm.get(re.sub(r'[^a-z0-9]', '', n.lower()))
            if v not in (None, ""):
                return v
        return None

    def _fmt_cap(v):
        """Market Cap a texto tipo Finviz ('1.23B', '487.51M')."""
        if isinstance(v, (int, float)):
            m = float(v)
            if m > 1e6:            # viene en unidades -> pasar a millones
                m = m / 1e6
            return f"{m/1000:.2f}B" if m >= 1000 else f"{m:.2f}M"
        return str(v)

    def _fmt_chg(v):
        if isinstance(v, (int, float)):
            pct = v * 100 if abs(v) < 1 else v
            return f"{pct:.2f}%"
        return str(v)

    def rows_from_json(text):
        out = []
        for m in re.finditer(r'"ticker"\s*:\s*"([A-Za-z0-9.\-]+)"', text):
            obj = _enclosing_object(text, m.start())
            if not isinstance(obj, dict):
                obj = {}
            out.append((m.group(1).upper(), obj))
        return out

    def rows_from_links(text):
        return [(t.upper(), {}) for t in re.findall(r'/quote\?t=([A-Za-z0-9.\-]+)', text)]

    stocks  = []
    seen    = set()
    session = requests.Session()
    debug_keys_shown = False

    # Elegir la URL que responda (URL nueva primero, antigua como respaldo)
    url = None
    for cand in url_candidates:
        try:
            r = session.get(cand + "&r=1", headers=headers, timeout=20)
            if B20_DEBUG:
                print(f"  [debug] {cand}&r=1 -> HTTP {r.status_code}, {len(r.content)} bytes")
            if r.status_code == 200 and ("/quote?t=" in r.text or "quote.ashx?t=" in r.text
                                         or '"ticker"' in r.text):
                url = cand
                break
        except Exception as e:
            if B20_DEBUG:
                print(f"  [debug] {cand} -> {e}")
    if url is None:
        print(f"  {Fore.RED}Finviz no ha devuelto la página del screener "
              f"(posible bloqueo 403/429 o cambio de URL). Activa B20_DEBUG=True.{Style.RESET_ALL}")
        return []

    total_finviz = None
    page = 1
    while True:
        paged_url = url + f"&r={1 + (page - 1) * 20}"
        try:
            resp = session.get(paged_url, headers=headers, timeout=20)
            resp.raise_for_status()
        except Exception as e:
            print(f"  {Fore.RED}Error al conectar con Finviz: {e}{Style.RESET_ALL}")
            break

        text = resp.text
        tree = lxml_html.fromstring(resp.content)
        if total_finviz is None:
            total_finviz = total_from_page(tree)
            if total_finviz is None:
                mt = re.search(r'"(?:totalCount|total|count)"\s*:\s*(\d+)', text)
                total_finviz = int(mt.group(1)) if mt else None

        # 1º JSON embebido; 2º enlaces /quote?t= ; 3º tabla HTML clásica
        candidates = rows_from_json(text) or rows_from_links(text)
        if not candidates:
            cmap = header_map(tree)
            need = max(cmap.values())
            for tr in tree.xpath('//tr'):
                tds = tr.xpath('./td')
                if len(tds) <= need:
                    continue
                t = ticker_from_row(tds[cmap["ticker"]]) or ticker_from_row(tr)
                if t:
                    cols = [td.text_content().strip() for td in tds]
                    candidates.append((t, {
                        "company": cols[cmap["company"]], "sector": cols[cmap["sector"]],
                        "marketCap": cols[cmap["market_cap"]], "price": cols[cmap["price"]],
                        "change": cols[cmap["change"]], "volume": cols[cmap["volume"]],
                    }))

        added_this_page = 0
        for ticker, obj in candidates:
            if ticker in seen:
                continue
            if B20_DEBUG and obj and not debug_keys_shown:
                print(f"  [debug] claves JSON de {ticker}: {list(obj.keys())}")
                debug_keys_shown = True
            seen.add(ticker)
            cap = _pick(obj, "marketCap", "market_cap", "Market Cap", "mktCap")
            chg = _pick(obj, "change", "changePct", "change%", "Change", "perfDay")
            stocks.append({
                "Ticker":     ticker,
                "Company":    str(_pick(obj, "company", "companyName", "name") or ""),
                "Sector":     str(_pick(obj, "sector", "Sector") or ""),
                "Market Cap": _fmt_cap(cap) if cap is not None else "",
                "Price":      str(_pick(obj, "price", "close", "last", "Price") or ""),
                "Change%":    _fmt_chg(chg) if chg is not None else "",
                "Volume":     str(_pick(obj, "volume", "Volume") or ""),
            })
            added_this_page += 1

        if B20_DEBUG:
            print(f"  [debug] página {page}: {added_this_page} filas (Total Finviz: {total_finviz})")

        if added_this_page == 0:
            break
        if total_finviz is not None and len(stocks) >= total_finviz:
            break
        if total_finviz is None and added_this_page < 20:
            break
        page += 1

    # ── Completar campos que no vengan en el JSON (quote de Finviz / yfinance)
    faltan = [s for s in stocks if not (s["Sector"] and s["Market Cap"] and s["Price"])]
    if faltan:
        print(f"  Completando datos de {len(faltan)} ticker(s)...")
    for s in faltan:
        snap = fetch_quote_snapshot(s["Ticker"])
        s["Market Cap"] = s["Market Cap"] or snap.get("Market Cap", "")
        s["Price"]      = s["Price"]      or snap.get("Price", "")
        s["Change%"]    = s["Change%"]    or snap.get("Change", "")
        s["Volume"]     = s["Volume"]     or snap.get("Volume", "")
        if not s["Sector"] or not s["Company"]:
            try:
                import yfinance as yf
                info = yf.Ticker(s["Ticker"]).info or {}
                s["Sector"]  = s["Sector"]  or info.get("sector", "")
                s["Company"] = s["Company"] or info.get("shortName", "")
            except Exception:
                pass

    n_finviz = len(stocks)
    if total_finviz is not None and n_finviz != total_finviz:
        print(f"  {Fore.YELLOW}Aviso: Finviz indica {total_finviz} y se han leído {n_finviz}.{Style.RESET_ALL}")

    # Filtro 1: Market Cap >= B20_MIN_MARKET_CAP_M
    stocks = [
        s for s in stocks
        if parse_market_cap_value(s["Market Cap"]) >= B20_MIN_MARKET_CAP_M
    ]
    n_cap = len(stocks)

    # Filtro 2: limitar el screener a los sectores seleccionados.
    # Se aplica fuera del bucle de paginacion para filtrar el resultado completo.
    allowed_sectors = {x.strip().casefold() for x in B20_SECTORS}
    if allowed_sectors:
        stocks = [
            s for s in stocks
            if s.get("Sector", "").strip().casefold() in allowed_sectors
        ]
    print(f"  Finviz: {n_finviz} | tras Market Cap>={B20_MIN_MARKET_CAP_M}M: {n_cap} "
          f"| tras sectores: {len(stocks)}")

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
            s["VolPct"]   = None
            s["VolFecha"] = None
            _sma = get_sma_distances(ticker)
            s["DistSMA20"]  = _sma["dist20"]
            s["DistSMA200"] = _sma["dist200"]
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
                cutoff   = datetime.date.today() - datetime.timedelta(days=int(round(365.25 * B20_MIN_IPO_YEARS)))
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
        s["ok_pb"]    = pb is not None and pb < B20_MAX_PB
        s["ok_sales"] = sales_f is not None and sales_f > B20_MIN_SALES
        s["ok_inc"]   = income_f is not None and income_f > B20_MIN_INCOME
        s["ok_idx"]   = (not B20_REQUIRE_INDEX) or (bool(index_val) and index_val not in ("-", "N/A"))
        s["ok_ipo"]   = ipo_ok

        # Vol% eliminado: ya no se descarga (ver columna VolCaida)
        s["VolPct"]   = None
        s["VolFecha"] = None

        # ── DISTANCIA al SMA20 y SMA200 (una sola descarga de yfinance) ───────
        _sma = get_sma_distances(ticker)
        s["DistSMA20"]  = _sma["dist20"]
        s["DistSMA200"] = _sma["dist200"]

    print(" " * 50, end="\r")  # limpiar línea de progreso

    # ── Excluir las que no cotizan en ningún índice (B20_INDEX_EXCLUDE) ─────
    if B20_INDEX_EXCLUDE:
        n_antes = len(stocks)
        stocks = [s for s in stocks if s.get("Index", "-") not in ("", "-", "N/A")]
        print(f"  Index (excluye sin índice): {n_antes} -> {len(stocks)}")

    # ── Filtro de TENDENCIA DIARIA (MA20/40/100/200) ─────────────────────────
    if B20_TREND_FILTER:
        total = len(stocks)
        for i, s in enumerate(stocks, 1):
            print(f"  Tendencia D {s['Ticker']} ({i}/{total})...", end="\r")
            tr = get_daily_trend(s["Ticker"])
            s["TrendOK"]  = tr["ok"]
            s["TrendTxt"] = tr["txt"]
            s["TrendNOK"] = tr.get("n_ok")
        print(" " * 50, end="\r")
        n_antes = len(stocks)
        if B20_TREND_EXCLUDE:
            stocks = [s for s in stocks if s.get("TrendOK") is True]
        print(f"  Tendencia D ({B20_TREND_WINDOW_DAYS} velas antes de '{B20_TREND_REF}', "
              f"mín {B20_TREND_MIN_PCT}%): "
              f"{n_antes} -> {len(stocks)}"
              + ("" if B20_TREND_EXCLUDE else " (solo marcado, no excluye)"))

    # ── Columna VolCaida (informativa, no filtra) ───────────────────────────
    total = len(stocks)
    for i, s in enumerate(stocks, 1):
        print(f"  VolCaida {s['Ticker']} ({i}/{total})...", end="\r")
        vc = get_vol_caida(s["Ticker"])
        s["VolCaidaOK"]  = vc["ok"]
        s["VolCaidaTxt"] = vc["txt"]
    print(" " * 50, end="\r")

    # Ordenar RESPETANDO grupos: primero las que cumplen los 5 filtros y, dentro
    # de cada grupo, la más hundida por debajo del SMA20 arriba (más negativa).
    def _sort_key(s):
        cumple_todo = (s.get("ok_pb") and s.get("ok_sales") and
                       s.get("ok_inc") and s.get("ok_idx") and s.get("ok_ipo"))
        dist = s.get("DistSMA20")
        dist = dist if dist is not None else 0.0  # sin dato -> al fondo de su grupo
        # grupo 0 = cumple todo (va antes); dentro, orden ascendente por distancia
        return (0 if cumple_todo else 1, dist)

    stocks.sort(key=_sort_key)
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
    sect_txt = ", ".join(sorted(B20_SECTORS)) if B20_SECTORS else "todos"
    print(f"\n{Fore.CYAN}Screener Finviz: {', '.join(B20_FINVIZ_FILTERS)}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}Filtros locales: Market Cap >={B20_MIN_MARKET_CAP_M}M | Sectores: {sect_txt}{Style.RESET_ALL}")
    print(f"  Consulta : {ts_str}")
    print(f"  Mercado  : {s_color}{status}{Style.RESET_ALL}")
    print(f"\n  {Fore.YELLOW}Filtros post-screener:{Style.RESET_ALL}")
    print(f"    {'P/B<' + format(B20_MAX_PB, 'g'):<10} Precio no mayor de {B20_MAX_PB:g}x el valor en libros")
    print(f"    {'Sales>' + format(B20_MIN_SALES, 'g'):<10} Ventas por encima de {B20_MIN_SALES:g}")
    print(f"    {'Income>' + format(B20_MIN_INCOME, 'g'):<10} Beneficio neto por encima de {B20_MIN_INCOME:g}")
    print(f"    {'Index':<10} " + ("Cotiza en algún índice (S&P500, RUT, NDX...)" if B20_REQUIRE_INDEX else "No exigido"))
    print(f"    {'IPO>=' + format(B20_MIN_IPO_YEARS, 'g') + 'y':<10} IPO hace mínimo {B20_MIN_IPO_YEARS:g} años")

    if not stocks:
        print(f"\n  {Fore.YELLOW}No se encontraron resultados.{Style.RESET_ALL}")
        return

    # ── NEW/OLD: comparar con ejecución anterior ──────────────────────────────
    prev_tickers  = set(load_screener_cache())
    current_tickers = set(s["Ticker"] for s in stocks)
    save_screener_cache(current_tickers)

    new_count = len(current_tickers - prev_tickers)

    # Contar cuántos pasan todos los filtros
    all_pass = [s for s in stocks if s.get("ok_pb") and s.get("ok_sales") and s.get("ok_inc") and s.get("ok_idx") and s.get("ok_ipo")]
    new_label = f"  |  {Fore.YELLOW}{new_count} nueva(s) hoy{Style.RESET_ALL}" if (prev_tickers and new_count > 0) else ""
    print(f"\n  {len(stocks)} resultado(s) del screener  |  {Fore.GREEN}{len(all_pass)} cumple(n) todos los filtros{Style.RESET_ALL}{new_label}\n")

    # Columnas: datos + 5 validaciones + Vol% + New?
    columns = [
        "#", "Ticker", "Company", "Sector", "Market Cap", "Price",
        "Dist20%", f"P/B<{B20_MAX_PB:g}", f"Sales>{B20_MIN_SALES:g}", f"Inc>{B20_MIN_INCOME:g}", "Index", "IPO", "New?", "Tend D", "VolCaida",
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

        # Dist20% — separación del precio respecto al SMA20 (negativo = por debajo)
        dist_val = s.get("DistSMA20")
        if dist_val is not None:
            dist_str = f"{dist_val:+.1f}%"
            # más hundida = más llamativo. Por debajo -> rojo; por encima -> verde
            if dist_val <= -20:
                dist_color = Fore.RED
            elif dist_val < 0:
                dist_color = Fore.YELLOW
            else:
                dist_color = Fore.GREEN
        else:
            dist_str   = "—"
            dist_color = ""

        # Tend — tendencia de fondo según SMA200 (tu caso: ALCISTA que corrige).
        #   🟢 precio POR ENCIMA del SMA200 -> venía alcista -> candidata (corrección)
        #   🔴 precio POR DEBAJO del SMA200 -> venía bajista  -> descartar
        #   ⚪ sin dato (IPO reciente, <200 días) -> no clasificable
        dist200 = s.get("DistSMA200")
        if dist200 is None:
            tend_str   = "⚪ n/d"
            tend_color = Fore.LIGHTBLACK_EX
        elif dist200 >= 0:
            tend_str   = "🟢 alza"
            tend_color = Fore.GREEN
        else:
            tend_str   = "🔴 baja"
            tend_color = Fore.RED

        # Vol% — siempre visible con fecha del día de caída
        vol_pct   = s.get("VolPct")
        vol_fecha = s.get("VolFecha")
        if vol_pct is not None:
            fecha_tag = f" ({vol_fecha})" if vol_fecha else ""
            vol_str   = f"{vol_pct:+.0f}%{fecha_tag}"
            if vol_pct >= 200:
                vol_color = Fore.GREEN
            elif vol_pct >= 50:
                vol_color = Fore.YELLOW
            else:
                vol_color = Fore.RED
        else:
            vol_str   = "—"
            vol_color = ""

        # NEW/OLD
        ticker    = s["Ticker"]
        is_new    = ticker not in prev_tickers and bool(prev_tickers)
        new_str   = "NEW" if is_new else "old"
        new_color = Fore.YELLOW if is_new else Fore.LIGHTBLACK_EX

        row = [
            "",  # placeholder para #, se rellena después de ordenar
            ticker,
            s["Company"][:30],
            s["Sector"][:15],
            s["Market Cap"],
            s["Price"],
            dist_str,
            pb_text,
            sales_text,
            inc_text,
            idx_text,
            ipo_text,
            new_str,
            s.get("TrendTxt", "-"),
            s.get("VolCaidaTxt", "-"),
        ]
        colors = {
            "chg":    chg_color,
            "dist":   dist_color,
            "tend":   tend_color,
            "pb":     Fore.GREEN if ok_pb    else Fore.RED,
            "sales":  Fore.GREEN if ok_sales else Fore.RED,
            "inc":    Fore.GREEN if ok_inc   else Fore.RED,
            "idx":    Fore.GREEN if ok_idx   else Fore.RED,
            "ipo":    Fore.GREEN if ok_ipo   else Fore.RED,
            "vol":    vol_color,
            "new":    new_color,
            "trd":    (Fore.GREEN if s.get("TrendOK") is True
                       else Fore.YELLOW if (s.get("TrendNOK") or 0) >= B20_TREND_YELLOW_MIN
                       else Fore.LIGHTBLACK_EX if s.get("TrendNOK") is not None else ""),
            "volc":   (Fore.GREEN if s.get("VolCaidaOK") is True
                       else Fore.RED if s.get("VolCaidaOK") is False else ""),
            "all_ok": all_ok,
        }
        # valor numérico de distancia para ordenar (sin dato -> al fondo del grupo)
        dist_sort = dist_val if dist_val is not None else 0.0
        entries.append((all_ok, row, colors, dist_sort))

    # Ordenar: las que pasan todos los filtros primero y, dentro de cada grupo,
    # la MÁS separada por debajo del SMA20 arriba (distancia más negativa).
    def sort_key(entry):
        all_ok    = entry[0]
        dist_sort = entry[3]  # % de distancia al SMA20 (negativo = por debajo)
        return (not all_ok, dist_sort)

    entries.sort(key=sort_key)

    # Renumerar y asignar colores finales
    rows       = []
    row_colors = []
    for i, (all_ok, row, colors, _dist) in enumerate(entries, 1):
        row[0] = str(i)
        rows.append(row)
        row_colors.append(colors)

    # Calcular anchos de columna
    widths = [len(col) for col in columns]
    for row in rows:
        for j, cell in enumerate(row):
            widths[j] = max(widths[j], len(str(cell)))

    # Índices de las columnas de validación
    # (tras insertar "Dist20%" en 7 y "Tend" en 8, todo lo de después va +1)
    IDX_CHG   = -1   # columna Change% eliminada
    IDX_DIST  = 6
    IDX_TEND  = -1   # columna Tend eliminada
    IDX_PB    = 7
    IDX_SALES = 8
    IDX_INC   = 9
    IDX_IDX   = 10
    IDX_IPO   = 11
    IDX_VOL   = -1   # columna Vol% eliminada
    IDX_NEW   = 12
    IDX_TRD   = 13
    IDX_VOLC  = 14

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
                if   j == IDX_CHG and colors["chg"]:   text = f"{colors['chg']}{text}{Style.RESET_ALL}"
                elif j == IDX_DIST and colors["dist"]: text = f"{colors['dist']}{text}{Style.RESET_ALL}"
                elif j == IDX_TEND and colors["tend"]: text = f"{colors['tend']}{text}{Style.RESET_ALL}"
                elif j == IDX_VOL and colors["vol"]:   text = f"{colors['vol']}{text}{Style.RESET_ALL}"
                elif j == IDX_NEW:                     text = f"{colors['new']}{text}{Style.RESET_ALL}"
                elif j == IDX_TRD and colors["trd"]:   text = f"{colors['trd']}{text}{Style.RESET_ALL}"
                elif j == IDX_VOLC and colors["volc"]: text = f"{colors['volc']}{text}{Style.RESET_ALL}"
                elif j in VAL_COLS:                    text = f"{Fore.GREEN}{text}{Style.RESET_ALL}"
                elif j == 1:                           text = f"{Fore.GREEN}{text}{Style.RESET_ALL}"
            else:
                if j in VAL_COLS:
                    color_key = {IDX_PB: "pb", IDX_SALES: "sales", IDX_INC: "inc", IDX_IDX: "idx", IDX_IPO: "ipo"}[j]
                    text = f"{colors[color_key]}{text}{Style.RESET_ALL}"
                elif j == IDX_DIST and colors["dist"]: text = f"{colors['dist']}{text}{Style.RESET_ALL}"
                elif j == IDX_TEND and colors["tend"]: text = f"{colors['tend']}{text}{Style.RESET_ALL}"
                elif j == IDX_VOL and colors["vol"]:   text = f"{colors['vol']}{text}{Style.RESET_ALL}"
                elif j == IDX_NEW:                     text = f"{colors['new']}{text}{Style.RESET_ALL}"
                elif j == IDX_TRD and colors["trd"]:   text = f"{colors['trd']}{text}{Style.RESET_ALL}"
                elif j == IDX_VOLC and colors["volc"]: text = f"{colors['volc']}{text}{Style.RESET_ALL}"
                elif j == IDX_CHG and colors["chg"]:   text = f"{GREY}{text}{Style.RESET_ALL}"
                else:                                  text = f"{GREY}{text}{Style.RESET_ALL}"
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

    # Devolver los tickers en el MISMO orden que se muestran (col 1 = Ticker)
    return [row[1] for row in rows]


def tickers_below20_ordered(stocks):
    """Devuelve los tickers en el mismo orden que la tabla --below20, SIN imprimir.
    Reutiliza la lógica de orden de print_below20_table silenciando la salida."""
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        tickers = print_below20_table(stocks)
    return tickers


def export_below20_txt(stocks, path=None):
    """Exporta a un .txt los tickers en el mismo orden que la tabla --below20,
    uno por línea (formato compatible con TC2000)."""
    import datetime
    tickers = tickers_below20_ordered(stocks)
    if path is None:
        fecha = datetime.datetime.now().strftime("%Y%m%d")
        path = f"mm20_{fecha}.txt"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for t in tickers:
            f.write(f"{t}\n")
    return path, tickers



# ─── SCREENER STRONG BUY TECHNOLOGY ────────────────────────────────────────────
def fetch_stbuy_screener():
    """Empresas Technology de USA, Market Cap Small+, precio superior a $40, Target Price 50% Above Price y Analyst Recom Strong Buy."""
    from lxml import html as lxml_html
    import re
    base = ("https://finviz.com/screener.ashx?v=111"
            "&f=cap_smallover,geo_usa,sec_technology,an_recom_strongbuy,sh_price_o40,targetprice_a50&o=-marketcap&ft=4")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
               "Referer": "https://finviz.com/"}
    stocks, seen, page = [], set(), 1
    while True:
        try:
            resp = requests.get(base + f"&r={1 + (page-1)*20}", headers=headers, timeout=20)
            resp.raise_for_status()
        except Exception as e:
            print(f"  {Fore.RED}Error al conectar con Finviz: {e}{Style.RESET_ALL}")
            break
        tree = lxml_html.fromstring(resp.content)
        tables = tree.xpath('//table[contains(@class,"screener_table")]')
        rows = tables[0].xpath('.//tr') if tables else []
        added = 0
        for tr in rows:
            ticker = None
            for a in tr.xpath('.//a[@href]'):
                m = re.search(r'[?&]t=([A-Za-z0-9.\-]+)', a.get('href',''))
                if m: ticker = m.group(1).upper(); break
            cols = [td.text_content().strip() for td in tr.xpath('.//td')]
            if not ticker or ticker in seen or len(cols) < 11: continue
            seen.add(ticker); added += 1
            stocks.append({"Ticker":ticker, "Company":cols[2], "Industry":cols[4],
                           "Country":cols[5], "Market Cap":cols[6], "P/E":cols[7],
                           "Price":cols[8], "Change%":cols[9], "Volume":cols[10]})
        nxt = tree.xpath('//a[@id="screener-next"] | //a[contains(@class,"screener-pages") and contains(translate(text(),"NEXT","next"),"next")]')
        if not nxt or not added: break
        page += 1
    return stocks

def get_analyst_target_range(ticker):
    """Devuelve Low Target, Avg Target y High Target usando yfinance."""
    try:
        import yfinance as yf
        targets = yf.Ticker(ticker).analyst_price_targets
        if targets is None:
            return "-", "-", "-"
        # yfinance puede devolver dict o Series segun la version.
        if hasattr(targets, "to_dict"):
            targets = targets.to_dict()
        if not isinstance(targets, dict):
            return "-", "-", "-"
        low = targets.get("low") or targets.get("Low")
        avg = (targets.get("mean") or targets.get("Mean") or
               targets.get("average") or targets.get("Average"))
        high = targets.get("high") or targets.get("High")
        def fmt(value):
            try:
                return f"${float(value):.2f}"
            except (TypeError, ValueError):
                return "-"
        return fmt(low), fmt(avg), fmt(high)
    except Exception:
        return "-", "-", "-"

def print_stbuy_table(stocks):
    print(f"\n{Fore.CYAN}Screener: Technology | USA | Market Cap Small+ | Price > $40 | Target +50% | Strong Buy{Style.RESET_ALL}")
    if not stocks:
        print(f"  {Fore.YELLOW}No se encontraron resultados.{Style.RESET_ALL}"); return
    print(f"  Obteniendo targets de analistas para {len(stocks)} empresa(s)...")
    filtered = []
    for stock in stocks:
        low, avg, high = get_analyst_target_range(stock["Ticker"])
        stock["Low Target"] = low
        stock["Avg Target"] = avg
        stock["High Target"] = high
        price_value = to_float(stock.get("Price"))
        low_value = to_float(low)
        # Excluir si falta un Low Target válido o si Price >= Low Target.
        if price_value is None or low_value is None or price_value >= low_value:
            continue
        stock["Low Upside %"] = ((low_value - price_value) / price_value) * 100
        filtered.append(stock)

    # Mayor distancia porcentual al Low Target primero.
    filtered.sort(key=lambda stock: stock["Low Upside %"], reverse=True)

    columns=["#","Ticker","Company","Industry","Country","Market Cap","P/E","Price","Low Target","Low Upside %","Avg Target","High Target"]
    rows=[[i,st["Ticker"],st["Company"],st["Industry"],st["Country"],st["Market Cap"],st["P/E"],st["Price"],st["Low Target"],f"{st['Low Upside %']:+.1f}%",st["Avg Target"],st["High Target"]] for i,st in enumerate(filtered,1)]
    widths=[len(c) for c in columns]
    for row in rows:
        for i,c in enumerate(row): widths[i]=max(widths[i],len(str(c)))
    fmt=lambda row: " | ".join(str(c).ljust(widths[i]) for i,c in enumerate(row))
    print(f"  {len(filtered)} resultado(s) tras exigir Price < Low Target\n"); print(fmt(columns)); print("-+-".join("-"*w for w in widths))
    for row in rows: print(fmt(row))

# ─── PREMARKET RESUMIDO (--pre) ───────────────────────────────────────────────
def _pre_headers(referer="https://finance.yahoo.com/"):
    return {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Accept-Language":"es-ES,es;q=0.9,en;q=0.7", "Referer":referer}

def _pre_signed(v):
    try: v=float(v)
    except (TypeError,ValueError): return f"{Fore.LIGHTBLACK_EX}n/d{Style.RESET_ALL}"
    c=Fore.GREEN if v>0 else Fore.RED if v<0 else Fore.WHITE
    return f"{c}{v:+.2f}%".replace(".",",")+Style.RESET_ALL

def _trim(v,n):
    v=" ".join(str(v or "").split()); return v if len(v)<=n else v[:n-1]+"…"

# ─── PREMARKET v3: hora ET, frescura de datos, noticias filtradas y reacción a earnings ──────────
def _et_offset_hours(d):
    """UTC -> ET: EDT (-4) del 2º domingo de marzo al 1º de noviembre; EST (-5) el resto."""
    mar1=datetime.date(d.year,3,1); dst_start=mar1+datetime.timedelta(days=(6-mar1.weekday())%7+7)
    nov1=datetime.date(d.year,11,1); dst_end=nov1+datetime.timedelta(days=(6-nov1.weekday())%7)
    return -4 if dst_start<=d<dst_end else -5

def _utc_now(): return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
def _from_epoch(ts): return datetime.datetime.fromtimestamp(ts,datetime.timezone.utc).replace(tzinfo=None)
def _utc_to_et(dt_utc): return dt_utc+datetime.timedelta(hours=_et_offset_hours(dt_utc.date()))
def _et_now(): return _utc_to_et(_utc_now())

def _since_last_close_utc():
    """Instante (UTC) del último cierre regular: 16:00 ET del día hábil anterior a hoy."""
    d=_et_now().date()-datetime.timedelta(days=1)
    while d.weekday()>=5: d-=datetime.timedelta(days=1)
    return datetime.datetime.combine(d,datetime.time(16,0))-datetime.timedelta(hours=_et_offset_hours(d))

def _yahoo_chart(symbol,rng="1d",interval="5m"):
    from urllib.parse import quote
    u=("https://query1.finance.yahoo.com/v8/finance/chart/"+quote(symbol)+
       f"?interval={interval}&range={rng}&includePrePost=true")
    return requests.get(u,headers=_pre_headers(),timeout=12).json()["chart"]["result"][0]

def fetch_pre_futures():
    """Devuelve (lista [(etiqueta,pct)], en_vivo, info). 'en_vivo' se decide por la EDAD del último dato
    (no por el marketState de Yahoo, que a veces marca CLOSED con los futuros operando).
    Russell 2000 se mide con el futuro RTY=F: es 24h y no depende del estado del ETF IWM."""
    out=[]; info=[]
    for label,symbol in [("DOW","YM=F"),("S&P 500","ES=F"),("NASDAQ 100","NQ=F"),("RUSSELL 2000","RTY=F")]:
        pct=None; d=dict(symbol=symbol,state="?",last=None,prev=None,age=None,hora="")
        try:
            res=_yahoo_chart(symbol); m=res["meta"]
            last=m.get("regularMarketPrice"); prev=m.get("chartPreviousClose") or m.get("previousClose")
            d.update(state=str(m.get("marketState") or "?").upper(),last=last,prev=prev)
            ts=m.get("regularMarketTime")
            if not ts:
                tss=res.get("timestamp") or []; ts=tss[-1] if tss else None
            if ts:
                d["age"]=(_utc_now()-_from_epoch(ts)).total_seconds()/60
                d["hora"]=_utc_to_et(_from_epoch(ts)).strftime("%H:%M")
            if last is not None and prev: pct=(float(last)/float(prev)-1)*100
        except Exception: pass
        out.append((label,pct)); info.append(d)
    live=any(i["age"] is not None and i["age"]<=30 for i in info)
    return out,live,info

def _stock_snapshot(symbol):
    """Movimiento ACTUAL (premarket o regular) y after-hours de ayer, frente al último cierre regular.
    Usa velas de 5 min (5 días) con horario extendido; el 'cierre' es la última vela regular (~16:00)."""
    try: res=_yahoo_chart(symbol,"5d","5m")
    except Exception: return None
    try:
        ts=res.get("timestamp") or []
        closes=(((res.get("indicators") or {}).get("quote") or [{}])[0].get("close")) or []
        bars=[(_utc_to_et(_from_epoch(t)),float(c)) for t,c in zip(ts,closes) if c is not None]
        if not bars: return None
        today=_et_now().date()
        reg=[(e,c) for e,c in bars if datetime.time(9,30)<=e.time()<datetime.time(16,0) and e.date()<today]
        if not reg: return None
        prev_close=reg[-1][1]; prev_date=reg[-1][0].date()
        post=[c for e,c in bars if e.date()==prev_date and e.time()>=datetime.time(16,0)]
        now_bars=[(e,c) for e,c in bars if e.date()>=today]
        ah=((post[-1]/prev_close-1)*100 if post else None)
        if now_bars:
            e_last,c_last=now_bars[-1]; fuente="premarket" if e_last.time()<datetime.time(9,30) else "regular"
        else:
            # Antes de las 4:00 ET Yahoo aún no tiene velas de hoy: lo último que hay es el after-hours de ayer.
            post_bars=[(e,c) for e,c in bars if e.date()==prev_date and e.time()>=datetime.time(16,0)]
            if not post_bars: return None
            e_last,c_last=post_bars[-1]; fuente="after-hours"
        return dict(prev_close=prev_close,last=c_last,pct=(c_last/prev_close-1)*100,ah=ah,
                    hora=e_last.strftime("%a %H:%M") if fuente=="after-hours" else e_last.strftime("%H:%M"),fuente=fuente)
    except Exception: return None

def fetch_pre_movers(tickers):
    """{ticker: snapshot} en paralelo; los que fallan simplemente no aparecen."""
    from concurrent.futures import ThreadPoolExecutor
    snaps={}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for t,sn in zip(tickers,ex.map(_stock_snapshot,tickers)):
            if sn: snaps[t]=sn
    return snaps

# ── Noticias: Yahoo + Google News, filtradas por relevancia para la apertura ─────────────────────
_NEWS_MACRO=[(r"\b(fed|fomc|powell|rate (cut|hike)s?|interest rates?)\b","FED",5),
             (r"\b(cpi|ppi|inflation|pce|jobs report|payrolls?|jobless|unemployment|gdp|retail sales|ism|pmi)\b","MACRO",3),
             (r"\b(treasur(y|ies)|bond yields?|yields?)\b","MACRO",3),
             (r"\b(tariffs?|trade war|sanctions?|export controls?|shutdown|debt ceiling)\b","POLÍTICA",3),
             (r"\b(oil|crude|opec)\b","ENERGÍA",2),
             (r"\b(war|ceasefire|missile|iran|israel|ukraine|taiwan)\b","GEOPOL.",2),
             (r"\b(ipo)\b","IPO",2)]
_NEWS_EARN=r"\b(earnings|results|beats?|misses?|guidance|outlook|raises?|cuts?|revenue|profit warning|eps)\b"
_NEWS_MKT=r"\b(futures|pre-?market|wall street|stock market today|markets? wrap|stocks? (rise|fall|slip|surge|drop|waver|slide))\b"
_NEWS_MOVE=r"\b(surge[sd]?|plunge[sd]?|soar(s|ed)?|tumbl(e|es|ed)|jump(s|ed)?|sink(s)?|slump(s|ed)?|rall(y|ies|ied)|crash(es|ed)?|spike[sd]?)\b"
_NEWS_JUNK=r"(should you buy|best stocks?|top stocks?|stocks? to buy|here'?s why|is it too late|reasons? to|motley fool|zacks|insider monkey|24/7 wall st|forever stock|millionaire|retire|dividend (stock|king)|\b\d+ (stocks?|reasons?|things?)\b|(stock|share|price) (forecast|prediction)s?|price targets? (for|to) 20\d\d|(?:20(?:2[7-9]|[3-9]\d)|beyond)\b.*(forecast|prediction)|bargain or|peak\?)"
_NEWS_OFFTOPIC=r"\b(uk|u\.k\.|europe(an)?|india|nifty|sensex|japan|nikkei|hang seng|australia|canada|brazil)\b"
_NEWS_LOW_SOURCES=("litefinance","tikr","investorplace","stocktwits","benzinga pro","seeking alpha","simply wall st","coincentral","msn")
_NEWS_TOP_SOURCES=("federal reserve","sec.gov","white house","treasury","reuters","bloomberg","cnbc","wsj","wall street journal","marketwatch","barron","associated press","financial times","yahoo finance","investopedia")
_NEWS_CORE=("nvidia","nvda","tesla","tsla","apple","aapl","amd","advanced micro","meta","alphabet","google","amazon","amzn","microsoft","msft")
_NEWS_AMBIGUOUS_TICKERS={"low","now","dash","coin","hood","ba","c","v","ma","li","ci"}
_GOOGLE_QUERIES=["stock futures premarket","Wall Street stocks Fed Treasury yields","premarket movers earnings","stock market today Nasdaq S&P 500","earnings after the bell results stock moves","earnings before the bell results stock"]

def _parse_rss_items(content):
    """RSS -> [dict(headline, dt UTC naive, source, link)]. Valido para Google News y Yahoo."""
    import xml.etree.ElementTree as ET, email.utils
    items=[]
    for it in ET.fromstring(content).findall(".//item"):
        title=(it.findtext("title") or "").strip(); dt=None
        pub=it.findtext("pubDate")
        if pub:
            try:
                dt=email.utils.parsedate_to_datetime(pub)
                dt=dt.astimezone(datetime.timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
            except Exception: dt=None
        se=it.find("source"); src=(se.text or "").strip() if se is not None else ""
        if src and title.endswith(" - "+src): title=title[:-len(src)-3].strip()
        if title: items.append(dict(headline=title,dt=dt,source=src,link=(it.findtext("link") or "").strip()))
    return items

def fetch_pre_news_pool(extra_names=(),extra_tickers=()):
    """Reúne candidatos de Google News (RSS), Yahoo (RSS + yfinance) y, si no hay nada, Finviz.
    Devuelve (items, conteo_por_origen)."""
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import quote_plus
    def rss(url,origin):
        try:
            r=requests.get(url,headers=_pre_headers(),timeout=12); r.raise_for_status()
            its=_parse_rss_items(r.content)
            for x in its: x["origin"]=origin; x["is_pr"]=x.get("source","") in PR_SOURCES
            return its
        except Exception: return []
    def gurl(q): return "https://news.google.com/rss/search?q="+quote_plus(q+" when:1d")+"&hl=en-US&gl=US&ceid=US:en"
    jobs=[(gurl(q),"Google") for q in _GOOGLE_QUERIES]+[(gurl(n+" stock"),"Google") for n in extra_names]
    jobs.append(("https://finance.yahoo.com/news/rssindex","Yahoo"))
    pool=[]
    with ThreadPoolExecutor(max_workers=8) as ex:
        for its in ex.map(lambda j: rss(*j),jobs): pool+=its
    for t in ["SPY","QQQ"]+list(extra_tickers):                                      # yfinance (Yahoo)
        try: pool+=fetch_news_yahoo(t,10)
        except Exception: pass
    conteo={}
    for x in pool: conteo[x.get("origin","?")]=conteo.get(x.get("origin","?"),0)+1
    if not pool:                                    # último recurso
        for h,o in fetch_pre_headlines(6): pool.append(dict(headline=h,dt=None,source=o,origin=o,is_pr=False))
    return pool,conteo

def _news_regexes():
    import re
    names={n.lower() for n in _EARNINGS.values() if len(n)>=4}
    tick={t.lower() for t in _EARNINGS if len(t)>=3 and t.lower() not in _NEWS_AMBIGUOUS_TICKERS}
    watch=re.compile(r"\b("+"|".join(re.escape(x) for x in sorted(names|tick,key=len,reverse=True))+r")\b",re.I)
    core=re.compile(r"\b("+"|".join(re.escape(x) for x in _NEWS_CORE)+r")\b",re.I)
    return watch,core

def _news_score(item,watch_re,core_re):
    import re
    hl=(item.get("headline") or "").lower(); sc=0; tags=[]; macro=0
    for pat,tag,wt in _NEWS_MACRO:
        if re.search(pat,hl): macro+=wt; tags.append(tag)
    sc+=min(macro,6)
    earn=bool(re.search(_NEWS_EARN,hl)); mkt=bool(re.search(_NEWS_MKT,hl)); move=bool(re.search(_NEWS_MOVE,hl))
    w=bool(watch_re.search(hl)); c=bool(core_re.search(hl))
    if earn and (w or c): sc+=3; tags.insert(0,"EARNINGS")
    elif earn: sc+=1
    if w: sc+=2
    if c: sc+=3
    if mkt: sc+=2; tags.append("MERCADO")
    if move: sc+=1
    if re.search(_NEWS_JUNK,hl): sc-=6
    if re.search(_NEWS_OFFTOPIC,hl) and not (w or c): sc-=3
    if item.get("is_pr"): sc-=3
    src=(item.get("source") or "").lower()
    if any(x in src for x in _NEWS_TOP_SOURCES): sc+=1
    if any(x in src for x in _NEWS_LOW_SOURCES): sc-=4
    dt=item.get("dt")
    if dt and (_utc_now()-dt).total_seconds()<6*3600: sc+=1
    if dt and (_utc_now()-dt).total_seconds()<2*3600: sc+=1
    macro_tags=[t for t in tags if t not in ("EARNINGS","MERCADO")]
    if "EARNINGS" in tags: tag="EARNINGS"
    elif macro_tags: tag=macro_tags[0]
    elif w or c: tag="EMPRESA"
    elif "MERCADO" in tags: tag="MERCADO"
    else: tag=""
    return sc,tag

def pick_pre_news(items,limit=5,min_score=4):
    """Solo lo publicado desde el cierre de ayer, con puntuación mínima, sin duplicados y máximo 'limit'."""
    import re
    watch_re,core_re=_news_regexes(); since=_since_last_close_utc(); scored=[]
    for it in items:
        dt=it.get("dt")
        if not dt or dt<since: continue
        sc,tag=_news_score(it,watch_re,core_re)
        if sc>=min_score: scored.append(dict(it,score=sc,tag=tag))
    scored.sort(key=lambda x:(x["score"],x["dt"]),reverse=True)
    kept=[]; toks_kept=[]; por_tema={}
    for it in scored:
        hl=it["headline"].lower()
        toks={w for w in re.findall(r"[a-z0-9]+",hl) if len(w)>3}
        if any(len(toks&k)/max(1,len(toks|k))>=0.4 for k in toks_kept): continue    # misma noticia, otro medio
        ent=core_re.search(hl) or watch_re.search(hl)
        clave=ent.group(1).lower() if ent else (it["tag"] or "otra")
        if por_tema.get(clave,0)>=2: continue                                         # no 5 titulares de lo mismo
        por_tema[clave]=por_tema.get(clave,0)+1
        kept.append(it); toks_kept.append(toks)
        if len(kept)>=limit: break
    return kept



def _earn_cache_path():
    import os
    try: return os.path.join(os.path.dirname(os.path.abspath(__file__)),"earnings_cache.json")
    except NameError: return "earnings_cache.json"

def _load_earn_cache():
    """Memoria local {ticker: {fecha, hora}} con la última fecha de earnings que vio OptionSlam. Si no existe, {}."""
    import json
    try:
        with open(_earn_cache_path(),encoding="utf-8") as f: d=json.load(f)
        return d if isinstance(d,dict) else {}
    except Exception: return {}

def _save_earn_cache(nuevo):
    import json
    try:
        d=_load_earn_cache(); d.update(nuevo)
        with open(_earn_cache_path(),"w",encoding="utf-8") as f: json.dump(d,f,ensure_ascii=False,indent=0)
    except Exception: pass                          # sin permisos de escritura: se sigue sin memoria

def fetch_pre_headlines(limit=2):
    import xml.etree.ElementTree as ET
    found=[]
    try:
        r=requests.get("https://finance.yahoo.com/news/rssindex",headers=_pre_headers(),timeout=12); r.raise_for_status()
        for n in ET.fromstring(r.content).findall(".//item"):
            t=(n.findtext("title") or "").strip()
            if t and t not in [x[0] for x in found]: found.append((t,"Yahoo"))
            if len(found)>=limit: break
    except Exception: pass
    if len(found)<limit:
        try:
            from bs4 import BeautifulSoup
            r=requests.get("https://finviz.com/news.ashx",headers=_pre_headers("https://finviz.com/"),timeout=12); r.raise_for_status()
            for a in BeautifulSoup(r.text,"html.parser").select("a.nn-tab-link, a.news-link-left"):
                t=a.get_text(" ",strip=True)
                if len(t)>=25 and t not in [x[0] for x in found]: found.append((t,"Finviz"))
                if len(found)>=limit: break
        except Exception: pass
    return found[:limit]

def fetch_pre_agenda(days=7, limit=40):
    """Investing.com ★★★ USA; fallback Yahoo con eventos macro prioritarios."""
    from bs4 import BeautifulSoup
    import re
    today=datetime.date.today(); last=today+datetime.timedelta(days=days-1)
    session=requests.Session(); page="https://es.investing.com/economic-calendar"
    endpoint=page+"/Service/getCalendarFilteredData"
    headers=_pre_headers(page)
    headers.update({"X-Requested-With":"XMLHttpRequest","Origin":"https://es.investing.com"})
    payload=[("dateFrom",today.isoformat()),("dateTo",last.isoformat()),("timeZone","8"),
             ("timeFilter","timeRemain"),("currentTab","custom"),("limit_from","0"),
             ("country[]","5"),("importance[]","3")]
    primary_error=None
    try:
        session.get(page,headers=_pre_headers(),timeout=15)
        r=session.post(endpoint,headers=headers,data=payload,timeout=20); r.raise_for_status()
        try: body=r.json().get("data","")
        except Exception: body=r.text
        soup=BeautifulSoup(body,"html.parser")
        html_rows=soup.select("tr.js-event-item, tr[event_attr_id], tr[data-event-datetime]")
        if not html_rows:
            primary_error="Investing.com respondió, pero cambió o bloqueó el formato"
        rows=[]
        for tr in html_rows:
            cur=tr.select_one("td.flagCur,.flagCur")
            if cur and "USD" not in cur.get_text(" ",strip=True).upper(): continue
            imp=tr.select_one("td.sentiment,.sentiment"); stars=0
            if imp:
                stars=len(imp.select("i.grayFullBullishIcon,i.fullBullishIcon,i[class*='FullBullish']"))
                hint=" ".join((imp.get("title",""),imp.get("aria-label",""),str(imp)))
                if re.search(r"alta|high|bull3|sentiment-3",hint,re.I): stars=max(stars,3)
            if stars<3: continue
            raw=tr.get("data-event-datetime",""); dt=None
            for fmt in ("%Y/%m/%d %H:%M:%S","%Y-%m-%d %H:%M:%S"):
                try: dt=datetime.datetime.strptime(raw,fmt); break
                except ValueError: pass
            if not dt or not(today<=dt.date()<=last): continue
            n=tr.select_one("td.event a,td.event,.event"); event=n.get_text(" ",strip=True) if n else ""
            def cell(sel):
                x=tr.select_one(sel); return x.get_text(" ",strip=True) if x else ""
            if event: rows.append((dt.date(),dt.strftime("%I:%M %p").lstrip("0"),event,"",cell("td.act"),cell("td.fore"),cell("td.prev")))
        if rows: return sorted(rows,key=lambda x:(x[0],x[1],x[2]))[:limit],None,"Investing.com"
        if primary_error is None: primary_error="Investing.com respondió sin eventos ★★★ USA reconocibles"
    except Exception as e: primary_error=f"Investing.com no accesible ({type(e).__name__})"

    # Fallback transparente: Yahoo, solo USA y eventos macro considerados prioritarios.
    important=re.compile(r"JOLTS|Nonfarm|Non-Farm|Unemployment Rate|Jobless Claims|Initial Claims|CPI|Consumer Price|PPI|Producer Price|Core PCE|GDP|Gross Domestic|Retail Sales|Services PMI|Manufacturing PMI|ISM|Interest Rate|FOMC|Powell|Fed Minutes",re.I)
    fallback=[]
    for offset in range(days):
        day=today+datetime.timedelta(days=offset)
        try:
            r=requests.get(f"https://finance.yahoo.com/calendar/economic?day={day.isoformat()}",headers=_pre_headers(),timeout=15); r.raise_for_status()
            soup=BeautifulSoup(r.text,"html.parser")
            for tr in soup.select("table tbody tr"):
                cols=[" ".join(x.get_text(" ",strip=True).split()) for x in tr.select("td")]
                ci=next((i for i,x in enumerate(cols) if x.upper() in {"US","USA","UNITED STATES"}),None)
                if ci is None or not cols or not important.search(cols[0]): continue
                def val(i): return cols[i] if i<len(cols) else ""
                fallback.append((day,val(ci+1),cols[0].rstrip("* "),val(ci+2),val(ci+3),val(ci+4),val(ci+5)))
        except Exception: pass
    if fallback: return sorted(fallback,key=lambda x:(x[0],x[1],x[2]))[:limit],primary_error,"Yahoo fallback"
    return [],primary_error,None

_EARNINGS={
"SPCX":"SPACEX","NVDA":"NVIDIA","AVGO":"Broadcom","AMD":"AMD","MU":"Micron","INTC":"Intel","TXN":"Texas Instruments",
"ADI":"Analog Devices","QCOM":"Qualcomm","MRVL":"Marvell","LRCX":"Lam Research","AMAT":"Applied Materials","KLAC":"KLA",
"MSFT":"Microsoft","AAPL":"Apple","GOOGL":"Alphabet","META":"Meta","AMZN":"Amazon","TSLA":"Tesla","ORCL":"Oracle",
"PLTR":"Palantir","NFLX":"Netflix","CSCO":"Cisco","MCD":"McDonald's","HD":"Home Depot","WMT":"Walmart","COST":"Costco",
"JPM":"JPMorgan","BAC":"Bank of America","V":"Visa","MA":"Mastercard","AXP":"American Express","GS":"Goldman Sachs",
"LLY":"Eli Lilly","JNJ":"Johnson & Johnson","UNH":"UnitedHealth","GE":"GE Aerospace","CAT":"Caterpillar","XOM":"Exxon Mobil","CVX":"Chevron",
# ── ampliación ──
"IBM":"IBM","NOW":"ServiceNow","DASH":"DoorDash","LYFT":"Lyft","UBER":"Uber","LOW":"Lowe's",
"C":"Citigroup","PYPL":"PayPal","BABA":"Alibaba","LI":"Li Auto","NIO":"NIO","XPEV":"XPeng",
"COIN":"Coinbase","HOOD":"Robinhood","CVS":"CVS Health","MRNA":"Moderna","PFE":"Pfizer",
"BA":"Boeing","CCL":"Carnival","RCL":"Royal Caribbean","AAL":"American Airlines","DAL":"Delta Air Lines"}

# ETFs / índices que sigues: NO publican earnings, así que no se consultan (evita ~11 peticiones inútiles).
_EARNINGS_ETF={
"SOXL":"Direxion Semiconductor Bull 3x","DIA":"SPDR Dow Jones","QQQ":"Invesco QQQ","SPY":"SPDR S&P 500",
"SPX":"S&P 500 (índice)","IWM":"iShares Russell 2000","TNA":"Direxion Small Cap Bull 3x",
"GLD":"SPDR Gold","SLV":"iShares Silver","USO":"US Oil Fund","URA":"Global X Uranium"}

# Mismo emisor, misma fecha de resultados: se consulta una sola vez y se muestra "GOOGL/GOOG".
_EARNINGS_ALIAS={"GOOG":"GOOGL"}

def _optionslam(ticker):
    import re, time
    from bs4 import BeautifulSoup
    r=None
    for intento in range(2):                       # un reintento ante fallo puntual / límite de peticiones
        try:
            r=requests.get(f"https://www.optionslam.com/earnings/stocks/{ticker}",headers=_pre_headers("https://www.optionslam.com/"),timeout=15)
            r.raise_for_status(); break
        except Exception:
            r=None
            if intento==0: time.sleep(0.8)
    if r is None: return None
    try:
        text=" ".join(BeautifulSoup(r.text,"html.parser").get_text(" ",strip=True).split())
        # La hora (BO/AC) es opcional: algunos ADR (BABA, NIO, LI...) pueden no traerla.
        m=re.search(r"Next Earnings Date\s*:?\s*([A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+\d{4})(?:\s+(BO|AC)\b)?",text,re.I)
        if not m: return None
        d=None
        for fmt in ("%b %d, %Y","%B %d, %Y"):
            try: d=datetime.datetime.strptime(m.group(1).replace(".",""),fmt).date(); break
            except ValueError: pass
        if not d: return None
        est=bool(re.search(r"\bEstimated\b",text[max(0,m.start()-120):m.end()+120],re.I))
        return d,(m.group(2) or "—").upper(),est
    except Exception: return None

def fetch_pre_earnings(limit=120):
    from concurrent.futures import ThreadPoolExecutor,as_completed
    today=datetime.date.today(); last=today+datetime.timedelta(days=6); out=[]
    alias_de={}                                    # {"GOOGL":["GOOG"]}
    for a,t in _EARNINGS_ALIAS.items(): alias_de.setdefault(t,[]).append(a)
    consulta=[t for t in _EARNINGS if t not in _EARNINGS_ALIAS and t not in _EARNINGS_ETF]
    memoria={}
    with ThreadPoolExecutor(max_workers=8) as ex:
        jobs={ex.submit(_optionslam,t):t for t in consulta}
        for job in as_completed(jobs):
            t=jobs[job]; v=job.result()
            if v: memoria[t]={"fecha":v[0].isoformat(),"hora":v[1]}
            if v and today<=v[0]<=last:
                etiqueta="/".join([t]+alias_de.get(t,[]))
                out.append((v[0],v[1],v[2],etiqueta,_EARNINGS.get(t,t)))
    _save_earn_cache(memoria)                       # para saber mañana quién reportó hoy
    return sorted(out,key=lambda x:(x[0],x[1],x[3]))[:limit]

def _agenda_time_value(day, time_text):
    """Devuelve datetime para ordenar y clasificar una hora ET."""
    text = " ".join(str(time_text or "").strip().upper().split())
    for fmt in ("%I:%M %p", "%H:%M"):
        try:
            return datetime.datetime.combine(day, datetime.datetime.strptime(text, fmt).time())
        except ValueError:
            pass
    return datetime.datetime.combine(day, datetime.time.max)


def _agenda_operational_alert(day, time_text):
    """Aviso compacto respecto a la apertura regular de las 9:30 ET."""
    today = datetime.datetime.now().date()
    if day != today:
        return "", ""
    event_dt = _agenda_time_value(day, time_text)
    market_open = datetime.datetime.combine(day, datetime.time(9, 30))
    delta_min = int((event_dt - market_open).total_seconds() // 60)

    # Ventana que puede alterar la preparación/apertura normal: 8:00–10:30 ET.
    if datetime.time(9, 30) <= event_dt.time() <= datetime.time(10, 30):
        return f"NO NORMAL +{delta_min}m", Fore.RED
    if datetime.time(8, 0) <= event_dt.time() < datetime.time(9, 30):
        return f"PRE-OPEN {abs(delta_min)}m", Fore.YELLOW
    return "", ""


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# PREMARKET v4: futuros con el retraso declarado, earnings de las GRANDES (resultado + reacción),
# agenda con avisos de festivos, próximos earnings y próximos splits. Sin lecturas de gap.
# ═══════════════════════════════════════════════════════════════════════════════════════════════
_MESES_ES=["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"]
_DIAS_ES=["lun","mar","mié","jue","vie","sáb","dom"]
_DIAS_LARGO_ES=["lunes","martes","miércoles","jueves","viernes","sábado","domingo"]

def _dia_es(d): return f"{_DIAS_ES[d.weekday()]} {d.day} {_MESES_ES[d.month-1][:3]}"

def _ancho():
    import shutil
    return max(100,shutil.get_terminal_size((130,20)).columns)

def _n(x,dec=2):
    """Número con coma decimal (formato español)."""
    return f"{x:.{dec}f}".replace(".",",")

def _pct_es(v,dec=1):
    try: v=float(v)
    except (TypeError,ValueError): return "n/d"
    return f"{v:+.{dec}f}".replace(".",",")+" %"

def _cuadro(v,tol=0.005):
    """Cuadrito de color: verde si v>0, rojo si v<0, amarillo si empate, gris si no hay dato."""
    if v is None: return f"{Fore.LIGHTBLACK_EX}■{Style.RESET_ALL}"
    c=Fore.GREEN if v>tol else Fore.RED if v<-tol else Fore.YELLOW
    return f"{c}■{Style.RESET_ALL}"

def _prev_bday(d):
    d=d-datetime.timedelta(days=1)
    while d.weekday()>=5 or d in _nyse_cierres(d.year): d-=datetime.timedelta(days=1)
    return d

# ── Calendario de la bolsa: festivos y cierres anticipados (NYSE/Nasdaq) ────────────────────────
def _easter(y):
    a=y%19; b=y//100; c=y%100; d=b//4; e=b%4; f=(b+8)//25; g=(b-f+1)//3; h=(19*a+b-d-g+15)%30
    i=c//4; k=c%4; l=(32+2*e+2*i-h-k)%7; m=(a+11*h+22*l)//451
    mes=(h+l-7*m+114)//31; dia=((h+l-7*m+114)%31)+1
    return datetime.date(y,mes,dia)

def _nth_weekday(y,m,wd,n):
    """n-ésimo día de la semana wd (0=lun) del mes; n=-1 -> el último."""
    if n>0:
        d=datetime.date(y,m,1); d+=datetime.timedelta(days=(wd-d.weekday())%7)
        return d+datetime.timedelta(days=7*(n-1))
    d=datetime.date(y+(m==12),(m%12)+1,1)-datetime.timedelta(days=1)
    return d-datetime.timedelta(days=(d.weekday()-wd)%7)

def _observado(d):
    if d.weekday()==5: return d-datetime.timedelta(days=1)
    if d.weekday()==6: return d+datetime.timedelta(days=1)
    return d

def _nyse_cierres(y):
    """{fecha: nombre} de los días en que NYSE/Nasdaq están cerrados."""
    h={}
    ny=datetime.date(y,1,1)
    if ny.weekday()!=5: h[_observado(ny)]="Año Nuevo"          # si cae en sábado, no se traslada
    h[_nth_weekday(y,1,0,3)]="Martin Luther King"
    h[_nth_weekday(y,2,0,3)]="Día de los Presidentes"
    h[_easter(y)-datetime.timedelta(days=2)]="Viernes Santo"
    h[_nth_weekday(y,5,0,-1)]="Memorial Day"
    h[_observado(datetime.date(y,6,19))]="Juneteenth"
    h[_observado(datetime.date(y,7,4))]="Día de la Independencia"
    h[_nth_weekday(y,9,0,1)]="Labor Day"
    h[_nth_weekday(y,11,3,4)]="Acción de Gracias"
    h[_observado(datetime.date(y,12,25))]="Navidad"
    return h

def _nyse_medio_dia(y):
    """{fecha: nombre} de los cierres anticipados (13:00 ET)."""
    h={_nth_weekday(y,11,3,4)+datetime.timedelta(days=1):"día después de Acción de Gracias"}
    for m,dd,nom in ((12,24,"Nochebuena"),(7,3,"víspera del 4 de julio")):
        d=datetime.date(y,m,dd)
        if d.weekday()<5 and d not in _nyse_cierres(y): h[d]=nom
    return h

def _avisos_mercado(desde,hasta):
    out=[]; d=desde
    while d<=hasta:
        if d.weekday()<5:
            if d in _nyse_cierres(d.year): out.append((d,f"MERCADO CERRADO ({_nyse_cierres(d.year)[d]})",Fore.RED))
            elif d in _nyse_medio_dia(d.year): out.append((d,f"cierre anticipado 13:00 ET ({_nyse_medio_dia(d.year)[d]})",Fore.YELLOW))
        d+=datetime.timedelta(days=1)
    return out

# ── Empresas que ARRASTRAN al mercado (peso alto en SPY/QQQ/DIA, semiconductores, TSLA/GOOGL/META) ──
# Edita esta lista a tu gusto. Además, cada día se añaden las 10 de mayor capitalización que reporten.
_GRANDES={
"AAPL":"Apple","MSFT":"Microsoft","NVDA":"NVIDIA","AMZN":"Amazon","GOOGL":"Alphabet","GOOG":"Alphabet","META":"Meta","TSLA":"Tesla",
"AVGO":"Broadcom","ORCL":"Oracle","NFLX":"Netflix","PLTR":"Palantir","CSCO":"Cisco","IBM":"IBM","CRM":"Salesforce","ADBE":"Adobe",
"NOW":"ServiceNow","INTU":"Intuit","JPM":"JPMorgan","BAC":"Bank of America","GS":"Goldman Sachs","WFC":"Wells Fargo","MS":"Morgan Stanley",
"V":"Visa","MA":"Mastercard","AXP":"American Express","LLY":"Eli Lilly","UNH":"UnitedHealth","JNJ":"Johnson & Johnson","ABBV":"AbbVie",
"WMT":"Walmart","COST":"Costco","HD":"Home Depot","LOW":"Lowe's","MCD":"McDonald's","KO":"Coca-Cola","PEP":"PepsiCo","PG":"Procter & Gamble",
"XOM":"Exxon Mobil","CVX":"Chevron","CAT":"Caterpillar","GE":"GE Aerospace","BA":"Boeing",
# semiconductores
"AMD":"AMD","MU":"Micron","INTC":"Intel","QCOM":"Qualcomm","TXN":"Texas Instruments","ADI":"Analog Devices","MRVL":"Marvell",
"LRCX":"Lam Research","AMAT":"Applied Materials","KLAC":"KLA","TSM":"TSMC","ASML":"ASML","ARM":"Arm"}

# ── ETF de referencia y qué empresas pesan en cada uno (aproximado: edítalo si cambian los índices) ──
_ETFS_REF=["SPY","QQQ","IWM","SOXL","DIA"]
_ETF_SOXL={"NVDA","AVGO","AMD","MU","INTC","QCOM","TXN","ADI","MRVL","LRCX","AMAT","KLAC","TSM","ASML","ARM"}
_ETF_QQQ={"AAPL","MSFT","NVDA","AMZN","GOOGL","GOOG","META","TSLA","AVGO","NFLX","COST","CSCO","PEP","ADBE","AMD","QCOM","TXN",
          "INTU","MU","AMAT","LRCX","KLAC","ADI","MRVL","INTC","PLTR","ASML","ARM","CRWD","WDAY","ADSK"}
_ETF_DIA={"AAPL","AMGN","AMZN","AXP","BA","CAT","CRM","CSCO","CVX","DIS","GS","HD","HON","IBM","JNJ","JPM","KO","MCD","MMM",
          "MRK","MSFT","NKE","NVDA","PG","SHW","TRV","UNH","V","VZ","WMT"}
_NO_SPY={"TSM","ASML","ARM","BHP"}                       # extranjeras: no están en el S&P 500

def _etfs_relacionados(sym):
    """ETF de referencia en los que pesa la empresa, por orden de relevancia (SOXL, QQQ, DIA, SPY). IWM: ninguna grande."""
    out=[]
    if sym in _ETF_SOXL: out.append("SOXL")
    if sym in _ETF_QQQ: out.append("QQQ")
    if sym in _ETF_DIA: out.append("DIA")
    if sym in _GRANDES and sym not in _NO_SPY: out.append("SPY")
    return out

def _etf_txt(e,pct):
    if pct is None: return f"{e} {Fore.LIGHTBLACK_EX}n/d{Style.RESET_ALL}"
    c=Fore.GREEN if pct>0 else Fore.RED if pct<0 else Fore.WHITE
    return f"{e} {c}{_pct_es(pct)}{Style.RESET_ALL}"

def _num(v):
    """'$1.18' -> 1.18 · '($0.03)' -> -0.03 · '$234,931,618,000' -> 2.3e11 · vacío/N/A -> None."""
    import re
    s=str(v if v is not None else "").strip()
    if not s or s.upper() in ("N/A","--"): return None
    neg=s.startswith("(") or s.startswith("-")
    s=re.sub(r"[^0-9.]","",s)
    try: x=float(s)
    except ValueError: return None
    return -x if neg else x

def _nasdaq_headers():
    return dict(_pre_headers("https://www.nasdaq.com/"),**{"Accept":"application/json, text/plain, */*","Origin":"https://www.nasdaq.com"})

def _fetch_nasdaq_day(d):
    try:
        r=requests.get("https://api.nasdaq.com/api/calendar/earnings",params={"date":d.isoformat()},headers=_nasdaq_headers(),timeout=12)
        rows=(((r.json() or {}).get("data") or {}).get("rows")) or []
    except Exception: return None
    out=[]
    for x in rows:
        sym=(x.get("symbol") or "").upper().strip()
        if not sym: continue
        tm=x.get("time") or ""
        out.append(dict(sym=sym,name=x.get("name") or sym,cap=_num(x.get("marketCap")) or 0.0,
                        hora="BO" if tm=="time-pre-market" else "AC" if tm=="time-after-hours" else "?",
                        eps=_num(x.get("eps")),cons=_num(x.get("epsForecast")),fecha=d))
    return out

def fetch_nasdaq_earnings(fechas):
    """Calendario de earnings del Nasdaq por día: ({fecha: [filas]}, ok). ok=False si no respondió ningún día."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex: res=list(ex.map(_fetch_nasdaq_day,fechas))
    return {d:(r or []) for d,r in zip(fechas,res)}, any(r is not None for r in res)

def _eps_empresa(sym,fecha):
    """(eps, consenso) según el Nasdaq para esa fecha de reporte (misma base que el consenso). None si no hay."""
    try:
        r=requests.get(f"https://api.nasdaq.com/api/company/{sym}/earnings-surprise",headers=_nasdaq_headers(),timeout=10)
        rows=((((r.json() or {}).get("data") or {}).get("earningsSurpriseTable") or {}).get("rows")) or []
    except Exception: return None
    clave=f"{fecha.month}/{fecha.day}/{fecha.year}"
    for x in rows:
        if (x.get("dateReported") or "").strip()==clave:
            e=_num(x.get("eps")); c=_num(x.get("consensusForecast"))
            if e is not None and c is not None: return e,c
    return None

def _resultado(row):
    v=_eps_empresa(row["sym"],row["fecha"])
    if v: return v[0],v[1],"empresa"
    # Respaldo: fila del calendario. El Nasdaq tarda en cargar el trimestre nuevo en la ficha de la empresa y
    # esta cifra puede mezclar EPS GAAP y ajustado; por eso se marca con '~' en pantalla.
    if row["eps"] is not None and row["cons"] is not None: return row["eps"],row["cons"],"calendario"
    return None

def _ysym(s): return s.replace("/","-").replace(".","-")

def _limpia_nombre(n):
    """'CrowdStrike Holdings, Inc.' -> 'CrowdStrike' (quita sufijos legales para que quepa en la tabla)."""
    import re
    c=re.sub(r"\s*,?\s*\b(Inc|Incorporated|Corporation|Corp|Company|Co|Ltd|Limited|plc|PLC|N\.V|S\.A|Holdings?|Group|Technologies|Communications)\b\.?","",n)
    c=" ".join(c.replace(" ,",",").split()).strip(" ,&")
    return c or n

def _nombre_corto(row):
    return _GRANDES.get(row["sym"]) or _EARNINGS.get(row["sym"]) or _limpia_nombre(row["name"])

def _seleccion_grandes(rows,n_top=10):
    """GRANDES que reportan + las n_top de mayor capitalización de esas filas. Devuelve [(fila, es_top10_extra)]."""
    top={r["sym"] for r in sorted(rows,key=lambda r:r["cap"],reverse=True)[:n_top]}
    out=[(r,(r["sym"] not in _GRANDES)) for r in rows if r["sym"] in _GRANDES or r["sym"] in top]
    return sorted(out,key=lambda x:x[0]["cap"],reverse=True)

def _mis_tickers():
    """Tickers de tu tickers.txt (junto al script) para marcar los splits que te afectan."""
    ruta=os.path.join(os.path.dirname(os.path.abspath(__file__)),"tickers.txt"); out=set()
    try:
        for ln in open(ruta,encoding="utf-8-sig"):
            ln=ln.split("#")[0].split()
            if ln: out.add(ln[0].upper())
    except Exception: pass
    return out

def fetch_splits():
    """Próximos splits del Nasdaq: [(fecha, símbolo, nombre, ratio, tipo)] o None si falla."""
    import re
    try:
        r=requests.get("https://api.nasdaq.com/api/calendar/splits",headers=_nasdaq_headers(),timeout=12)
        rows=(((r.json() or {}).get("data") or {}).get("rows")) or []
    except Exception: return None
    out=[]
    for x in rows:
        try: d=datetime.datetime.strptime((x.get("executionDate") or "").strip(),"%m/%d/%Y").date()
        except ValueError: continue
        ratio=(x.get("ratio") or "").strip(); tipo="?"
        m=re.match(r"^\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*$",ratio)
        if m:
            a,b=float(m.group(1)),float(m.group(2)); tipo="INVERSO" if a<b else "DIRECTO" if a>b else "?"
        elif "%" in ratio: tipo="DIV. ACCIONES"
        out.append((d,(x.get("symbol") or "").upper(),x.get("name") or "",ratio,tipo))
    return sorted(out)

# ── DATOS (los usan la terminal y la página web) ────────────────────────────────────────────────
def datos_earnings_recientes(cal,hoy,ayer):
    """Earnings de ayer (AC) / hoy (BO) de las grandes + top 10 por capitalización, con resultado, reacción y ETF."""
    from concurrent.futures import ThreadPoolExecutor
    rows=[r for r in cal.get(ayer,[]) if r["hora"]=="AC" or (r["hora"]=="?" and r["eps"] is not None)]
    rows+=[r for r in cal.get(hoy,[]) if r["hora"]=="BO" or (r["hora"]=="?" and r["eps"] is not None)]
    sel=_seleccion_grandes(rows)
    if not sel: return dict(filas=[],etf=[])
    with ThreadPoolExecutor(max_workers=8) as ex:
        res=list(ex.map(lambda x:_resultado(x[0]),sel))
    snaps=fetch_pre_movers([_ysym(x[0]["sym"]) for x in sel]+_ETFS_REF)
    pct=lambda s:(snaps.get(s) or {}).get("pct")
    filas=[]
    for (r,extra),v in zip(sel,res):
        tag=("AC ayer" if r["hora"]=="AC" else "ayer n/d") if r["fecha"]==ayer else ("BO hoy" if r["hora"]=="BO" else "hoy n/d")
        resu=None
        if v:
            eps,cons,fuente=v; dif=eps-cons
            resu=dict(eps=eps,cons=cons,dif=dif,pct=(dif/abs(cons)*100 if cons else None),fuente=fuente)
        sn=snaps.get(_ysym(r["sym"]))
        reac=dict(pct=sn["pct"],fuente=sn["fuente"],hora=sn["hora"]) if sn else None
        filas.append(dict(sym=r["sym"],nombre=_nombre_corto(r),tag=tag,cap=r["cap"],extra=extra,resultado=resu,
                          reaccion=reac,etfs=[(e,pct(e)) for e in _etfs_relacionados(r["sym"])]))
    return dict(filas=filas,etf=[(e,pct(e)) for e in _ETFS_REF])

def datos_noticias(pool,limit=5):
    top=pick_pre_news(pool,limit=limit)
    if not top: return []
    top=sorted(top,key=lambda x:x["dt"],reverse=True)
    try: es=translate_headlines([x["headline"] for x in top])
    except Exception: es=[x["headline"] for x in top]
    out=[]
    for it,txt in zip(top,es):
        e=_utc_to_et(it["dt"])
        out.append(dict(hora=f"{_DIAS_ES[e.weekday()]} {e:%H:%M}",tag=it["tag"] or "OTRA",texto=txt,
                        fuente=it.get("source") or it.get("origin","")))
    return out

def datos_agenda():
    agenda,err,fuente=fetch_pre_agenda()
    filas=[]
    for d,tm,e,p,a,f,pr in sorted(agenda or [],key=lambda row:_agenda_time_value(row[0],row[1])):
        alert,color=_agenda_operational_alert(d,tm)
        filas.append(dict(fecha=d,hora=tm,evento=e,actual=a,cons=f,ant=pr,aviso=alert,
                          nivel="rojo" if color==Fore.RED else "amarillo" if color==Fore.YELLOW else ""))
    return dict(filas=filas,err=err,fuente=fuente)

def datos_earnings_proximos(cal,cal_ok,hoy):
    if not cal_ok: return dict(ok=False,filas=[],falta=[])
    rows=[r for d in [hoy+datetime.timedelta(days=i) for i in range(7)] for r in cal.get(d,[])]
    orden={"BO":0,"AC":1,"?":2}
    sel=sorted(_seleccion_grandes(rows),key=lambda x:(x[0]["fecha"],orden[x[0]["hora"]],-x[0]["cap"]))
    filas=[dict(fecha=r["fecha"],hora={"BO":"BO","AC":"AC","?":"n/d"}[r["hora"]],sym=r["sym"],nombre=_nombre_corto(r),
                cap=r["cap"],extra=extra) for r,extra in sel]
    vistos={f["sym"] for f in filas}
    return dict(ok=True,filas=filas,falta=[t for t in ["AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA"] if t not in vistos])

def datos_splits(hoy,dias=30,max_filas=14):
    sp=fetch_splits()
    if sp is None: return dict(ok=False,filas=[],mas=0,etf=None,dias=dias)
    mios=_mis_tickers()|set(_EARNINGS)|set(_EARNINGS_ETF)|set(_GRANDES)
    prox=[x for x in sp if hoy<=x[0]<=hoy+datetime.timedelta(days=dias)]
    # Los ETF apalancados de Leverage Shares/Themes hacen muchos splits a la vez: se resumen en una línea.
    etf=[x for x in prox if x[1] not in mios and ("leverage shares" in x[2].lower() or "etf trust" in x[2].lower())]
    prox=[x for x in prox if x not in etf]
    prox.sort(key=lambda x:(x[1] not in mios,x[0],x[1]))               # primero los tuyos, luego por fecha
    filas=[dict(fecha=d,sym=s,nombre=n,ratio=r,tipo=t,suyo=s in mios) for d,s,n,r,t in prox[:max_filas]]
    res=None
    if etf:
        fechas=sorted({x[0] for x in etf})
        res=dict(n=len(etf),tipos="/".join(sorted({x[4].lower() for x in etf})),fechas=(fechas[0],fechas[-1]),
                 tickers=[x[1] for x in etf[:8]],mas=len(etf)>8)
    return dict(ok=True,filas=filas,mas=max(0,len(prox)-max_filas),etf=res,dias=dias)

def reunir_premarket(debug=False):
    """Recoge TODOS los datos del resumen premarket en un diccionario (sin imprimir nada)."""
    et=_et_now(); hoy=et.date(); ayer=_prev_bday(hoy)
    fut,live,info=fetch_pre_futures()
    avisos=_avisos_mercado(hoy,hoy+datetime.timedelta(days=7))
    cal,cal_ok=fetch_nasdaq_earnings([ayer]+[hoy+datetime.timedelta(days=i) for i in range(7)])
    base=[r for r in cal.get(ayer,[]) if r["hora"]!="BO"]+cal.get(hoy,[])
    foco=[r for r,_ in _seleccion_grandes(base)][:10]
    pool,conteo=fetch_pre_news_pool(extra_names=[_nombre_corto(r) for r in foco],extra_tickers=[_ysym(r["sym"]) for r in foco[:8]])
    return dict(et=et,hoy=hoy,ayer=ayer,fut=fut,live=live,info=info,avisos=avisos,cal_ok=cal_ok,
                noticias=datos_noticias(pool),
                earnings=(datos_earnings_recientes(cal,hoy,ayer) if cal_ok else None),
                agenda=datos_agenda(),proximos=datos_earnings_proximos(cal,cal_ok,hoy),splits=datos_splits(hoy),
                debug=dict(filas_por_dia={str(d):len(v) for d,v in cal.items()},noticias_origen=conteo))

# ── TERMINAL ────────────────────────────────────────────────────────────────────────────────────
def print_pre_earnings_recientes(E):
    print(f"\n{Fore.CYAN}02{Style.RESET_ALL}  {Fore.YELLOW}{Style.BRIGHT}EARNINGS DE AYER (AC) / HOY (BO) — LAS GRANDES{Style.RESET_ALL}")
    if E is None:
        print(f"  {Fore.RED}Calendario del Nasdaq no disponible ahora mismo; no puedo saber quién reportó.{Style.RESET_ALL}"); return
    if not E["filas"]:
        print(f"  {Fore.LIGHTBLACK_EX}Ninguna de las grandes (ni el top 10 por capitalización) reportó ayer tras el cierre ni reporta hoy antes de abrir.{Style.RESET_ALL}"); return
    print(f"  {Fore.LIGHTBLACK_EX}■ resultado: EPS vs consenso (verde = mejor, rojo = peor)   ■ reacción: precio ahora vs cierre de ayer{Style.RESET_ALL}")
    print(f"  {Style.BRIGHT}ETF de referencia{Style.RESET_ALL} {Fore.LIGHTBLACK_EX}(ahora vs cierre de ayer){Style.RESET_ALL}: "+"  ·  ".join(_etf_txt(e,p) for e,p in E["etf"]))
    print(f"  {Style.BRIGHT}{'TICKER':<7}{'EMPRESA':<26}{'REPORTA':<9}{'CAP.':>7}  {'RESULTADO':<36}REACCIÓN{Style.RESET_ALL}")
    for f in E["filas"]:
        v=f["resultado"]
        if v:
            plano=f"EPS {_n(v['eps'])} vs {_n(v['cons'])}"+(f" ({_pct_es(v['pct'],0)})" if v["pct"] is not None else "")+("~" if v["fuente"]=="calendario" else "")
            rtxt=f"{_cuadro(v['dif'])} {plano:<34}"
        else:
            rtxt=f"{_cuadro(None)} {Fore.LIGHTBLACK_EX}{'pendiente / sin dato':<34}{Style.RESET_ALL}"
        sn=f["reaccion"]
        if sn:
            et={"after-hours":"after-hours","premarket":"premarket","regular":"sesión"}.get(sn["fuente"],sn["fuente"])
            mtxt=f"{_cuadro(sn['pct'],0.0)} {_pct_es(sn['pct'])} {Fore.LIGHTBLACK_EX}{et} {sn['hora']}{Style.RESET_ALL}"
        else: mtxt=f"{_cuadro(None)} {Fore.LIGHTBLACK_EX}sin precio{Style.RESET_ALL}"
        mark=f" {Fore.LIGHTBLACK_EX}★top10{Style.RESET_ALL}" if f["extra"] else ""
        print(f"  {Style.BRIGHT}{f['sym']:<7}{Style.RESET_ALL}{_trim(f['nombre'],24):<26}{f['tag']:<9}{_n(f['cap']/1e9,1):>7}  {rtxt}  {mtxt}{mark}")
        if f["etfs"]: print(f"  {Fore.LIGHTBLACK_EX}{'':<7}↔ pesa en:{Style.RESET_ALL} "+"  ·  ".join(_etf_txt(e,p) for e,p in f["etfs"]))
    print(f"  {Fore.LIGHTBLACK_EX}CAP. en miles de millones de $ · ★top10 = entra por capitalización, no está en tu lista de GRANDES.{Style.RESET_ALL}")
    print(f"  {Fore.LIGHTBLACK_EX}~ = cifra del calendario del Nasdaq (puede mezclar EPS GAAP y ajustado): contrasta con la nota de la empresa antes de fiarte.{Style.RESET_ALL}")

def print_pre_agenda(A):
    print(f"\n{Fore.CYAN}03{Style.RESET_ALL}  {Fore.YELLOW}{Style.BRIGHT}AGENDA USA — ALTO IMPACTO ★★★ — PRÓXIMOS 7 DÍAS — ET{Style.RESET_ALL}")
    if A["filas"]:
        w=min(60,max(29,_ancho()-72))
        print(f"  {Style.BRIGHT}{'FECHA':<11} {'HORA':<9} {'EVENTO':<{w}} {'ACTUAL':>7} {'CONS.':>7} {'ANT.':>7} {'AVISO':<18}{Style.RESET_ALL}")
        print("  "+"-"*(w+66))
        for x in A["filas"]:
            col={"rojo":Fore.RED,"amarillo":Fore.YELLOW}.get(x["nivel"],"")
            alert_text=f"{col}{Style.BRIGHT}{x['aviso']:<18}{Style.RESET_ALL}" if x["aviso"] else " "*18
            print(f"  {_dia_es(x['fecha']):<11} {x['hora']:<9} {_trim(x['evento'],w):<{w}} {_trim(x['actual'],7):>7} {_trim(x['cons'],7):>7} {_trim(x['ant'],7):>7} {alert_text}")
        if A["err"]: print(f"  {Fore.YELLOW}Aviso: {A['err']}; usando {A['fuente']}.{Style.RESET_ALL}")
    elif A["err"]: print(f"  {Fore.RED}{A['err']}.{Style.RESET_ALL}")
    else: print(f"  {Fore.LIGHTBLACK_EX}Sin eventos USA de alto impacto (★★★) en los próximos 7 días.{Style.RESET_ALL}")

def print_pre_earnings_proximos(P,hoy):
    print(f"\n{Fore.CYAN}04{Style.RESET_ALL}  {Fore.YELLOW}{Style.BRIGHT}EARNINGS — PRÓXIMOS 7 DÍAS — LAS GRANDES + TOP 10 POR CAPITALIZACIÓN{Style.RESET_ALL}")
    if not P["ok"]:
        print(f"  {Fore.RED}Calendario del Nasdaq no disponible ahora mismo.{Style.RESET_ALL}"); return
    if not P["filas"]:
        print(f"  {Fore.LIGHTBLACK_EX}Ninguna de las grandes reporta en los próximos 7 días.{Style.RESET_ALL}"); return
    print(f"  {Style.BRIGHT}{'FECHA':<11}{'HORA':<6}{'TICKER':<8}{'EMPRESA':<26}{'CAP.':>7}{Style.RESET_ALL}")
    print("  "+"-"*62)
    for f in P["filas"]:
        col=Style.BRIGHT if f["fecha"]==hoy else ""
        print(f"  {col}{_dia_es(f['fecha']):<11}{f['hora']:<6}{f['sym']:<8}{_trim(f['nombre'],24):<26}{_n(f['cap']/1e9,1):>7}{Style.RESET_ALL}"
              +(f" {Fore.LIGHTBLACK_EX}★top10{Style.RESET_ALL}" if f["extra"] else ""))
    print(f"  {Fore.LIGHTBLACK_EX}BO: antes de abrir · AC: tras el cierre · n/d: hora no confirmada · CAP. en miles de millones de $.{Style.RESET_ALL}")
    if P["falta"]: print(f"  {Fore.LIGHTBLACK_EX}Sin earnings en la ventana: {', '.join(P['falta'])}{Style.RESET_ALL}")

def print_pre_splits(S,hoy):
    print(f"\n{Fore.CYAN}05{Style.RESET_ALL}  {Fore.YELLOW}{Style.BRIGHT}PRÓXIMOS SPLITS — {S['dias']} DÍAS (directos e inversos){Style.RESET_ALL}")
    if not S["ok"]:
        print(f"  {Fore.RED}Calendario de splits del Nasdaq no disponible ahora mismo.{Style.RESET_ALL}"); return
    if not S["filas"] and not S["etf"]:
        print(f"  {Fore.LIGHTBLACK_EX}Sin splits anunciados en los próximos {S['dias']} días.{Style.RESET_ALL}"); return
    print(f"  {Style.BRIGHT}{'FECHA':<11}{'TICKER':<8}{'TIPO':<15}{'RATIO':<9}EMPRESA{Style.RESET_ALL}")
    print("  "+"-"*78)
    for x in S["filas"]:
        cd=Fore.YELLOW+Style.BRIGHT if x["fecha"]==hoy else ""
        ct=Fore.RED if x["tipo"]=="INVERSO" else Fore.GREEN if x["tipo"]=="DIRECTO" else ""
        print(f"  {cd}{('HOY' if x['fecha']==hoy else _dia_es(x['fecha'])):<11}{Style.RESET_ALL}{Style.BRIGHT if x['suyo'] else ''}{x['sym']:<8}{Style.RESET_ALL}"
              f"{ct}{x['tipo']:<15}{Style.RESET_ALL}{x['ratio']:<9}{_trim(x['nombre'],44)}"+(f" {Fore.CYAN}★ tuyo{Style.RESET_ALL}" if x["suyo"] else ""))
    if S["mas"]: print(f"  {Fore.LIGHTBLACK_EX}… y {S['mas']} más (se muestran primero los tuyos, luego por fecha).{Style.RESET_ALL}")
    e=S["etf"]
    if e:
        cuando=_dia_es(e["fechas"][0]) if e["fechas"][0]==e["fechas"][1] else f"{_dia_es(e['fechas'][0])} – {_dia_es(e['fechas'][1])}"
        print(f"  {Fore.LIGHTBLACK_EX}+ {e['n']} ETF apalancados (Leverage Shares/Themes) con split {e['tipos']} el {cuando}: "
              f"{', '.join(e['tickers'])}{'…' if e['mas'] else ''}{Style.RESET_ALL}")
    print(f"  {Fore.LIGHTBLACK_EX}Ratio A : B = A acciones nuevas por B antiguas (1 : 10 = inverso, 3 : 1 = directo). Fuente: Nasdaq.{Style.RESET_ALL}")

def imprimir_premarket(D,debug=False):
    et=D["et"]; hoy=D["hoy"]
    print(f"\n{Fore.CYAN}{'='*82}{Style.RESET_ALL}")
    print(f"{Style.BRIGHT}  WALL STREET HOY — PREMARKET  {_DIAS_LARGO_ES[hoy.weekday()]} {hoy.day} de {_MESES_ES[hoy.month-1]} de {hoy.year} · {et:%H:%M} ET{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'='*82}{Style.RESET_ALL}")
    fut,live,info=D["fut"],D["live"],D["info"]
    title="FUTUROS CME (Yahoo · retraso habitual ≈10 min)" if live else "FUTUROS — MERCADO CERRADO / ÚLTIMA SESIÓN"
    print(f"\n{Fore.YELLOW}{Style.BRIGHT}{title}{Style.RESET_ALL}\n  "+"   ".join(f"{n}: {_pre_signed(v)}" for n,v in fut))
    horas="  ".join(f"{n} {i['hora'] or 'n/d'}" for (n,_),i in zip(fut,info))
    edades=[i["age"] for i in info if i["age"] is not None]
    edad=f" · edad del dato ≈{min(edades):.0f} min" if edades else ""
    print(f"  {Fore.LIGHTBLACK_EX}Variación vs liquidación del día anterior · último dato (hora ET): {horas}{edad}{Style.RESET_ALL}")
    viejos=[n for (n,_),i in zip(fut,info) if live and i["age"] is not None and i["age"]>30]
    if viejos: print(f"  {Fore.YELLOW}⚠ dato con más de 30 min: {', '.join(viejos)}{Style.RESET_ALL}")
    if debug:
        print(f"  {Fore.LIGHTBLACK_EX}{'SÍMBOLO':<8} {'ESTADO':<9} {'ÚLTIMO':>10} {'PREVIO':>10} {'EDAD(min)':>10}{Style.RESET_ALL}")
        for i in info:
            print(f"  {Fore.LIGHTBLACK_EX}{i['symbol']:<8} {i['state']:<9} {str(i['last']):>10} {str(i['prev']):>10} {('%.1f'%i['age']) if i['age'] is not None else 'n/d':>10}{Style.RESET_ALL}")
        print(f"\n  {Fore.LIGHTBLACK_EX}[debug] calendario Nasdaq: {'ok' if D['cal_ok'] else 'SIN RESPUESTA'} · filas por día: {D['debug']['filas_por_dia']} · noticias por origen: {D['debug']['noticias_origen'] or 'ninguna'}{Style.RESET_ALL}")
    for d,txt,col in D["avisos"]:
        print(f"  {col}{Style.BRIGHT}⚠ {_dia_es(d)}: {txt}{Style.RESET_ALL}")

    print(f"\n{Fore.CYAN}01{Style.RESET_ALL}  {Fore.YELLOW}{Style.BRIGHT}NOTICIAS QUE MUEVEN LA APERTURA (desde el cierre de ayer · las más recientes primero){Style.RESET_ALL}")
    if D["noticias"]:
        ancho=max(60,_ancho()-34)
        for n in D["noticias"]:
            col=Fore.YELLOW if n["tag"]=="EARNINGS" else Fore.CYAN
            print(f"  {Fore.LIGHTBLACK_EX}{n['hora']} ET{Style.RESET_ALL} {col}[{n['tag']:<9}]{Style.RESET_ALL} {_trim(n['texto'],ancho)} {Fore.LIGHTBLACK_EX}({n['fuente']}){Style.RESET_ALL}")
    else:
        print(f"  {Fore.LIGHTBLACK_EX}Nada relevante desde el cierre de ayer según los filtros (macro, earnings, tus empresas).{Style.RESET_ALL}")

    print_pre_earnings_recientes(D["earnings"])
    print_pre_agenda(D["agenda"])
    print_pre_earnings_proximos(D["proximos"],hoy)
    print_pre_splits(D["splits"],hoy)
    print(f"\n{Fore.LIGHTBLACK_EX}BO: before open | AC: after close. Fuentes: Yahoo Finance (futuros, precios), Nasdaq (earnings, splits), Google News, Investing.com y Federal Reserve.{Style.RESET_ALL}\n")

def print_premarket_summary(debug=False):
    imprimir_premarket(reunir_premarket(debug),debug)


# ─── AYUDA ────────────────────────────────────────────────────────────────────

def print_help():
    print(f"""
{Fore.CYAN}{'='*60}
  get-info.py — Herramienta de análisis de acciones
{'='*60}{Style.RESET_ALL}

{Fore.YELLOW}USO:{Style.RESET_ALL}
  python get-info.py [FLAGS] TICKER1 [TICKER2 ... TICKER15]

{Fore.YELLOW}FLAGS:{Style.RESET_ALL}
  --pre       Resumen premarket: futuros CME (con la hora y la edad del dato), noticias que
              mueven la apertura (las más recientes primero), earnings de ayer (AC) / hoy (BO)
              de las grandes con cuadrito verde/rojo (resultado vs consenso y reacción del
              precio), agenda USA ★★★ con avisos de festivos, earnings de las grandes de los
              próximos 7 días (+ top 10 por capitalización) y próximos splits.
              La lista de grandes se edita en _GRANDES. Añade --debug para ver datos crudos.
  --target    Muestra solo la tabla resumen de precios y targets.
              No muestra ratings ni insider trading.

  --news      Muestra la tabla resumen + las últimas 5 noticias
              por ticker extraídas de Finviz.

  --below20   Ejecuta el screener de Finviz con los filtros:
                Market Cap >500M, Target Price +50%, Analyst Buy+,
                Price 20% below SMA20, Country USA.
              No requiere tickers como argumento.
              Columna Tend D: velas D (de las 21 previas a la caída) con
                MA20>MA40 y MA100>MA200. Verde = cumple | Amarillo >= 10/21
                | Gris < 10/21 | n/d = histórico insuficiente.
              Columna VolCaida: volumen del día de mayor caída vs la media
                de los 20 días anteriores. Verde = vela roja y volumen
                superior a la media | Rojo = no cumple.

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
  python get-info.py --exp20
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
    if "--pre" in args:
        print_premarket_summary(debug="--debug" in args)
        return

    if "--help" in args:
        print_help()
        sys.exit(0)

    target_only = "--target"  in args
    news_only   = "--news"    in args
    below20     = "--below20" in args
    stbuy       = "--stbuy" in args
    exp20       = "--exp20"   in args
    args = [a for a in args if a not in ("--target", "--news", "--below20", "--exp20", "--stbuy")]

    # Modo --exp20: ejecuta el mismo screener que --below20 y EXPORTA a .txt
    # los tickers en el mismo orden de la tabla (para importar en TC2000).
    if exp20:
        print(f"{Fore.CYAN}Consultando screener Finviz...{Style.RESET_ALL}")
        stocks = fetch_below20_screener()
        if not stocks:
            print(f"{Fore.RED}Sin resultados del screener; no se exporta nada.{Style.RESET_ALL}")
            sys.exit(0)
        path, tickers_exp = export_below20_txt(stocks)
        print(f"{Fore.GREEN}✓ Exportados {len(tickers_exp)} tickers a: {path}{Style.RESET_ALL}")
        print(f"  {', '.join(tickers_exp)}")
        sys.exit(0)

    # Modo --stbuy: Technology + Analyst Recom Strong Buy
    if stbuy:
        print(f"{Fore.CYAN}Consultando screener Finviz...{Style.RESET_ALL}")
        print_stbuy_table(fetch_stbuy_screener())
        sys.exit(0)
    # Modo --below20: screener independiente, no necesita tickers
    if below20:
        print(f"{Fore.CYAN}Consultando screener Finviz...{Style.RESET_ALL}")
        stocks = fetch_below20_screener()
        print_below20_table(stocks)
        sys.exit(0)

    tickers = args

    if not tickers:
        print("Uso: python get-info.py [--pre] [--target] [--news] [--below20] [--stbuy] [--help] TICKER1 [TICKER2 ... TICKER15]")
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
            print(f"Últimas noticias - {ticker}  (Finviz + Yahoo)")
            news, err = get_merged_news(ticker, count=8)
            if not news and err:
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
        print(f"Últimas noticias - {ticker}  (Finviz + Yahoo)")
        news, err = get_merged_news(ticker, count=8)
        if not news and err:
            print(f"  Error: {err}")
        else:
            print_news(ticker, news)

if __name__ == "__main__":
    main()