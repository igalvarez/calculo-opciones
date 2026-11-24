#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
opciones_baseline.py  -  SOLO INFORMATIVO (no opera nada, no envia ordenes)

Que mide, para cada ticker de tu lista, en la cadena de opciones:
  1) DISTANCIA AL ATM de "tu contrato": el contrato mas cercano al ATM cuya prima (mid)
     esta dentro de tu rango (por defecto 0,30 - 0,60), lado OTM, vencimiento mas cercano.
  2) ANCHO BID-ASK de ese contrato (y del contrato ATM), en $ por accion y en $ por contrato.

Con unos dias de datos guardados sabes que es "normal" para cada ticker y franja horaria.
Durante la operacion, `check` compara la cadena de AHORA con esa normalidad y avisa si
esta ANORMAL (contrato muy lejos del ATM, spread muy ancho, o ningun contrato en rango).

IMPORTANTE
  - No hay historico gratuito de bid/ask de opciones: hay que IR GUARDANDO fotos (modo collect).
  - Yahoo Finance da los datos con ~15 min de retraso y algunos bid/ask pueden venir mal:
    valida unos cuantos tickers contra la cadena de tu broker antes de fiarte.

Instalacion:   pip install pandas yfinance          (en Windows: pip install tzdata)

Uso:
  python opciones_baseline.py collect --tickers tickers.txt --loop --every 15
  python opciones_baseline.py report  --tickers tickers.txt
  python opciones_baseline.py check   --tickers tickers.txt
  python opciones_baseline.py html    --tickers tickers.txt --out docs/estado_opciones.html
  python opciones_baseline.py demo                  (prueba con datos simulados, sin internet)

Formato de tickers.txt (uno por linea; el rango de prima por ticker es opcional):
  SPY
  AAPL 0,30 0,60
  # las lineas con # se ignoran
"""
import argparse
import math
import os
import re
import sys
import time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
OPEN_MIN = 9 * 60 + 30     # 9:30 ET en minutos
CLOSE_MIN = 16 * 60        # 16:00 ET en minutos


# ----------------------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------------------
def fmt(x, nd=2):
    """Formatea con coma decimal para la salida por pantalla."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{x:.{nd}f}".replace(".", ",")


