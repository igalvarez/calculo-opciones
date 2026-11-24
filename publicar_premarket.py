"""
Genera docs/premarket/index.html con el resumen de `get-info.py --pre` en formato web (tarjetas) para GitHub Pages.
Usa los MISMOS datos que la terminal (get-info.py: reunir_premarket) y añade al final la versión texto.

Solo publica en las 3 franjas de antes de la apertura (hora de Nueva York), de lunes a viernes y sin festivos:
    8:00   8:30   9:15      (el mercado abre a las 9:30 ET)

GitHub no es puntual con los cron (y tampoco sabe de horario de verano), asi que el workflow lanza el script en
varios minutos candidatos y es ESTE script el que decide, con la hora real de Nueva York, si toca publicar.

Uso:
    python publicar_premarket.py                       # solo publica si estamos en una de las 3 franjas
    python publicar_premarket.py --forzar              # ignora franja, dia y festivos (pruebas / ejecucion manual)
    python publicar_premarket.py --forzar --salida X   # escribe en otra carpeta (para probar sin tocar docs/premarket)
"""
import argparse
import contextlib
import datetime
import html
import importlib.util
import io
import json
import os
import re
import sys
import traceback
from zoneinfo import ZoneInfo

AQUI = os.path.dirname(os.path.abspath(__file__))
SALIDA = os.path.join(AQUI, "docs", "premarket")
NY = ZoneInfo("America/New_York")

# (nombre, desde, hasta) en minutos desde las 00:00 ET; el intervalo es [desde, hasta).
# Los cron estan en :03, :33 y :18 (fuera de los minutos mas saturados de GitHub). Las ventanas excluyen los
# minutos a los que caeria el cron "de la otra hora" (cambio verano/invierno): 8:18, 9:03 y 9:33 ET.
FRANJAS = [("8:00", 8 * 60, 8 * 60 + 18), ("8:30", 8 * 60 + 30, 9 * 60 + 3), ("9:15", 9 * 60 + 15, 9 * 60 + 33)]

DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
DIAS_LARGO = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
MESES_LARGO = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
               "noviembre", "diciembre"]


def franja_actual(ahora_et):
    m = ahora_et.hour * 60 + ahora_et.minute
    for nombre, ini, fin in FRANJAS:
        if ini <= m < fin:
            return nombre
    return None


