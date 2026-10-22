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
        "Dist20%", "Tend", f"P/B<{B20_MAX_PB:g}", f"Sales>{B20_MIN_SALES:g}", f"Inc>{B20_MIN_INCOME:g}", "Index", "IPO", "New?", "Tend D", "VolCaida",
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
            tend_str,
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
    IDX_TEND  = 7
    IDX_PB    = 8
    IDX_SALES = 9
    IDX_INC   = 10
    IDX_IDX   = 11
    IDX_IPO   = 12
    IDX_VOL   = -1   # columna Vol% eliminada
    IDX_NEW   = 13
    IDX_TRD   = 14
    IDX_VOLC  = 15

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
        print("Uso: python get-info.py [--target] [--news] [--below20] [--stbuy] [--help] TICKER1 [TICKER2 ... TICKER15]")
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