def fmt_n(x, nd=1):
    """Entero sin decimales; si no es entero, nd decimales (coma)."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return str(int(round(x))) if abs(x - round(x)) < 1e-9 else fmt(x, nd)


def num(s):
    return float(str(s).replace(",", "."))


def _read_tickers_raw(path):
    """Lee tickers.txt. Devuelve [(symbol, prem_min|None, prem_max|None)]; None = sin rango propio."""
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.split("#")[0].strip()
            if not line:
                continue
            parts = line.replace(";", " ").split()
            if len(parts) == 1 and "," in parts[0]:          # "SPY,QQQ,AAPL" en una linea
                for s_ in parts[0].split(","):
                    if s_.strip():
                        out.append((s_.strip().upper(), None, None))
                continue
            sym = parts[0].upper()
            if len(parts) >= 3:
                out.append((sym, num(parts[1]), num(parts[2])))
            else:
                out.append((sym, None, None))
    if not out:
        sys.exit(f"No encontre tickers en {path}")
    return out


def read_tickers(path, default_min, default_max):
    """Lee tickers.txt. Devuelve lista de (symbol, prem_min, prem_max) rellenando el rango por defecto."""
    return [(s_, a if a is not None else default_min, b if b is not None else default_max)
            for s_, a, b in _read_tickers_raw(path)]


def _norm(txt):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", txt) if unicodedata.category(c) != "Mn").upper().strip()


def read_rp(path):
    """Lee el Excel de la academia (rp.xls; en realidad es HTML). Fuente de verdad de los rangos.
    Devuelve ({TICKER: (min, max, fecha|None)} en $ por ACCION, [avisos]). El Excel esta en $ por contrato (/100)."""
    import html as _html
    txt = open(path, encoding="utf-8-sig", errors="replace").read()
    out, problems, header = {}, [], None
    for tr in re.findall(r"<tr.*?</tr>", txt, re.S | re.I):
        cells = [_html.unescape(re.sub(r"<.*?>", "", c)).strip() for c in re.findall(r"<t[hd].*?</t[hd]>", tr, re.S | re.I)]
        if not cells:
            continue
        if header is None:
            h = [_norm(c) for c in cells]
            if "TICKER" in h and any("OPTIMO" in x for x in h):
                header = {"t": h.index("TICKER"), "r": next(i for i, x in enumerate(h) if "OPTIMO" in x),
                          "f": next((i for i, x in enumerate(h) if "FECHA" in x), None)}
            continue
        if len(cells) <= max(header["t"], header["r"]):
            continue
        tk = cells[header["t"]].strip().upper()
        if not tk:
            continue
        nums = [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", cells[header["r"]])]
        if len(nums) != 2 or not (0 < nums[0] < nums[1]):
            problems.append(f"{tk}: rango no valido en rp.xls ({cells[header['r']]!r})")
            continue
        fecha = None
        if header["f"] is not None and header["f"] < len(cells):
            m = re.search(r"\d{4}-\d{2}-\d{2}", cells[header["f"]])
            fecha = m.group(0) if m else None
        if tk in out:
            problems.append(f"{tk}: aparece repetido en rp.xls (me quedo con la fecha mas reciente)")
            if (fecha or "") < (out[tk][2] or ""):
                continue
        out[tk] = (nums[0] / 100.0, nums[1] / 100.0, fecha)
    if header is None:
        problems.append("rp.xls: no encuentro la cabecera (TICKER / RANGO OPTIMO); revisa el formato")
    return out, problems


def resolve_tickers(path, default_min, default_max, rp_path="rp.xls", verbose=True):
    """Rango de cada ticker. Prioridad: 1) rango escrito en tickers.txt, 2) rp.xls (fuente de verdad),
    3) rango por defecto SOLO si no existe rp.xls. Un ticker sin rango en rp.xls NO se vigila (queda en `missing`).
    Devuelve (lista [(sym, min, max)], missing [sym], fechas {sym: 'AAAA-MM-DD'})."""
    raw = _read_tickers_raw(path)
    have_rp = bool(rp_path) and os.path.exists(rp_path)
    rp, problems = read_rp(rp_path) if have_rp else ({}, [])
    resolved, missing, dates = [], [], {}
    for sym, a, b in raw:
        if a is not None:
            resolved.append((sym, a, b))
        elif sym in rp:
            lo, hi, f = rp[sym]
            resolved.append((sym, lo, hi))
            if f:
                dates[sym] = f
        elif have_rp:
            missing.append(sym)
        else:
            resolved.append((sym, default_min, default_max))
    if verbose:
        if not have_rp:
            print(f"AVISO: no encuentro {rp_path}; uso el rango por defecto ({fmt(default_min)}-{fmt(default_max)}) para todos.")
        for p in problems:
            print("AVISO:", p)
        if missing:
            print(f"AVISO: sin rango en {rp_path}, NO se vigilan: {', '.join(missing)} "
                  "(anade una linea 'SIMBOLO min max' en tickers.txt o el ticker a rp.xls)")
    return resolved, missing, dates


def minutes_since_midnight(dtm):
    return dtm.hour * 60 + dtm.minute


def time_bucket(dtm, size):
    """Franja horaria (etiqueta HH:MM de inicio) en bloques de `size` minutos desde las 9:30."""
    m = max(0, minutes_since_midnight(dtm) - OPEN_MIN)
    t = OPEN_MIN + (m // size) * size
    return f"{t // 60:02d}:{t % 60:02d}"


def dte_bucket(dte):
    return "0DTE" if dte == 0 else ("1-7DTE" if dte <= 7 else "8+DTE")


# Calendario NYSE/Nasdaq (fuente: NYSE, "Holidays & Trading Hours"). Completo hasta CALENDAR_LAST_YEAR;
# de 2027 solo estan las fechas confirmadas. Cada ano hay que revisarlo.
HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 3, 26), date(2027, 11, 25), date(2027, 12, 24),
}
EARLY_CLOSE = {date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)}   # cierran a las 13:00 ET
CALENDAR_LAST_YEAR = 2026


def session_end(d):
    """Minuto de cierre de la sesion de ese dia en ET (None = no hay sesion: fin de semana o festivo)."""
    if d.weekday() >= 5 or d in HOLIDAYS:
        return None
    return 13 * 60 if d in EARLY_CLOSE else CLOSE_MIN


def market_open(now):
    end = session_end(now.date())
    return end is not None and OPEN_MIN <= minutes_since_midnight(now) < end


# Yahoo da las opciones con ~15 min de retraso: una foto antes de las 9:45 ET todavia muestra
# cotizaciones de ANTES de la apertura. Por eso collect no guarda nada antes de esa hora.
DATA_START_MIN = 9 * 60 + 45


def data_open(now):
    end = session_end(now.date())
    if end is None:
        return False
    last = end if end >= CLOSE_MIN else end + 30      # media sesion: datos validos hasta ~13:30
    return DATA_START_MIN <= minutes_since_midnight(now) < last


# ----------------------------------------------------------------------------------------
# Proveedores de datos
# ----------------------------------------------------------------------------------------
class YahooProvider:
    """Datos de Yahoo Finance via yfinance (retraso ~15 min)."""

    def __init__(self):
        try:
            import yfinance as yf
        except ImportError:
            sys.exit("Falta yfinance:  pip install yfinance")
        self.yf = yf
        self._t = {}

    def _tk(self, sym):
        if sym not in self._t:
            self._t[sym] = self.yf.Ticker(sym)
        return self._t[sym]

    def spot(self, sym):
        t = self._tk(sym)
        price = None
        try:
            fi = t.fast_info
            price = getattr(fi, "last_price", None)
            if price is None:
                price = fi["lastPrice"]
        except Exception:
            price = None
        if price is None or (isinstance(price, float) and math.isnan(price)):
            h = t.history(period="1d", interval="1m")
            if len(h):
                price = float(h["Close"].iloc[-1])
        return float(price) if price else None

    def expiries(self, sym):
        return list(self._tk(sym).options)

    def chain(self, sym, expiry):
        ch = self._tk(sym).option_chain(expiry)
        cols = ["strike", "bid", "ask", "impliedVolatility"]
        calls = ch.calls[cols].rename(columns={"impliedVolatility": "iv"})
        puts = ch.puts[cols].rename(columns={"impliedVolatility": "iv"})
        return calls, puts


class MockProvider:
    """Datos SIMULADOS (Black-Scholes simplificado) para probar el script sin internet."""

    BASE = {"SPY": 690, "QQQ": 645, "AAPL": 260, "TSLA": 330, "NVDA": 160, "IWM": 265}

    def __init__(self):
        self.now = datetime.now(ET)
        self.iv_mult = 1.0
        self.spread_mult = 1.0

    @staticmethod
    def _ncdf(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    def _bs(self, S, K, T, sigma, is_call):
        if T <= 0:
            return max(0.0, (S - K) if is_call else (K - S))
        d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        if is_call:
            return S * self._ncdf(d1) - K * self._ncdf(d2)
        return K * self._ncdf(-d2) - S * self._ncdf(-d1)

    def spot(self, sym):
        base = self.BASE.get(sym, 50 + (sum(map(ord, sym)) % 400))
        m = minutes_since_midnight(self.now)
        return base * (1 + 0.003 * math.sin(m / 37.0 + len(sym)))

    def expiries(self, sym):
        d = self.now.date()
        out = [d] if d.weekday() < 5 else []
        f = d + timedelta(days=(4 - d.weekday()) % 7)
        if f not in out:
            out.append(f)
        return [x.isoformat() for x in out]

    def chain(self, sym, expiry):
        S = self.spot(sym)
        exp_dt = datetime.combine(date.fromisoformat(expiry), datetime.min.time(), ET).replace(hour=16)
        mins_left = max((exp_dt - self.now).total_seconds() / 60.0, 1.0)
        T = mins_left / 525600.0
        sigma = 0.25 * self.iv_mult
        step = 0.5 if S < 25 else (1.0 if S < 1000 else 5.0)
        atm = round(S / step) * step
        ks = [atm + i * step for i in range(-int(0.08 * S / step), int(0.08 * S / step) + 1)]
        since_open = max(0, minutes_since_midnight(self.now) - OPEN_MIN)
        open_factor = 1 + 1.5 * math.exp(-since_open / 20.0)
        rows = {True: [], False: []}
        for is_call in (True, False):
            for k in ks:
                mid = self._bs(S, k, T, sigma, is_call)
                rnd = 0.6 + 0.8 * ((hash((sym, k, minutes_since_midnight(self.now), is_call)) % 1000) / 1000.0)
                sp = max(0.01, (0.01 + 0.03 * math.sqrt(max(mid, 0.0))) * open_factor * rnd * self.spread_mult)
                bid = round(max(mid - sp / 2, 0.0), 2)
                ask = round(bid + sp, 2)
                rows[is_call].append({"strike": k, "bid": bid, "ask": ask, "iv": sigma})
        return pd.DataFrame(rows[True]), pd.DataFrame(rows[False])


# ----------------------------------------------------------------------------------------
# Calculo de una "foto" por ticker
# ----------------------------------------------------------------------------------------
def _valid(bid, ask):
    return (not pd.isna(bid)) and (not pd.isna(ask)) and bid > 0 and ask > 0 and ask >= bid


def _quote(df, strike):
    r = df[df["strike"] == strike]
    if r.empty:
        return None
    r = r.iloc[0]
    return float(r["bid"]), float(r["ask"]), float(r["iv"]) if "iv" in r and not pd.isna(r["iv"]) else float("nan")


def compute_rows(sym, spot, expiry, calls, puts, now, pmin, pmax):
    """Devuelve hasta 2 filas (una por lado C/P) con distancia al ATM y spread de tu contrato."""
    strikes = sorted(set(calls["strike"]).union(set(puts["strike"])))
    if not strikes or not spot:
        return []
    atm = min(strikes, key=lambda k: abs(k - spot))
    pos = {k: i for i, k in enumerate(strikes)}

    # --- contrato ATM (call y put) ---
    sps, mids, ivs = [], [], []
    for df in (calls, puts):
        q = _quote(df, atm)
        if q and _valid(q[0], q[1]):
            sps.append(q[1] - q[0])
            mids.append((q[0] + q[1]) / 2)
            if q[2] and q[2] > 0:
                ivs.append(q[2])
    atm_spread = sum(sps) / len(sps) if sps else float("nan")
    atm_mid = sum(mids) / len(mids) if mids else float("nan")
    atm_pct = 100 * atm_spread / atm_mid if sps and atm_mid > 0 else float("nan")
    atm_iv = sum(ivs) / len(ivs) if ivs else float("nan")

    exp_d = date.fromisoformat(expiry)
    dte = (exp_d - now.date()).days
    rows = []
    for side, df in (("C", calls), ("P", puts)):
        cands = []
        for _, r in df.iterrows():
            k = float(r["strike"])
            if side == "C" and k < atm:
                continue
            if side == "P" and k > atm:
                continue
            bid, ask = float(r["bid"]), float(r["ask"])
            if not _valid(bid, ask):
                continue
            mid = (bid + ask) / 2
            if pmin <= mid <= pmax:
                cands.append((abs(k - atm), k, bid, ask, mid))
        base = {
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"), "ticker": sym, "side": side,
            "spot": spot, "expiry": expiry, "dte": dte, "dte_bucket": dte_bucket(dte),
            "atm_strike": atm, "atm_spread": atm_spread, "atm_spread_pct": atm_pct, "atm_iv": atm_iv,
            "prem_min": pmin, "prem_max": pmax,
        }
        if cands:
            _, k, bid, ask, mid = min(cands)
            base.update({
                "in_range": True, "tgt_strike": k, "tgt_bid": bid, "tgt_ask": ask, "tgt_mid": mid,
                "tgt_spread": ask - bid, "tgt_spread_pct": 100 * (ask - bid) / mid,
                "tgt_dist_usd": abs(k - spot), "tgt_dist_pct": 100 * abs(k - spot) / spot,
                "tgt_dist_steps": abs(pos[k] - pos[atm]),
            })
        else:
            base.update({
                "in_range": False, "tgt_strike": float("nan"), "tgt_bid": float("nan"),
                "tgt_ask": float("nan"), "tgt_mid": float("nan"), "tgt_spread": float("nan"),
                "tgt_spread_pct": float("nan"), "tgt_dist_usd": float("nan"),
                "tgt_dist_pct": float("nan"), "tgt_dist_steps": float("nan"),
            })
        rows.append(base)
    return rows


def snapshot(provider, tickers, now, pause=0.0, verbose=True):
    """Toma una foto de todos los tickers. Vencimiento: el mas cercano >= hoy."""
    all_rows = []
    for sym, pmin, pmax in tickers:
        try:
            spot = provider.spot(sym)
            exps = sorted(e for e in provider.expiries(sym) if date.fromisoformat(e) >= now.date())
            if not exps or not spot:
                if verbose:
                    print(f"  {sym}: sin datos (spot o vencimientos)")
                continue
            calls, puts = provider.chain(sym, exps[0])
            rows = compute_rows(sym, spot, exps[0], calls, puts, now, pmin, pmax)
            all_rows += rows
            if verbose:
                ok = [r for r in rows if r["in_range"]]
                print(f"  {sym}: spot {fmt(spot)} | venc {exps[0]} | contratos en rango: {len(ok)}/2")
        except Exception as e:  # un ticker con error no debe parar al resto
            print(f"  {sym}: ERROR {type(e).__name__}: {e}")
        if pause:
            time.sleep(pause)
    return all_rows


def append_csv(path, rows):
    if not rows:
        return
    df = pd.DataFrame(rows)
    if os.path.exists(path):
        # nunca desalinear: se escribe EXACTAMENTE con las columnas de la cabecera existente
        df = df.reindex(columns=list(pd.read_csv(path, nrows=0).columns))
    df.to_csv(path, mode="a", header=not os.path.exists(path), index=False)


# ----------------------------------------------------------------------------------------
# Informe y comprobacion
# ----------------------------------------------------------------------------------------
def load(path, bucket_minutes):
    if not os.path.exists(path):
        sys.exit(f"No existe {path}. Primero ejecuta `collect` (o `demo`).")
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"])
    df["tbucket"] = df["ts"].apply(lambda t: time_bucket(t, bucket_minutes))
    df["tgt_spread_ct"] = df["tgt_spread"] * 100          # $ por contrato
    df["atm_spread_ct"] = df["atm_spread"] * 100
    df["in_range"] = df["in_range"].astype(bool)
    # Si el rango de un ticker cambio (p. ej. se actualizo rp.xls), las fotos con el rango antiguo no son
    # comparables: se ignoran (no se borran del CSV). Vale el rango de la ultima foto de cada ticker.
    df["prem_min"] = df["prem_min"].round(2)
    df["prem_max"] = df["prem_max"].round(2)
    last = df.sort_values("ts").drop_duplicates("ticker", keep="last").set_index("ticker")
    keep = (df["prem_min"] == df["ticker"].map(last["prem_min"])) & (df["prem_max"] == df["ticker"].map(last["prem_max"]))
    return df[keep].copy()


def show(df):
    out = df.copy()
    for c in out.columns:
        if out[c].dtype.kind == "f":
            out[c] = out[c].apply(lambda x: fmt(x, 2))
    print(out.to_string(index=False))


def _order_key(args):
    """Orden de los tickers segun tickers.txt (si existe)."""
    try:
        order = [t[0] for t in read_tickers(args.tickers, args.prem_min, args.prem_max)]
    except (SystemExit, FileNotFoundError, OSError):
        order = []
    return {s: i for i, s in enumerate(order)}


def report_full(df, args, days):
    """Informe completo (antiguo): todos los numeros, por lado."""
    keys = ["ticker", "side", "dte_bucket"] + (["tbucket"] if args.by_time else [])
    rows = []
    for k, g in df.groupby(keys):
        ir = g[g["in_range"]]
        rec = dict(zip(keys, k if isinstance(k, tuple) else (k,)))
        rec.update({
            "fotos": len(g),
            "%en_rango": 100 * len(ir) / len(g),
            "strikes_med": ir["tgt_dist_steps"].median(), "strikes_p90": ir["tgt_dist_steps"].quantile(0.9),
            "dist$_med": ir["tgt_dist_usd"].median(), "dist$_p90": ir["tgt_dist_usd"].quantile(0.9),
            "spread$ct_med": ir["tgt_spread_ct"].median(), "spread$ct_p90": ir["tgt_spread_ct"].quantile(0.9),
            "spread%_med": ir["tgt_spread_pct"].median(),
            "atm_spread$ct_med": g["atm_spread_ct"].median(), "atm_iv_med": g["atm_iv"].median(),
        })
        rows.append(rec)
    print(f"\nBASELINE DETALLADO  |  {days} dias, {len(df)} filas\n")
    show(pd.DataFrame(rows))


def cmd_report(args):
    """Baseline SIMPLE: por ticker, strikes y spread$ normales (tipico y maximo normal)."""
    df = load(args.csv, args.bucket_minutes)
    days = df["ts"].dt.date.nunique()
    if getattr(args, "detalle", False):
        return report_full(df, args, days)
    keys = ["ticker", "dte_bucket"] + (["tbucket"] if args.by_time else [])
    order = _order_key(args)
    rows = []
    for k, g in df.groupby(keys):
        ir = g[g["in_range"]]
        rec = {"ticker": k[0], "venc": k[1]}
        if args.by_time:
            rec["franja"] = k[2]
        rec["strikes normal (tipico / max)"] = (
            f"{fmt_n(ir['tgt_dist_steps'].median())} / {fmt_n(ir['tgt_dist_steps'].quantile(0.9))}"
            if len(ir) else "-")
        rec["spread$ normal (tipico / max)"] = (
            f"{fmt(ir['tgt_spread_ct'].median(), 1)} / {fmt(ir['tgt_spread_ct'].quantile(0.9), 1)}"
            if len(ir) else "-")
        rec["fotos"] = g["ts"].nunique()
        rec["_o"] = order.get(k[0], 999)
        rows.append(rec)
    res = pd.DataFrame(rows).sort_values(["_o", "ticker"]).drop(columns="_o")
    print(f"\nVALORES NORMALES ({days} dias de datos)")
    print("  strikes = cuantos contratos (strikes) hay desde el ATM hasta tu contrato. 1 = el contiguo al ATM")
    print("  spread$ = ancho bid-ask de ese contrato, en $ por contrato")
    print("  tipico = lo habitual (mediana) | max = tope de lo normal (el 90% de las veces esta por debajo)\n")
    print(res.to_string(index=False))
    if days < 5:
        print(f"\nAVISO: solo {days} dias de datos; con 5 o mas el baseline es mas fiable (mejor 10-20).")


def _ratio(r):
    out = 0.0
    for a, n in (("strikes_ahora", "strikes_normal"), ("spread$ct_ahora", "spread$ct_normal")):
        if r.get(a) is not None and r.get(n) is not None and not pd.isna(r.get(a)) and not pd.isna(r.get(n)):
            out = max(out, r[a] / max(r[n], 1.0))
    return out


def status_rows(out):
    """Un veredicto por ticker y vencimiento (estructurado). key: bad / ok / na."""
    groups = {}
    for r in out:
        groups.setdefault((r["ticker"], r["dte"]), []).append(r)
    rows = []
    for (tk, dte), rs in groups.items():
        stressed = [r for r in rs if r["estado"] in ("ANORMAL", "SIN CONTRATO EN RANGO")]
        ok = [r for r in rs if r["estado"] == "normal"]
        if stressed:
            r = max(stressed, key=_ratio)
            if r["estado"] == "ANORMAL":
                key, text = "bad", "ESTRESADO"
            else:
                key, text = "bad", "ESTRESADO (sin contrato en rango)"
            rank = 0
        elif ok:
            r, key, text, rank = max(ok, key=_ratio), "ok", "OK", 1
        elif any(x["estado"] == "sin baseline" for x in rs):
            r, key, text, rank = rs[0], "na", "sin baseline", 2
        else:
            r, key, text, rank = rs[0], "na", "sin contrato (habitual)", 2
        rows.append({
            "ticker": tk, "venc": dte, "key": key, "text": text, "rank": rank, "ratio": _ratio(r),
            "strikes_now": r.get("strikes_ahora"), "strikes_norm": r.get("strikes_normal"),
            "spread_now": r.get("spread$ct_ahora"), "spread_norm": r.get("spread$ct_normal"),
            "motivo": r.get("motivo", ""),
        })
    return rows


def simplify_check(out):
    """Tabla de texto: ESTRESADO / OK / sin baseline / sin contrato (habitual)."""
    rows = []
    for r in sorted(status_rows(out), key=lambda x: (x["rank"], -x["ratio"], x["ticker"])):
        k_now, k_nor, s_now, s_nor = r["strikes_now"], r["strikes_norm"], r["spread_now"], r["spread_norm"]
        rows.append({
            "ticker": r["ticker"], "venc": r["venc"], "estado": r["text"],
            "strikes (ahora / normal)": f"{fmt_n(k_now)} / {fmt_n(k_nor)}" if k_now is not None and not pd.isna(k_now) else "-",
            "spread$ (ahora / normal)": f"{fmt(s_now, 1)} / {fmt(s_nor, 1)}" if s_now is not None and not pd.isna(s_now) else "-",
        })
    return pd.DataFrame(rows)


def evaluate(rows, df, now, args):
    """Compara filas (foto actual) con el baseline guardado `df`. Devuelve una lista de registros."""
    tb = time_bucket(now, args.bucket_minutes)
    min_days = getattr(args, "min_days", 3)
    out = []
    for r in rows:
        allb = df[(df["ticker"] == r["ticker"]) & (df["side"] == r["side"]) &
                  (df["dte_bucket"] == r["dte_bucket"])]
        if "prem_min" in allb.columns and not pd.isna(r.get("prem_min", float("nan"))):
            allb = allb[(allb["prem_min"].round(2) == round(float(r["prem_min"]), 2)) &
                        (allb["prem_max"].round(2) == round(float(r["prem_max"]), 2))]
        base = allb[allb["in_range"]]
        sub = base[base["tbucket"] == tb]
        scope = f"franja {tb}"
        if len(sub) < args.min_n:
            sub, scope = base, "todo el dia"
        rec = {"ticker": r["ticker"], "lado": r["side"], "dte": r["dte_bucket"], "base": scope, "n": len(sub)}
        # dias COMPLETOS anteriores con datos: con menos de min_days no se compara (todo gris)
        days_prev = int(allb[allb["ts"].dt.date < now.date()]["ts"].dt.date.nunique()) if len(allb) else 0
        few_days = days_prev < min_days
        if not bool(r["in_range"]):
            if few_days:
                rec.update({"estado": "sin baseline",
                            "motivo": f"solo {days_prev} dia(s) previos de datos; hacen falta {min_days}"})
                out.append(rec)
                continue
            # ¿Es habitual no tener contrato en rango? Se compara con la MISMA franja horaria si hay datos
            # (cerca del cierre las primas se hunden y es normal que falten contratos en tu rango).
            ref = allb[allb["tbucket"] == tb]
            if len(ref) < args.min_n:
                ref = allb
            usual = 100 * float(ref["in_range"].mean()) if len(ref) else float("nan")
            if len(ref) < 5:
                # sin historial suficiente no se puede decir que sea anormal
                rec.update({"estado": "sin baseline", "motivo": "faltan datos guardados"})
            elif usual < 80:
                rec.update({"estado": "sin contrato en rango (habitual)",
                            "motivo": f"normalmente solo hay contrato en rango en el {fmt(usual, 0)}% de las fotos"})
            else:
                rec.update({"estado": "SIN CONTRATO EN RANGO",
                            "motivo": f"ninguna prima dentro de tu rango (normalmente si hay, {fmt(usual, 0)}% de las fotos)"})
            out.append(rec)
            continue
        cur_k, cur_s = float(r["tgt_dist_steps"]), float(r["tgt_spread"]) * 100
        rec.update({"strikes_ahora": cur_k, "dist$_ahora": r["tgt_dist_usd"], "spread$ct_ahora": cur_s})
        if few_days:
            rec.update({"estado": "sin baseline",
                        "motivo": f"solo {days_prev} dia(s) previos de datos; hacen falta {min_days}"})
            out.append(rec)
            continue
        if len(sub) < 3:
            rec.update({"estado": "sin baseline", "motivo": "faltan datos guardados"})
            out.append(rec)
            continue
        med_k, p90_k = sub["tgt_dist_steps"].median(), sub["tgt_dist_steps"].quantile(0.9)
        med_s, p90_s = sub["tgt_spread_ct"].median(), sub["tgt_spread_ct"].quantile(0.9)
        lim_k, lim_s = max(p90_k, args.factor * med_k), max(p90_s, args.factor * med_s)
        why = []
        # distancia: por encima del tope normal Y al menos 2 strikes mas lejos que lo tipico
        if cur_k > lim_k and cur_k - med_k >= 2:
            why.append(f"{fmt_n(cur_k)} strikes vs normal {fmt_n(med_k)}")
        if cur_s > lim_s:
            why.append(f"spread {fmt(cur_s, 1)}$ vs normal {fmt(med_s, 1)}$")
        rec.update({"strikes_normal": med_k, "spread$ct_normal": med_s,
                    "estado": "ANORMAL" if why else "normal", "motivo": "; ".join(why)})
        out.append(rec)
    return out


def cmd_check(args, provider):
    df = load(args.csv, args.bucket_minutes)
    tickers, _missing, _dates = resolve_tickers(args.tickers, args.prem_min, args.prem_max, args.rp)
    if args.only:
        wanted = {s.strip().upper() for s in args.only.split(",")}
        tickers = [t for t in tickers if t[0] in wanted]
    now = datetime.now(ET) if not hasattr(provider, "now") else provider.now
    if isinstance(provider, YahooProvider) and not market_open(now):
        print("AVISO: mercado cerrado; los bid/ask de Yahoo no son fiables fuera de horario.")
    rows = snapshot(provider, tickers, now, pause=args.pause, verbose=False)
    if args.save:
        append_csv(args.csv, rows)
    out = evaluate(rows, df, now, args)
    print(f"\nCHECK  {now.strftime('%Y-%m-%d %H:%M')} ET")
    if getattr(args, "detalle", False):
        print(f"(ANORMAL si supera max(p90, {fmt(args.factor, 1)} x mediana); strikes: ademas >= 2 por encima de lo tipico)\n")
        show(pd.DataFrame(out))
        return
    print("  OK        = strikes y spread dentro de lo normal")
    print("  ESTRESADO = tu contrato esta mas lejos del ATM (en strikes) o el spread es mas ancho de lo normal (no operar)\n")
    print(simplify_check(out).to_string(index=False))


HTML_TEMPLATE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Opciones: estado ahora</title>
<style>
:root{--bg:#ffffff;--fg:#1f2328;--muted:#656d76;--card:#f6f8fa;--line:#d0d7de;
--ok-bg:#dafbe1;--ok-fg:#116329;--bad-bg:#ffebe9;--bad-fg:#a40e26;--na-bg:#eaeef2;--na-fg:#57606a;--warn-bg:#fff8c5;--warn-fg:#7d4e00}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--card:#161b22;--line:#30363d;
--ok-bg:#12261e;--ok-fg:#56d364;--bad-bg:#2d1214;--bad-fg:#ff7b72;--na-bg:#21262d;--na-fg:#8d96a0;--warn-bg:#3b2e00;--warn-fg:#e3b341}}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1240px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 12px}
.counts{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}
.chip{padding:6px 12px;border-radius:999px;font-weight:600}
.chip.bad{background:var(--bad-bg);color:var(--bad-fg)}
.chip.ok{background:var(--ok-bg);color:var(--ok-fg)}
.chip.na{background:var(--na-bg);color:var(--na-fg)}
.note{background:var(--na-bg);color:var(--na-fg);padding:10px 12px;border-radius:8px;margin:8px 0}
#stale{display:none;background:var(--warn-bg);color:var(--warn-fg);padding:10px 12px;border-radius:8px;margin:8px 0}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;min-width:760px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{background:var(--card);font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted)}
tr:last-child td{border-bottom:0}
td.tk{font-weight:700}
td.muted{color:var(--muted)}
td.why{white-space:normal;min-width:180px}
tr.bad{box-shadow:inset 4px 0 0 var(--bad-fg);background:var(--bad-bg)}
tr.ok{box-shadow:inset 4px 0 0 var(--ok-fg);background:rgba(46,160,67,.10)}
tr.na{box-shadow:inset 4px 0 0 var(--line)}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:700;font-size:13px}
.pill.bad{background:var(--bad-fg);color:var(--bg)}
.pill.ok{background:var(--ok-bg);color:var(--ok-fg)}
.pill.na{background:var(--na-bg);color:var(--na-fg)}
.legend{color:var(--muted);font-size:13px;margin-top:14px}
.legend b{color:var(--fg)}
</style>
</head>
<body>
<div class="wrap">
<h1>Opciones: estado ahora</h1>
<p class="sub">Última foto: <b>%%UPDATED_ET%% ET</b> (%%UPDATED_ES%% España) · <span id="ago"></span><br>
%%DAYS%% día(s) de datos · %%SNAPS%% fotos guardadas · datos de Yahoo con unos 15 min de retraso</p>
<div id="stale">La última foto tiene más de 45 minutos: puede que GitHub haya retrasado la ejecución o que el mercado esté cerrado.</div>
%%NOTE%%
<div class="counts">%%COUNTS%%</div>
<div class="scroll"><table>
<thead><tr><th>Ticker</th><th>Venc.</th><th>Estado</th><th>Strikes<br>ahora / normal</th><th>Spread $<br>ahora / normal</th><th>Normal histórico<br>strikes típ/máx · spread típ/máx</th><th>Rango usado<br>(edad en rp.xls)</th><th>Motivo</th></tr></thead>
<tbody>
%%ROWS%%
</tbody></table></div>
<p class="legend"><b>OK</b> (verde): strikes y spread dentro de lo normal. <b>ESTRESADO</b> (rojo): tu contrato está al menos 2 strikes más lejos del ATM de lo típico y por encima del tope normal, o el spread es más ancho de lo normal. <b>Gris</b>: faltan datos o no suele haber contrato en tu rango. Strikes: 0 = ATM, 1 = el contiguo. Spread en $ por contrato. Normal = misma franja horaria.</p>
</div>
<script>
(function(){
  var t=new Date("%%ISO%%");
  function upd(){
    var m=Math.round((Date.now()-t.getTime())/60000);
    var txt=m<1?"hace menos de 1 min":(m<90?"hace "+m+" min":"hace "+(Math.round(m/6)/10)+" h");
    document.getElementById("ago").textContent=txt;
    document.getElementById("stale").style.display=(m>45)?"block":"none";
  }
  upd(); setInterval(upd,30000);
})();
</script>
</body>
</html>
"""


