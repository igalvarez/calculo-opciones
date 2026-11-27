"""
Genera docs/premarket/index.html con el resumen de `get-info.py --pre` en formato web compacto (pensado para móvil)
para publicarlo en GitHub Pages. Usa los MISMOS datos que la terminal (get-info.py: reunir_premarket).

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
import datetime
import html
import importlib.util
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


# ── Helpers de formato ──────────────────────────────────────────────────────────────────────────
def esc(x):
    return html.escape("" if x is None else str(x))


def pct(v, dec=1):
    if v is None:
        return "n/d"
    if round(v, dec) == 0:                      # "−0,0 %" confunde: un valor que redondea a cero va sin signo
        return num(0, dec) + " %"
    return f"{v:+.{dec}f}".replace(".", ",").replace("-", "−") + " %"


def num(x, dec=2):
    return f"{x:.{dec}f}".replace(".", ",")


def sg(v, tol=0.0):
    """Clase de color: p (verde), n (rojo), w (neutro/amarillo), g (sin dato)."""
    if v is None:
        return "g"
    return "p" if v > tol else "n" if v < -tol else "w"


def dia(d):
    return f"{DIAS[d.weekday()]} {d.day} {MESES[d.month - 1]}"


def cap_txt(c):
    return num(c / 1e9, 1) + " mM$" if c else ""


def chip(txt, cls=""):
    return f'<i class="ch {cls}">{esc(txt)}</i>'


def vacio(msg, cls=""):
    return f'<p class="ev0 {cls}">{esc(msg)}</p>'


def sec(titulo, cuerpo, nota=""):
    n = f'<p class="nt">{esc(nota)}</p>' if nota else ""
    return f"<section><h2>{esc(titulo)}</h2>{cuerpo}{n}</section>"


def lista(filas_html):
    return f'<div class="ls">{"".join(filas_html)}</div>'


# ── Secciones ───────────────────────────────────────────────────────────────────────────────────
_CORTO = {"S&P 500": "S&P 500", "NASDAQ 100": "Nasdaq 100", "RUSSELL 2000": "Russell 2000", "DOW": "Dow"}


def h_futuros(D):
    tarjetas = "".join(
        f'<div class="cd mt {sg(p)}"><span class="lb">{esc(_CORTO.get(n, n))}</span><b>{pct(p, 2)}</b>'
        f'<span class="lb">{esc(i["hora"] or "n/d")} ET</span></div>'
        for (n, p), i in zip(D["fut"], D["info"]))
    edades = [i["age"] for i in D["info"] if i["age"] is not None]
    nota = "Vs liquidación del día anterior · futuros CME vía Yahoo" + (f" · retraso ≈{min(edades):.0f} min" if edades else "")
    avisos = "".join(
        f'<div class="av {"n" if "CERRADO" in txt else "w"}">⚠ <b>{esc(dia(d))}</b> · {esc(txt)}</div>' for d, txt, _ in D["avisos"])
    return sec("Futuros" if D["live"] else "Futuros · mercado cerrado", f'<div class="g4">{tarjetas}</div>{avisos}', nota)


_TAG = {"EARNINGS": "w", "MACRO": "a", "FED": "m", "POLÍTICA": "w", "ENERGÍA": "w", "GEOPOL.": "n", "IPO": "m",
        "EMPRESA": "a", "MERCADO": "a", "8-K": "m"}


def h_noticias(D):
    if not D["noticias"]:
        return sec("Noticias que mueven la apertura", vacio("Nada relevante desde el cierre de ayer según los filtros."),
                   " ".join(D.get("noticias_nota", [])))
    filas = []
    for n in D["noticias"]:
        fuentes = n.get("fuentes") or ([n["fuente"]] if n.get("fuente") else [])
        meta = f'{esc(n["hora"])} ET {chip(n["tag"], _TAG.get(n["tag"], ""))}'
        if n.get("n", 1) > 1:
            meta += f' {chip(str(n["n"]) + " fuentes", "p")}'
        txt = esc(n["texto"])
        if n.get("link"):
            txt = f'<a href="{esc(n["link"])}" target="_blank" rel="noopener">{txt}</a>'
        filas.append(f'<div class="nw"><div class="mm">{meta}</div><div>{txt} <span class="fu">{esc(", ".join(fuentes))}</span></div></div>')
    nota = "Medios de primer nivel y fuentes oficiales · desde el cierre de ayer · las más recientes primero. " + " ".join(D.get("noticias_nota", []))
    return sec("Noticias que mueven la apertura", lista(filas), nota.strip())


def h_earnings(D):
    E = D["earnings"]
    t = "Earnings de ayer (AC) y hoy (BO) · las grandes"
    if E is None:
        return sec(t, vacio("Calendario del Nasdaq no disponible ahora mismo.", "n"))
    if not E["filas"]:
        return sec(t, vacio("Ninguna de las grandes (ni el top 10 por capitalización) reportó ayer tras el cierre ni reporta hoy antes de abrir."))
    etf = "".join(f'<span class="et {sg(p)}">{esc(e)} <b>{pct(p)}</b></span>' for e, p in E["etf"])
    cards = []
    for f in E["filas"]:
        v, r = f["resultado"], f["reaccion"]
        if v:
            sub = (pct(v["pct"], 0) + " vs cons.") if v["pct"] is not None else "cons. 0"
            res = f'<div class="bx {sg(v["dif"], 0.005)}"><span class="lb">Resultado</span><b>EPS {num(v["eps"])} vs {num(v["cons"])}</b><span class="lb">{esc(sub)}{" ~" if v["fuente"] == "calendario" else ""}</span></div>'
        else:
            res = '<div class="bx g"><span class="lb">Resultado</span><b>pendiente</b><span class="lb">sin dato aún</span></div>'
        if r:
            et = {"after-hours": "after-hours", "premarket": "premarket", "regular": "sesión"}.get(r["fuente"], r["fuente"])
            rea = f'<div class="bx {sg(r["pct"])}"><span class="lb">Reacción</span><b>{pct(r["pct"])}</b><span class="lb">{esc(et)} {esc(r["hora"])}</span></div>'
        else:
            rea = '<div class="bx g"><span class="lb">Reacción</span><b>n/d</b><span class="lb">sin precio</span></div>'
        rel = ""
        if f["etfs"]:
            rel = '<div class="rl">' + "".join(f'<span class="et {sg(p)}">{esc(e)} <b>{pct(p)}</b></span>' for e, p in f["etfs"]) + "</div>"
        top = chip("★ top 10") if f["extra"] else ""
        cards.append(
            f'<article class="cd em"><div class="hd"><b class="tk">{esc(f["sym"])}</b><span class="nm">{esc(f["nombre"])}</span>'
            f'{chip(f["tag"], "w")}{top}<span class="cp">{cap_txt(f["cap"])}</span></div>'
            f'<div class="du">{res}{rea}</div>{rel}</article>')
    nota = "Resultado: EPS vs consenso. Reacción: precio ahora vs cierre de ayer. «~»: cifra del calendario del Nasdaq (puede mezclar EPS GAAP y ajustado). Cap. en miles de millones de $."
    return sec(t, f'<div class="etr"><span class="lb">ETF</span>{etf}</div><div class="gc">{"".join(cards)}</div>', nota)


def _por_dias(filas, fila_html):
    out, ult = [], None
    for x in filas:
        if x["fecha"] != ult:
            out.append(f'<div class="dy">{esc(dia(x["fecha"]))}</div>')
            ult = x["fecha"]
        out.append(fila_html(x))
    return lista(out)


def h_agenda(D):
    A = D["agenda"]
    t = "Agenda USA · alto impacto ★★★ · 7 días (ET)"
    if not A["filas"]:
        return sec(t, vacio(A["err"], "n") if A["err"] else vacio("Sin eventos USA de alto impacto en los próximos 7 días."))

    def fila(x):
        nums = []
        if x["actual"]:
            nums.append(f'<b>{esc(x["actual"])}</b>')
        if x["cons"]:
            nums.append(f'C {esc(x["cons"])}')
        if x["ant"]:
            nums.append(f'A {esc(x["ant"])}')
        aviso = " " + chip(x["aviso"], "n" if x["nivel"] == "rojo" else "w") if x["aviso"] else ""
        return (f'<div class="ev {"r" if x["nivel"] == "rojo" else "y" if x["nivel"] == "amarillo" else ""}">'
                f'<span class="hr">{esc(x["hora"])}</span><span>{esc(x["evento"])}{aviso}</span>'
                f'<span class="nu">{" · ".join(nums)}</span></div>')
    nota = "Cifras: actual (negrita) · C = consenso · A = anterior."
    if A["err"]:
        nota += f' Aviso: {A["err"]}; usando {A["fuente"]}.'
    return sec(t, _por_dias(A["filas"], fila), nota)


def h_proximos(D):
    P = D["proximos"]
    t = "Earnings · próximos 7 días · las grandes + top 10"
    if not P["ok"]:
        return sec(t, vacio("Calendario del Nasdaq no disponible ahora mismo.", "n"))
    if not P["filas"]:
        return sec(t, vacio("Ninguna de las grandes reporta en los próximos 7 días."))

    def fila(x):
        top = " " + chip("★") if x["extra"] else ""
        return (f'<div class="pe"><span class="hr">{esc(x["hora"])}</span><b>{esc(x["sym"])}</b>'
                f'<span class="nm">{esc(x["nombre"])}{top}</span><span class="cp">{cap_txt(x["cap"])}</span></div>')
    nota = "BO: antes de abrir · AC: tras el cierre · n/d: hora no confirmada. ★ = entra por capitalización."
    if P["falta"]:
        nota += " Sin earnings en la ventana: " + ", ".join(P["falta"]) + "."
    return sec(t, _por_dias(P["filas"], fila), nota)


def h_splits(D):
    S = D["splits"]
    t = f'Próximos splits · {S["dias"]} días · mega caps y tus tickers'
    if not S["ok"]:
        return sec(t, vacio("Calendario de splits del Nasdaq no disponible ahora mismo.", "n"))
    hoy = D["hoy"]
    if not S["filas"]:
        cuerpo = vacio("Sin splits de mega caps ni de tus tickers en los próximos " + str(S["dias"]) + " días.")
    else:
        def fila(x):
            suyo = " " + chip("★ tuyo", "a") if x["suyo"] else ""
            tipo = chip(f'{x["tipo"].lower()} {x["ratio"]}', {"INVERSO": "n", "DIRECTO": "p"}.get(x["tipo"], ""))
            return (f'<div class="sp {"hy" if x["fecha"] == hoy else ""}"><span class="hr">{"HOY" if x["fecha"] == hoy else esc(dia(x["fecha"]))}</span>'
                    f'<b>{esc(x["sym"])}</b><span class="nm">{esc(x["nombre"])}{suyo}</span>{tipo}</div>')
        cuerpo = lista([fila(x) for x in S["filas"]])
    nota = (f'{S["ocultos"]} splits de empresas pequeñas ocultos. ' if S["ocultos"] else "") + \
           "Ratio A : B = A acciones nuevas por B antiguas (1 : 10 = inverso). Fuente: Nasdaq."
    return sec(t, cuerpo, nota)


CSS = (
    ":root{color-scheme:light dark;--bg:#f3f5f8;--c:#fff;--b:#d8dee6;--f:#1b2430;--m:#5f6b7a;--p:#12803c;--n:#c62828;--w:#a15c00;--a:#0b66c3;"
    "--pb:#e3f6ea;--nb:#fde7e7;--wb:#fff2d6;--ab:#e3effc;--gb:#eceff3;--mb:#efe6fb;--mc:#7a3fc0}"
    "@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--c:#161b22;--b:#2d343d;--f:#e6edf3;--m:#8b949e;--p:#3fb950;--n:#ff7b72;--w:#e3b341;"
    "--a:#79c0ff;--pb:#12301d;--nb:#3a1618;--wb:#3a2c0d;--ab:#10283f;--gb:#21262d;--mb:#2a1d40;--mc:#d2a8ff}}"
    "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--f);font:14px/1.35 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;"
    "-webkit-text-size-adjust:100%}.w{max-width:960px;margin:0 auto;padding:10px}"
    "header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:2px 10px}h1{font-size:17px;margin:0}"
    ".s{color:var(--m);font-size:12px}section{margin:14px 0}h2{font-size:11.5px;margin:0 0 5px;text-transform:uppercase;letter-spacing:.05em;color:var(--m)}"
    ".nt{color:var(--m);font-size:11px;margin:5px 0 0}.lb{color:var(--m);font-size:10.5px}"
    ".cd,.ls{background:var(--c);border:1px solid var(--b);border-radius:10px}.cd{padding:7px 9px}.ls{overflow:hidden}"
    ".g4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px}"
    ".mt{display:flex;flex-direction:column;gap:1px;border-left:4px solid var(--m);padding:6px 8px}.mt b{font-size:17px;font-variant-numeric:tabular-nums;white-space:nowrap}"
    ".mt.p{border-color:var(--p)}.mt.p b{color:var(--p)}.mt.n{border-color:var(--n)}.mt.n b{color:var(--n)}"
    ".av{margin-top:6px;padding:5px 9px;border-radius:8px;font-size:12.5px}.av.n{background:var(--nb);color:var(--n)}.av.w{background:var(--wb);color:var(--w)}"
    ".ch{display:inline-block;padding:0 6px;border-radius:99px;font-size:10.5px;font-weight:600;font-style:normal;white-space:nowrap;background:var(--gb);color:var(--m)}"
    ".ch.p{background:var(--pb);color:var(--p)}.ch.n{background:var(--nb);color:var(--n)}.ch.w{background:var(--wb);color:var(--w)}"
    ".ch.a{background:var(--ab);color:var(--a)}.ch.m{background:var(--mb);color:var(--mc)}"
    ".nw{padding:6px 9px;border-bottom:1px solid var(--b)}.nw:last-child,.ev:last-child,.pe:last-child,.sp:last-child{border-bottom:0}"
    ".mm{font-size:11px;color:var(--m);margin-bottom:1px}.fu{color:var(--m);font-size:11px}a{color:inherit;text-decoration:none}a:hover{text-decoration:underline}"
    ".etr{display:flex;flex-wrap:wrap;gap:4px;align-items:center;margin-bottom:6px}"
    ".et{background:var(--c);border:1px solid var(--b);border-radius:7px;padding:0 6px;font-size:11px;white-space:nowrap}.et.p b{color:var(--p)}.et.n b{color:var(--n)}"
    ".gc{display:grid;grid-template-columns:repeat(auto-fill,minmax(285px,1fr));gap:6px}"
    ".hd{display:flex;align-items:center;gap:5px;min-width:0}.tk{font-size:16px}.nm{color:var(--m);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}"
    ".cp{margin-left:auto;color:var(--m);font-size:11px;white-space:nowrap}"
    ".du{display:grid;grid-template-columns:1fr 1fr;gap:5px;margin-top:5px}"
    ".bx{border:1px solid var(--b);border-radius:8px;padding:4px 7px;background:var(--gb);display:flex;flex-direction:column;min-width:0}"
    ".bx b{font-size:13px;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
    ".bx.p{background:var(--pb);border-color:var(--p)}.bx.p b{color:var(--p)}.bx.n{background:var(--nb);border-color:var(--n)}.bx.n b{color:var(--n)}.bx.w{background:var(--wb);border-color:var(--w)}"
    ".rl{display:flex;flex-wrap:wrap;gap:4px;margin-top:5px}"
    # Tarjetas de earnings mas compactas (solo la seccion «Earnings de ayer y hoy»)
    ".gc:has(.em){grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:5px}"
    ".em{padding:4px 7px}.em .tk{font-size:13px}.em .nm{font-size:10.5px}.em .cp{font-size:10px}.em .ch{font-size:9.5px;padding:0 5px}"
    ".em .du{gap:4px;margin-top:3px}.em .bx{padding:2px 5px;border-radius:6px}.em .bx b{font-size:11.5px}.em .lb{font-size:9.5px}"
    ".em .rl{margin-top:3px;gap:3px}.em .et{font-size:10px;padding:0 5px}"
    ".dy{background:var(--ab);color:var(--a);font-size:11.5px;font-weight:600;padding:2px 9px}"
    ".ev,.pe,.sp{display:grid;gap:6px;align-items:baseline;padding:4px 9px;border-bottom:1px solid var(--b);font-size:12.5px}"
    ".ev{grid-template-columns:52px 1fr auto}.ev.r{background:var(--nb)}.ev.y{background:var(--wb)}"
    ".pe{grid-template-columns:28px 44px minmax(0,1fr) auto}.sp{grid-template-columns:50px 46px minmax(0,1fr) auto}.sp.hy{background:var(--wb)}"
    ".hr{color:var(--m);font-size:11.5px;white-space:nowrap}.nu{color:var(--m);font-size:11.5px;text-align:right}.nu b{color:var(--f)}"
    ".ev0{color:var(--m);margin:4px 0}.ev0.n{color:var(--n)}"
    ".up{font:inherit;font-size:11.5px;padding:1px 9px;border-radius:99px;border:1px solid var(--b);background:var(--c);color:var(--f);cursor:pointer}"
    ".up:disabled{opacity:.55;cursor:default}.up:hover:not(:disabled){border-color:var(--a)}.hdr{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center;justify-content:flex-end}"
    "footer{color:var(--m);font-size:11px;margin:14px 0 2px}"
    "@media(max-width:360px){.g4{grid-template-columns:repeat(2,minmax(0,1fr))}}"
)

# Botón "Actualizar": lanza el workflow premarket.yml con la API de GitHub. Necesita un token personal (fine-grained, solo este
# repo, permiso Actions: lectura y escritura) que se pega UNA vez y se guarda solo en el navegador (localStorage). Nunca va en el repo.
BOTON_JS = """(function(){
var b=document.getElementById('up'),st=document.getElementById('upst'),h=document.getElementById('hc');if(!b)return;
var K='premarket_gh_token';
function say(t){st.textContent=t}
function repo(){return b.dataset.repo||(location.hostname.split('.')[0]+'/'+location.pathname.split('/')[1])}
function get(){try{return localStorage.getItem(K)}catch(e){return null}}
function put(v){try{localStorage.setItem(K,v)}catch(e){}}
function del(){try{localStorage.removeItem(K)}catch(e){}}
function esperar(){
  var t0=Date.parse(h&&h.dataset.iso),ini=Date.now();
  var iv=setInterval(function(){
    fetch('estado.json?'+Date.now(),{cache:'no-store'}).then(function(r){return r.json()}).then(function(j){
      if(j&&j.utc&&Date.parse(j.utc)>t0){clearInterval(iv);say('Listo, recargando…');location.reload()}
    }).catch(function(){});
    if(Date.now()-ini>9*60e3){clearInterval(iv);say('Tarda más de lo normal: recarga en unos minutos.');b.disabled=false}
  },15000);
}
b.onclick=function(){
  var t=get();
  if(!t){
    t=prompt('Pega tu token de GitHub (fine-grained, solo este repositorio, permiso Actions: lectura y escritura). Se guarda solo en este navegador.');
    if(!t)return;t=t.trim();put(t);
  }
  b.disabled=true;say('Lanzando…');
  fetch('https://api.github.com/repos/'+repo()+'/actions/workflows/premarket.yml/dispatches',{
    method:'POST',
    headers:{'Accept':'application/vnd.github+json','Authorization':'Bearer '+t,'X-GitHub-Api-Version':'2022-11-28','Content-Type':'application/json'},
    body:JSON.stringify({ref:'main',inputs:{forzar:'true'}})
  }).then(function(r){
    if(r.status===204){say('Lanzado. Se actualiza en ~2-3 min…');esperar()}
    else if(r.status===401||r.status===403||r.status===404){del();say('Token rechazado ('+r.status+'). Pulsa de nuevo para pegar otro.');b.disabled=false}
    else{say('Error '+r.status);b.disabled=false}
  }).catch(function(){say('Sin conexión');b.disabled=false});
};
})();"""

PLANTILLA = (
    '<!doctype html><html lang="es"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Premarket Wall Street</title>'
    "<style>__CSS__</style></head><body><div class=\"w\">"
    '<header><div><h1>Wall Street hoy · Premarket</h1><div class="s">__FECHA__</div></div>'
    '<div class="s hdr">__CHIP__ <span>Actualizado <b>__ACTUALIZADO__</b> <span id="hc" data-iso="__ISO__"></span></span>'
    '<button id="up" class="up" type="button" data-repo="__REPO__" title="Lanza el workflow ahora">↻ Actualizar</button><span id="upst"></span></div></header>'
    "__CUERPO__"
    "<footer>Se actualiza 3 veces por día de mercado (8:00, 8:30 y 9:15 hora de Nueva York; la bolsa abre a las 9:30). "
    "Fuentes: Yahoo Finance (con retraso), Nasdaq, Google News, Investing.com. No es una recomendación de inversión.</footer>"
    "</div><script>(function(){var e=document.getElementById('hc');if(!e)return;var t=Date.parse(e.dataset.iso);if(isNaN(t))return;"
    "function f(){var m=Math.max(0,Math.round((Date.now()-t)/6e4));e.textContent='· '+(m<60?'hace '+m+' min':'hace '+Math.floor(m/60)+' h '+m%60+' min')}"
    "f();setInterval(f,3e4)})()</script><script>__BOTON__</script></body></html>"
)


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
        cuerpo = "".join([h_futuros(D), h_noticias(D), h_earnings(D), h_agenda(D), h_proximos(D), h_splits(D)])
    except Exception:
        sys.stderr.write(traceback.format_exc())
        return salir(False, "ERROR generando el resumen; la pagina anterior se mantiene", codigo=1)

    utc = ahora.astimezone(datetime.timezone.utc)
    fecha = f"{DIAS_LARGO[ahora.weekday()]} {ahora.day} de {MESES_LARGO[ahora.month - 1]} de {ahora.year}"
    actualizado = f"{ahora:%H:%M} ET"
    pagina = (PLANTILLA.replace("__CSS__", CSS).replace("__FECHA__", fecha).replace("__CHIP__", chip("franja " + franja, "a"))
              .replace("__ACTUALIZADO__", actualizado).replace("__ISO__", utc.isoformat())
              .replace("__REPO__", os.environ.get("GITHUB_REPOSITORY", "")).replace("__BOTON__", BOTON_JS).replace("__CUERPO__", cuerpo))

    os.makedirs(a.salida, exist_ok=True)
    with open(os.path.join(a.salida, "index.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(pagina)
    with open(estado_ruta, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"fecha": ahora.date().isoformat(), "franja": franja, "utc": utc.isoformat()}, f)
    return salir(True, f"Pagina generada en {a.salida} ({fecha}, {actualizado}, franja {franja}, {len(pagina) // 1024} KB)")


def salir(generado, mensaje, codigo=0):
    print(mensaje)
    ruta = os.environ.get("GITHUB_OUTPUT")
    if ruta:
        with open(ruta, "a", encoding="utf-8") as f:
            f.write(f"generado={'true' if generado else 'false'}\n")
    return codigo


if __name__ == "__main__":
    sys.exit(main())