def cargar_get_info():
    """Importa get-info.py como modulo (su main solo corre con __main__), sin leer los argumentos del sistema."""
    argv = sys.argv
    sys.argv = ["get-info.py"]
    try:
        spec = importlib.util.spec_from_file_location("get_info", os.path.join(AQUI, "get-info.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


# ── ANSI -> HTML (solo para la version texto del final) ────────────────────────────────────────
_COLOR = {30: "k", 31: "r", 32: "g", 33: "y", 34: "b", 35: "m", 36: "c", 37: "w",
          90: "gr", 91: "r", 92: "g", 93: "y", 94: "b", 95: "m", 96: "c", 97: "w"}
_SGR = re.compile(r"\x1b\[([0-9;]*)m")


def ansi_a_html(texto):
    """Convierte los codigos de color de colorama en <span>. Reinicia el estilo en cada salto de linea
    (en la terminal lo hace colorama con autoreset=True)."""
    color, negrita = None, False
    out = []

    def trozo(s):
        if not s:
            return
        s = html.escape(s)
        clases = ([color] if color else []) + (["B"] if negrita else [])
        out.append('<span class="' + " ".join(clases) + '">' + s + "</span>" if clases else s)

    pos = 0
    for m in _SGR.finditer(texto):
        partes = (texto[pos:m.start()]).split("\n")
        for i, p in enumerate(partes):
            trozo(p)
            if i < len(partes) - 1:
                out.append("\n")
                color, negrita = None, False
        pos = m.end()
        for code in (m.group(1) or "0").split(";"):
            n = int(code or 0)
            if n == 0:
                color, negrita = None, False
            elif n == 1:
                negrita = True
            elif n == 22:
                negrita = False
            elif n in _COLOR:
                color = _COLOR[n]
            elif n == 39:
                color = None
    partes = texto[pos:].split("\n")
    for i, p in enumerate(partes):
        trozo(p)
        if i < len(partes) - 1:
            out.append("\n")
            color, negrita = None, False
    return "".join(out)


# ── Helpers de formato ──────────────────────────────────────────────────────────────────────────
def esc(x):
    return html.escape("" if x is None else str(x))


def pct(v, dec=1):
    if v is None:
        return "n/d"
    return f"{v:+.{dec}f}".replace(".", ",").replace("-", "−") + " %"


def num(x, dec=2):
    return f"{x:.{dec}f}".replace(".", ",")


def signo(v, tol=0.0):
    if v is None:
        return "nd"
    return "pos" if v > tol else "neg" if v < -tol else "neu"


def dia_corto(d):
    return f"{DIAS[d.weekday()]} {d.day} {MESES[d.month - 1]}"


def cap_txt(c):
    return num(c / 1e9, 1) + " mil M$"


def vacio(msg, nivel=""):
    return f'<p class="vacio {nivel}">{esc(msg)}</p>'


# ── Secciones ───────────────────────────────────────────────────────────────────────────────────
def h_futuros(D):
    tarjetas = []
    for (nombre, p), i in zip(D["fut"], D["info"]):
        tarjetas.append(
            f'<div class="card metric {signo(p)}"><div class="lbl">{esc(nombre)}</div>'
            f'<div class="big">{pct(p, 2)}</div><div class="sub">dato de las {esc(i["hora"] or "n/d")} ET</div></div>')
    edades = [i["age"] for i in D["info"] if i["age"] is not None]
    nota = "Variación vs liquidación del día anterior · futuros CME vía Yahoo"
    if edades:
        nota += f" · retraso ≈{min(edades):.0f} min"
    titulo = "Futuros" if D["live"] else "Futuros — mercado cerrado / última sesión"
    avisos = "".join(
        f'<div class="aviso {"rojo" if "CERRADO" in txt else "amarillo"}">⚠ <b>{esc(dia_corto(d))}</b>: {esc(txt)}</div>'
        for d, txt, _ in D["avisos"])
    return (f'<section><h2>{esc(titulo)}</h2><div class="grid g4">{"".join(tarjetas)}</div>'
            f'<p class="nota">{esc(nota)}</p>{avisos}</section>')


_TAG = {"EARNINGS": "y", "MACRO": "b", "FED": "m", "POLÍTICA": "o", "ENERGÍA": "o", "GEOPOL.": "r", "IPO": "m",
        "EMPRESA": "c", "MERCADO": "c"}


def h_noticias(D):
    if not D["noticias"]:
        cuerpo = vacio("Nada relevante desde el cierre de ayer según los filtros (macro, earnings, tus empresas).")
    else:
        items = "".join(
            f'<li class="noticia"><div class="nt"><span class="hora">{esc(n["hora"])} ET</span>'
            f'<span class="chip {_TAG.get(n["tag"], "n")}">{esc(n["tag"])}</span></div>'
            f'<div class="tx">{esc(n["texto"])}<span class="fuente"> — {esc(n["fuente"])}</span></div></li>'
            for n in D["noticias"])
        cuerpo = f'<ul class="noticias">{items}</ul>'
    return f'<section><h2>Noticias que mueven la apertura</h2><p class="nota">Desde el cierre de ayer · las más recientes primero</p>{cuerpo}</section>'


def h_earnings(D):
    E = D["earnings"]
    t = '<h2>Earnings de ayer (AC) y hoy (BO) — las grandes</h2>'
    if E is None:
        return f'<section>{t}{vacio("Calendario del Nasdaq no disponible ahora mismo; no puedo saber quién reportó.", "rojo")}</section>'
    if not E["filas"]:
        return f'<section>{t}{vacio("Ninguna de las grandes (ni el top 10 por capitalización) reportó ayer tras el cierre ni reporta hoy antes de abrir.")}</section>'
    etf = "".join(
        f'<div class="etf {signo(p)}"><span class="lbl">{esc(e)}</span><b>{pct(p)}</b></div>' for e, p in E["etf"])
    cards = []
    for f in E["filas"]:
        v, r = f["resultado"], f["reaccion"]
        if v:
            det = f'EPS {num(v["eps"])} vs {num(v["cons"])}'
            sub = (pct(v["pct"], 0) + " vs consenso") if v["pct"] is not None else "consenso 0"
            if v["fuente"] == "calendario":
                sub += " ~"
            caja_res = (f'<div class="box {signo(v["dif"], 0.005)}"><span class="k">Resultado</span>'
                        f'<b>{esc(det)}</b><small>{esc(sub)}</small></div>')
        else:
            caja_res = '<div class="box nd"><span class="k">Resultado</span><b>pendiente</b><small>sin dato aún</small></div>'
        if r:
            et = {"after-hours": "after-hours", "premarket": "premarket", "regular": "sesión"}.get(r["fuente"], r["fuente"])
            caja_rea = (f'<div class="box {signo(r["pct"])}"><span class="k">Reacción</span>'
                        f'<b>{pct(r["pct"])}</b><small>{esc(et)} {esc(r["hora"])}</small></div>')
        else:
            caja_rea = '<div class="box nd"><span class="k">Reacción</span><b>n/d</b><small>sin precio</small></div>'
        rel = ""
        if f["etfs"]:
            rel = ('<div class="rel"><span class="k">pesa en</span>' + "".join(
                f'<span class="chip {signo(p)}">{esc(e)} {pct(p)}</span>' for e, p in f["etfs"]) + "</div>")
        top = '<span class="chip n" title="Entra por capitalización">★ top 10</span>' if f["extra"] else ""
        cards.append(
            f'<article class="card emp"><header><span class="tk">{esc(f["sym"])}</span>'
            f'<span class="nm">{esc(f["nombre"])}</span></header>'
            f'<div class="meta2"><span class="chip y">{esc(f["tag"])}</span>{top}<span class="cap">{cap_txt(f["cap"])}</span></div>'
            f'<div class="duo">{caja_res}{caja_rea}</div>{rel}</article>')
    nota = ("Resultado: EPS frente al consenso (verde = mejor, rojo = peor). Reacción: precio ahora frente al cierre de ayer. "
            "«~» = cifra del calendario del Nasdaq: puede mezclar EPS GAAP y ajustado; contrástala con la nota de la empresa.")
    return (f'<section>{t}<div class="etfs"><span class="k">ETF de referencia</span>{etf}</div>'
            f'<div class="grid cards">{"".join(cards)}</div><p class="nota">{esc(nota)}</p></section>')


def _tabla_por_dias(filas, cabecera, celdas, ncols):
    out, ult = [], None
    for x in filas:
        if x["fecha"] != ult:
            out.append(f'<tr class="dia"><th colspan="{ncols}">{esc(dia_corto(x["fecha"]))}</th></tr>')
            ult = x["fecha"]
        out.append(celdas(x))
    return (f'<div class="tabla"><table><thead><tr>{cabecera}</tr></thead><tbody>{"".join(out)}</tbody></table></div>')


def h_agenda(D):
    A = D["agenda"]
    t = '<h2>Agenda USA — alto impacto ★★★ · próximos 7 días (ET)</h2>'
    if not A["filas"]:
        cuerpo = vacio(A["err"], "rojo") if A["err"] else vacio("Sin eventos USA de alto impacto en los próximos 7 días.")
    else:
        def fila(x):
            aviso = f' <span class="chip {x["nivel"] or "n"}">{esc(x["aviso"])}</span>' if x["aviso"] else ""
            return (f'<tr class="{x["nivel"]}"><td class="hora">{esc(x["hora"])}</td><td>{esc(x["evento"])}{aviso}</td>'
                    f'<td class="n">{esc(x["actual"])}</td><td class="n">{esc(x["cons"])}</td><td class="n">{esc(x["ant"])}</td></tr>')
        cuerpo = _tabla_por_dias(A["filas"], '<th>Hora</th><th>Evento</th><th class="n">Actual</th><th class="n">Cons.</th><th class="n">Ant.</th>', fila, 5)
        if A["err"]:
            cuerpo += vacio(f'Aviso: {A["err"]}; usando {A["fuente"]}.', "amarillo")
    return f"<section>{t}{cuerpo}</section>"


def h_proximos(D):
    P = D["proximos"]
    t = '<h2>Earnings — próximos 7 días · las grandes + top 10 por capitalización</h2>'
    if not P["ok"]:
        return f'<section>{t}{vacio("Calendario del Nasdaq no disponible ahora mismo.", "rojo")}</section>'
    if not P["filas"]:
        return f'<section>{t}{vacio("Ninguna de las grandes reporta en los próximos 7 días.")}</section>'

    def fila(x):
        top = ' <span class="chip n">★ top 10</span>' if x["extra"] else ""
        return (f'<tr><td class="hora">{esc(x["hora"])}</td><td><b>{esc(x["sym"])}</b></td>'
                f'<td>{esc(x["nombre"])}{top}</td><td class="n">{cap_txt(x["cap"])}</td></tr>')
    cuerpo = _tabla_por_dias(P["filas"], '<th>Hora</th><th>Ticker</th><th>Empresa</th><th class="n">Capitalización</th>', fila, 4)
    notas = "BO: antes de abrir · AC: tras el cierre · n/d: hora no confirmada."
    if P["falta"]:
        notas += " Sin earnings en la ventana: " + ", ".join(P["falta"]) + "."
    return f'<section>{t}{cuerpo}<p class="nota">{esc(notas)}</p></section>'


def h_splits(D):
    S = D["splits"]
    t = f'<h2>Próximos splits — {S["dias"]} días</h2>'
    if not S["ok"]:
        return f'<section>{t}{vacio("Calendario de splits del Nasdaq no disponible ahora mismo.", "rojo")}</section>'
    if not S["filas"] and not S["etf"]:
        return f'<section>{t}{vacio("Sin splits anunciados en los próximos " + str(S["dias"]) + " días.")}</section>'
    hoy = D["hoy"]
    filas = []
    for x in S["filas"]:
        cls = {"INVERSO": "neg", "DIRECTO": "pos"}.get(x["tipo"], "n")
        suyo = ' <span class="chip c">★ tuyo</span>' if x["suyo"] else ""
        cuando = "HOY" if x["fecha"] == hoy else dia_corto(x["fecha"])
        filas.append(f'<tr class="{"hoy" if x["fecha"] == hoy else ""}"><td class="hora">{esc(cuando)}</td><td><b>{esc(x["sym"])}</b></td>'
                     f'<td><span class="chip {cls}">{esc(x["tipo"].lower())}</span></td><td>{esc(x["ratio"])}</td>'
                     f'<td>{esc(x["nombre"])}{suyo}</td></tr>')
    tabla = (f'<div class="tabla"><table><thead><tr><th>Fecha</th><th>Ticker</th><th>Tipo</th><th>Ratio</th><th>Empresa</th></tr></thead>'
             f'<tbody>{"".join(filas)}</tbody></table></div>') if filas else ""
    extra = ""
    if S["mas"]:
        extra += vacio(f'… y {S["mas"]} más (se muestran primero los tuyos, luego por fecha).')
    e = S["etf"]
    if e:
        a, b = e["fechas"]
        cuando = dia_corto(a) if a == b else dia_corto(a) + " – " + dia_corto(b)
        extra += (f'<p class="nota">+ {e["n"]} ETF apalancados (Leverage Shares/Themes) con split {esc(e["tipos"])} el {esc(cuando)}: '
                  f'{esc(", ".join(e["tickers"]))}{"…" if e["mas"] else ""}</p>')
    extra += '<p class="nota">Ratio A : B = A acciones nuevas por B antiguas (1 : 10 = inverso, 3 : 1 = directo). Fuente: Nasdaq.</p>'
    return f"<section>{t}{tabla}{extra}</section>"


CSS = """
:root { color-scheme: light dark;
  --bg:#f3f5f8; --card:#ffffff; --bd:#d8dee6; --fg:#1b2430; --mut:#5f6b7a;
  --pos:#12803c; --neg:#c62828; --warn:#a15c00; --neu:#6b7280; --acc:#0b66c3;
  --pos-bg:#e3f6ea; --neg-bg:#fde7e7; --warn-bg:#fff2d6; --neu-bg:#eceff3; --acc-bg:#e3effc; --term:#101418; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#0d1117; --card:#161b22; --bd:#2d343d; --fg:#e6edf3; --mut:#8b949e;
  --pos:#3fb950; --neg:#ff7b72; --warn:#e3b341; --neu:#9aa4af; --acc:#79c0ff;
  --pos-bg:#12301d; --neg-bg:#3a1618; --warn-bg:#3a2c0d; --neu-bg:#21262d; --acc-bg:#10283f; --term:#0a0d12; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
.wrap { max-width:1180px; margin:0 auto; padding:16px; }
header.top { display:flex; flex-wrap:wrap; gap:8px 16px; align-items:flex-end; justify-content:space-between; margin-bottom:8px; }
h1 { font-size:22px; margin:0; letter-spacing:-.01em; }
.fecha { color:var(--mut); font-size:14px; }
.estado { display:flex; flex-wrap:wrap; gap:6px; align-items:center; font-size:13px; color:var(--mut); }
section { margin:22px 0; }
h2 { font-size:15px; margin:0 0 8px; text-transform:uppercase; letter-spacing:.05em; color:var(--mut); font-weight:700; }
.nota { color:var(--mut); font-size:12.5px; margin:8px 0 0; }
.grid { display:grid; gap:10px; }
.g4 { grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); }
.cards { grid-template-columns:repeat(auto-fill,minmax(310px,1fr)); }
.card { background:var(--card); border:1px solid var(--bd); border-radius:12px; padding:12px 14px; }
.metric .lbl { font-size:12px; color:var(--mut); text-transform:uppercase; letter-spacing:.05em; font-weight:600; }
.metric .big { font-size:28px; font-weight:750; line-height:1.15; margin:2px 0; font-variant-numeric:tabular-nums; }
.metric .sub { font-size:12px; color:var(--mut); }
.metric.pos { border-left:5px solid var(--pos); } .metric.neg { border-left:5px solid var(--neg); } .metric.neu,.metric.nd { border-left:5px solid var(--neu); }
.metric.pos .big { color:var(--pos); } .metric.neg .big { color:var(--neg); }
.chip { display:inline-block; padding:1px 8px; border-radius:999px; font-size:12px; font-weight:600; white-space:nowrap;
        background:var(--neu-bg); color:var(--neu); }
.chip.pos,.chip.verde { background:var(--pos-bg); color:var(--pos); } .chip.neg,.chip.rojo,.chip.r { background:var(--neg-bg); color:var(--neg); }
.chip.y,.chip.amarillo,.chip.o,.chip.neu { background:var(--warn-bg); color:var(--warn); }
.chip.b,.chip.c { background:var(--acc-bg); color:var(--acc); } .chip.m { background:#efe6fb; color:#7a3fc0; }
@media (prefers-color-scheme: dark) { .chip.m { background:#2a1d40; color:#d2a8ff; } }
.aviso { margin:8px 0 0; padding:8px 12px; border-radius:10px; font-size:13.5px; border:1px solid transparent; }
.aviso.rojo { background:var(--neg-bg); color:var(--neg); } .aviso.amarillo { background:var(--warn-bg); color:var(--warn); }
.noticias { list-style:none; margin:0; padding:0; display:grid; gap:8px; }
.noticia { background:var(--card); border:1px solid var(--bd); border-radius:12px; padding:10px 14px; display:grid; grid-template-columns:150px 1fr; gap:10px; }
.noticia .nt { display:flex; flex-direction:column; gap:4px; align-items:flex-start; }
.hora { color:var(--mut); font-size:12.5px; font-variant-numeric:tabular-nums; white-space:nowrap; }
.fuente { color:var(--mut); font-size:12.5px; }
.etfs { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:10px; }
.etf { background:var(--card); border:1px solid var(--bd); border-radius:10px; padding:4px 12px; display:flex; gap:8px; align-items:baseline; }
.etf .lbl { font-size:12px; color:var(--mut); font-weight:700; } .etf b { font-variant-numeric:tabular-nums; }
.etf.pos b { color:var(--pos); } .etf.neg b { color:var(--neg); }
.k { color:var(--mut); font-size:11.5px; text-transform:uppercase; letter-spacing:.05em; font-weight:600; }
.emp header { display:flex; align-items:baseline; gap:8px; }
.tk { font-size:20px; font-weight:800; } .nm { color:var(--mut); font-size:13.5px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0; }
.noticia .tx { min-width:0; overflow-wrap:anywhere; }
.meta2 { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin:4px 0 10px; }
.cap { margin-left:auto; color:var(--mut); font-size:12.5px; }
.duo { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
.box { border-radius:10px; padding:8px 10px; border:1px solid var(--bd); background:var(--neu-bg); display:flex; flex-direction:column; gap:1px; min-width:0; }
.box b { font-size:15.5px; font-variant-numeric:tabular-nums; } .box small { color:var(--mut); font-size:12px; }
.box.pos { background:var(--pos-bg); border-color:var(--pos); } .box.pos b { color:var(--pos); }
.box.neg { background:var(--neg-bg); border-color:var(--neg); } .box.neg b { color:var(--neg); }
.box.neu { background:var(--warn-bg); border-color:var(--warn); }
.rel { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-top:10px; }
.tabla { background:var(--card); border:1px solid var(--bd); border-radius:12px; overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th,td { padding:7px 12px; text-align:left; vertical-align:top; border-bottom:1px solid var(--bd); }
thead th { color:var(--mut); font-size:12px; text-transform:uppercase; letter-spacing:.05em; background:var(--neu-bg); }
tr.dia th { background:var(--acc-bg); color:var(--acc); font-size:12.5px; text-transform:none; letter-spacing:0; }
td.n,th.n { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
tr.rojo td { background:var(--neg-bg); } tr.amarillo td { background:var(--warn-bg); } tr.hoy td { background:var(--warn-bg); }
tbody tr:last-child td { border-bottom:0; }
.vacio { color:var(--mut); margin:6px 0; } .vacio.rojo { color:var(--neg); } .vacio.amarillo { color:var(--warn); }
details { margin-top:22px; } summary { cursor:pointer; color:var(--mut); font-size:13px; }
.term { background:var(--term); color:#e6edf3; border-radius:10px; padding:12px 14px; margin:8px 0 0; overflow-x:auto;
        font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace; white-space:pre; }
.term .k{color:#6e7681}.term .r{color:#ff7b72}.term .g{color:#3fb950}.term .y{color:#e3b341}.term .b{color:#79c0ff}.term .m{color:#d2a8ff}
.term .c{color:#56d4dd}.term .w{color:#e6edf3}.term .gr{color:#8b949e}.term .B{font-weight:700}
footer { color:var(--mut); font-size:12px; margin:22px 0 4px; }
@media (max-width:640px) { .noticia { grid-template-columns:1fr; } .duo { grid-template-columns:1fr; } .metric .big { font-size:24px; } }
"""

PLANTILLA = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Premarket Wall Street</title>
<style>__CSS__</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div><h1>Wall Street hoy · Premarket</h1><div class="fecha">__FECHA__</div></div>
    <div class="estado"><span class="chip c">franja __FRANJA__</span><span>Actualizado __ACTUALIZADO__</span><span id="hace" data-iso="__ISO__"></span></div>
  </header>
__CUERPO__
  <details><summary>Ver versión texto (la misma salida que la terminal)</summary><pre class="term">__TEXTO__</pre></details>
  <footer>Se actualiza 3 veces por día de mercado (8:00, 8:30 y 9:15 hora de Nueva York; la bolsa abre a las 9:30).
  Fuentes: Yahoo Finance (futuros y precios, con retraso), Nasdaq (earnings y splits), Google News, Investing.com. No es una recomendación de inversión.</footer>
</div>
<script>
  (function () {
    var el = document.getElementById("hace"); if (!el) return;
    var t = Date.parse(el.dataset.iso); if (isNaN(t)) return;
    function pinta() {
      var min = Math.max(0, Math.round((Date.now() - t) / 60000));
      el.textContent = "· " + (min < 60 ? "hace " + min + " min" : "hace " + Math.floor(min / 60) + " h " + (min % 60) + " min");
    }
    pinta(); setInterval(pinta, 30000);
  })();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--forzar", action="store_true", help="ignora franja, dia de mercado y 'ya publicado'")
    ap.add_argument("--salida", default=SALIDA, help="carpeta de salida (por defecto docs/premarket)")
    a = ap.parse_args()

    ahora = datetime.datetime.now(NY)
    gi = cargar_get_info()
    franja = franja_actual(ahora)
    dia_mercado = ahora.weekday() < 5 and ahora.date() not in gi._nyse_cierres(ahora.year)
    estado_ruta = os.path.join(a.salida, "estado.json")

    if not a.forzar:
        if not dia_mercado:
            return salir(False, f"NY {ahora:%a %d %b %H:%M}: no es dia de mercado; no se publica")
        if franja is None:
            return salir(False, f"NY {ahora:%H:%M}: fuera de las franjas 8:00 / 8:30 / 9:15; no se publica")
        try:
            prev = json.load(open(estado_ruta, encoding="utf-8"))
        except Exception:
            prev = {}
        if prev.get("fecha") == ahora.date().isoformat() and prev.get("franja") == franja:
            return salir(False, f"La franja {franja} de hoy ya esta publicada; no se repite")
    franja = franja or "manual"

    try:
        D = gi.reunir_premarket()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gi.imprimir_premarket(D)
        cuerpo = "\n".join([h_futuros(D), h_noticias(D), h_earnings(D), h_agenda(D), h_proximos(D), h_splits(D)])
    except Exception:
        sys.stderr.write(traceback.format_exc())
        return salir(False, "ERROR generando el resumen; la pagina anterior se mantiene", codigo=1)

    utc = ahora.astimezone(datetime.timezone.utc)
    fecha = f"{DIAS_LARGO[ahora.weekday()]} {ahora.day} de {MESES_LARGO[ahora.month - 1]} de {ahora.year}"
    actualizado = f"{ahora:%H:%M} ET ({utc:%H:%M} UTC)"
    pagina = (PLANTILLA.replace("__CSS__", CSS).replace("__FECHA__", fecha).replace("__FRANJA__", franja)
              .replace("__ACTUALIZADO__", actualizado).replace("__ISO__", utc.isoformat())
              .replace("__CUERPO__", cuerpo).replace("__TEXTO__", ansi_a_html(buf.getvalue()).strip("\n")))

    os.makedirs(a.salida, exist_ok=True)
    with open(os.path.join(a.salida, "index.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(pagina)
    with open(estado_ruta, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"fecha": ahora.date().isoformat(), "franja": franja, "utc": utc.isoformat()}, f)
    return salir(True, f"Pagina generada en {a.salida} ({fecha}, {actualizado}, franja {franja})")


def salir(generado, mensaje, codigo=0):
    print(mensaje)
    ruta = os.environ.get("GITHUB_OUTPUT")
    if ruta:
        with open(ruta, "a", encoding="utf-8") as f:
            f.write(f"generado={'true' if generado else 'false'}\n")
    return codigo


if __name__ == "__main__":
    sys.exit(main())