def cmd_html(args):
    """Genera una pagina HTML estatica con el estado de ahora (verde/rojo) y el baseline."""
    from html import escape
    out_path = args.out
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    if not os.path.exists(args.csv):
        open(out_path, "w", encoding="utf-8").write(
            "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta http-equiv='refresh' content='300'><title>Opciones</title>"
            "<body style='font-family:sans-serif;padding:24px'><h1>Opciones: estado ahora</h1>"
            "<p>Aun no hay datos guardados. La primera foto se guarda cuando el mercado esta abierto.</p></body>")
        print(f"Sin datos todavia: pagina vacia en {out_path}")
        return
    df = load(args.csv, args.bucket_minutes)
    latest = df["ts"].max()
    cur = df[df["ts"] == latest]
    base_df = df[df["ts"] < latest]                      # baseline sin la ultima foto
    now = latest.to_pydatetime()
    out = evaluate(cur.to_dict("records"), base_df, now, args)
    st = status_rows(out)
    order = _order_key(args)
    st.sort(key=lambda r: (r["rank"], -r["ratio"] if r["rank"] == 0 else order.get(r["ticker"], 999), r["ticker"]))

    # baseline por ticker/vencimiento (calls y puts juntos, solo filas con contrato en rango)
    base_ir = base_df[base_df["in_range"]]
    bl = {}
    for (tk, dte), g in base_ir.groupby(["ticker", "dte_bucket"]):
        bl[(tk, dte)] = (f"{fmt_n(g['tgt_dist_steps'].median())} / {fmt_n(g['tgt_dist_steps'].quantile(0.9))} · "
                         f"{fmt(g['tgt_spread_ct'].median(), 1)} / {fmt(g['tgt_spread_ct'].quantile(0.9), 1)}")

    def cell(a, b, nd=None):
        if a is None or pd.isna(a):
            return "-"
        f = (lambda x: fmt(x, nd)) if nd else fmt_n
        return f"{f(a)} / {f(b) if b is not None and not pd.isna(b) else '-'}"

    # rango usado por ticker (el de la ultima foto) y su edad segun rp.xls
    _res, missing, rp_dates = resolve_tickers(args.tickers, args.prem_min, args.prem_max, args.rp, verbose=False)
    rng_txt = {}
    for _, rr in cur.drop_duplicates("ticker").iterrows():
        t_ = f"{fmt(float(rr['prem_min']))}–{fmt(float(rr['prem_max']))}"
        f_ = rp_dates.get(rr["ticker"])
        if f_:
            try:
                t_ += f" · {(now.date() - date.fromisoformat(f_)).days} d"
            except ValueError:
                pass
        else:
            t_ += " · manual"
        rng_txt[rr["ticker"]] = t_

    rows_html = []
    for r in st:
        rows_html.append(
            f'<tr class="{r["key"]}"><td class="tk">{escape(r["ticker"])}</td><td>{escape(r["venc"])}</td>'
            f'<td><span class="pill {r["key"]}">{escape(r["text"])}</span></td>'
            f'<td>{cell(r["strikes_now"], r["strikes_norm"])}</td>'
            f'<td>{cell(r["spread_now"], r["spread_norm"], 1)}</td>'
            f'<td class="muted">{escape(bl.get((r["ticker"], r["venc"]), "-"))}</td>'
            f'<td class="muted">{escape(rng_txt.get(r["ticker"], "-"))}</td>'
            f'<td class="why muted">{escape(str(r["motivo"]) if r["key"] != "ok" else "")}</td></tr>')
    min_days = getattr(args, "min_days", 3)
    days_prev = int(base_df[base_df["ts"].dt.date < now.date()]["ts"].dt.date.nunique())
    note = ""
    if days_prev < min_days:
        note = (f'<div class="note">Todavía no hay suficientes días para comparar: hacen falta {min_days} días anteriores '
                f'y hay {days_prev}. Mientras tanto todo sale en gris, porque verdes y rojos no serían fiables.</div>')
    if missing:
        note += (f'<div class="note">Sin rango en rp.xls, no se vigilan: <b>{escape(", ".join(missing))}</b>. '
                 'Añádelos a rp.xls o escribe su rango en tickers.txt.</div>')
    n_bad = sum(1 for r in st if r["key"] == "bad")
    n_ok = sum(1 for r in st if r["key"] == "ok")
    n_na = sum(1 for r in st if r["key"] == "na")
    counts = (f'<span class="chip bad">{n_bad} estresados</span><span class="chip ok">{n_ok} OK</span>'
              f'<span class="chip na">{n_na} sin datos suficientes</span>')

    et_dt = latest.to_pydatetime().replace(tzinfo=ET)
    page = (HTML_TEMPLATE
            .replace("%%UPDATED_ET%%", et_dt.strftime("%d/%m/%Y %H:%M"))
            .replace("%%UPDATED_ES%%", et_dt.astimezone(ZoneInfo("Europe/Madrid")).strftime("%H:%M"))
            .replace("%%ISO%%", et_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"))
            .replace("%%DAYS%%", str(df["ts"].dt.date.nunique()))
            .replace("%%NOTE%%", note)
            .replace("%%SNAPS%%", str(df["ts"].nunique()))
            .replace("%%COUNTS%%", counts)
            .replace("%%ROWS%%", "\n".join(rows_html)))
    open(out_path, "w", encoding="utf-8").write(page)
    print(f"Pagina generada: {out_path} | {n_bad} estresados, {n_ok} OK, {n_na} sin datos | foto {latest}")


def cmd_collect(args, provider):
    tickers, _missing, _dates = resolve_tickers(args.tickers, args.prem_min, args.prem_max, args.rp)
    while True:
        now = datetime.now(ET)
        if not data_open(now) and not args.force:
            if args.loop and now.weekday() < 5 and minutes_since_midnight(now) < DATA_START_MIN:
                wait = (DATA_START_MIN - minutes_since_midnight(now)) * 60
                print(f"Todavia no hay datos fiables (Yahoo va con retraso). Espero {wait // 60} min hasta las 9:45 ET...")
                time.sleep(wait)
                continue
            print(f"[{now.strftime('%H:%M')} ET] Fuera de horario de datos fiables (9:45-16:00 ET en dias habiles; "
                  "festivos y medias sesiones aparte): no se guarda nada. Usa --force para guardar igualmente.")
            return
        if now.year > CALENDAR_LAST_YEAR:
            print(f"AVISO: el calendario de festivos del script solo esta completo hasta {CALENDAR_LAST_YEAR}; revisalo.")
        print(f"[{now.strftime('%H:%M:%S')} ET] foto de {len(tickers)} tickers...")
        rows = snapshot(provider, tickers, now, pause=args.pause)
        append_csv(args.csv, rows)
        print(f"  guardadas {len(rows)} filas en {args.csv}")
        if not args.loop:
            return
        time.sleep(args.every * 60)


def cmd_demo(args):
    """Prueba completa con datos simulados: 6 dias de historia normal + un check 'estresado'."""
    path = "demo_snapshots.csv"
    if os.path.exists(path):
        os.remove(path)
    prov = MockProvider()
    tickers = [("SPY", 0.30, 0.60), ("AAPL", 0.30, 0.60), ("TSLA", 0.30, 0.60)]
    d = date(2026, 11, 16)
    n_days = 0
    while n_days < 6:
        if d.weekday() < 5:
            for m in range(OPEN_MIN + 5, CLOSE_MIN - 4, 15):
                prov.now = datetime(d.year, d.month, d.day, m // 60, m % 60, tzinfo=ET)
                append_csv(path, snapshot(prov, tickers, prov.now, verbose=False))
            n_days += 1
        d += timedelta(days=1)
    args.csv = path
    print("=== DEMO (datos simulados) ===")
    cmd_report(args)
    # hoy: dia normal y dia con volatilidad implicita x2,5 y spreads x3
    open("demo_tickers.txt", "w").write("SPY 0,30 0,60\nAAPL 0,30 0,60\nTSLA 0,30 0,60\n")   # rango explicito: ignora rp.xls
    args.tickers = "demo_tickers.txt"
    args.only, args.save = None, False
    for label, ivm, spm in (("HOY NORMAL", 1.0, 1.0), ("HOY ESTRESADO (IV x2,5, spread x3)", 2.5, 3.0)):
        prov.now = datetime(2026, 11, 24, 11, 5, tzinfo=ET)
        prov.iv_mult, prov.spread_mult = ivm, spm
        print(f"\n--- {label} ---")
        cmd_check(args, prov)


# ----------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Baseline informativo de opciones (no opera).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--tickers", default="tickers.txt")
        p.add_argument("--rp", default="rp.xls", help="Excel de rangos de la academia (fuente de verdad de los rangos)")
        p.add_argument("--csv", default="options_snapshots.csv")
        p.add_argument("--prem-min", type=num, default=0.30, help="prima minima de tu contrato (def 0,30)")
        p.add_argument("--prem-max", type=num, default=0.60, help="prima maxima de tu contrato (def 0,60)")
        p.add_argument("--provider", choices=["yahoo", "mock"], default="yahoo")
        p.add_argument("--bucket-minutes", type=int, default=30)
        p.add_argument("--pause", type=float, default=0.7, help="segundos entre tickers")

    c = sub.add_parser("collect", help="guarda una foto (o en bucle durante la sesion)")
    common(c)
    c.add_argument("--loop", action="store_true")
    c.add_argument("--every", type=int, default=15, help="minutos entre fotos en --loop")
    c.add_argument("--force", action="store_true", help="guardar aunque el mercado este cerrado")

    r = sub.add_parser("report", help="baseline normal por ticker")
    common(r)
    r.add_argument("--by-time", action="store_true", help="desglosar por franja horaria")
    r.add_argument("--detalle", action="store_true", help="informe completo con todos los numeros")

    k = sub.add_parser("check", help="compara la cadena de AHORA con el baseline")
    common(k)
    k.add_argument("--only", help="solo estos tickers, ej. AAPL,TSLA")
    k.add_argument("--factor", type=num, default=2.0, help="ANORMAL si supera factor x mediana (def 2)")
    k.add_argument("--min-n", type=int, default=5, help="minimo de fotos para usar la misma franja")
    k.add_argument("--min-days", type=int, default=3, help="dias previos de datos para empezar a comparar (def 3)")
    k.add_argument("--save", action="store_true", help="guardar tambien esta foto")
    k.add_argument("--detalle", action="store_true", help="tabla completa con motivos")

    w = sub.add_parser("html", help="genera una pagina HTML con el estado de la ultima foto")
    common(w)
    w.add_argument("--out", default="docs/estado_opciones.html")
    w.add_argument("--factor", type=num, default=2.0)
    w.add_argument("--min-n", type=int, default=5)
    w.add_argument("--min-days", type=int, default=3, help="dias previos de datos para empezar a comparar (def 3)")

    d = sub.add_parser("demo", help="prueba con datos simulados")
    common(d)
    d.add_argument("--by-time", action="store_true")
    d.add_argument("--detalle", action="store_true")
    d.add_argument("--factor", type=num, default=2.0)
    d.add_argument("--min-n", type=int, default=5)
    d.add_argument("--min-days", type=int, default=3)

    args = ap.parse_args()
    if args.cmd == "demo":
        return cmd_demo(args)
    if args.cmd == "report":                    # el informe solo lee el CSV: no necesita internet
        return cmd_report(args)
    if args.cmd == "html":
        return cmd_html(args)
    provider = MockProvider() if args.provider == "mock" else YahooProvider()
    if args.cmd == "collect":
        cmd_collect(args, provider)
    elif args.cmd == "check":
        cmd_check(args, provider)


if __name__ == "__main__":
    main()
