"""
Genera docs/premarket/index.html con el resumen de `get-info.py --pre` (misma salida que ves en la terminal,
con sus colores) para publicarlo en GitHub Pages.

Solo publica en las 3 franjas de antes de la apertura (hora de Nueva York), de lunes a viernes y sin festivos:
    8:00   8:30   9:15      (el mercado abre a las 9:30 ET)

GitHub no es puntual con los cron (y tampoco sabe de horario de verano), asi que el workflow lanza el script en
varios minutos candidatos y es ESTE script el que decide, con la hora real de Nueva York, si toca publicar.

Uso:
    python publicar_premarket.py            # solo publica si estamos en una de las 3 franjas
    python publicar_premarket.py --forzar   # ignora franja, dia y festivos (pruebas / ejecucion manual)
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


# ── ANSI -> HTML ────────────────────────────────────────────────────────────────────────────────
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
        out.append(f'<span class="{" ".join(clases)}">{s}</span>' if clases else s)

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


PLANTILLA = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Premarket Wall Street</title>
<style>
  :root { color-scheme: dark; --bg:#0d1117; --card:#161b22; --bd:#30363d; --fg:#e6edf3; --mut:#8b949e; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
  .wrap { max-width:1100px; margin:0 auto; padding:16px; }
  h1 { font-size:20px; margin:4px 0 2px; }
  .meta { color:var(--mut); font-size:13px; margin:0 0 12px; }
  .meta b { color:var(--fg); }
  .term { background:var(--card); border:1px solid var(--bd); border-radius:8px; padding:12px 14px; margin:0;
          overflow-x:auto; font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace; white-space:pre; }
  .k{color:#6e7681}.r{color:#ff7b72}.g{color:#3fb950}.y{color:#e3b341}.b{color:#79c0ff}.m{color:#d2a8ff}
  .c{color:#56d4dd}.w{color:#e6edf3}.gr{color:#8b949e}.B{font-weight:700}
  footer { color:var(--mut); font-size:12px; margin:12px 0 4px; }
  a { color:#79c0ff; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Wall Street hoy · Premarket</h1>
  <p class="meta">Última actualización: <b>__ACTUALIZADO__</b> · franja __FRANJA__ · <span id="hace" data-iso="__ISO__"></span></p>
<pre class="term">__CUERPO__</pre>
  <footer>Se actualiza 3 veces por día de mercado (8:00, 8:30 y 9:15 hora de Nueva York; la bolsa abre a las 9:30).
  Datos con retraso: no es una recomendación de inversión. · <a href="../">← volver a la página principal</a></footer>
</div>
<script>
  (function () {
    var el = document.getElementById("hace"); if (!el) return;
    var t = Date.parse(el.dataset.iso); if (isNaN(t)) return;
    function pinta() {
      var min = Math.max(0, Math.round((Date.now() - t) / 60000));
      el.textContent = min < 60 ? "hace " + min + " min" : "hace " + Math.floor(min / 60) + " h " + (min % 60) + " min";
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
    a = ap.parse_args()

    ahora = datetime.datetime.now(NY)
    gi = cargar_get_info()
    franja = franja_actual(ahora)
    dia_mercado = ahora.weekday() < 5 and ahora.date() not in gi._nyse_cierres(ahora.year)
    estado_ruta = os.path.join(SALIDA, "estado.json")

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

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            gi.print_premarket_summary()
    except Exception:
        sys.stderr.write(traceback.format_exc())
        return salir(False, "ERROR generando el resumen; la pagina anterior se mantiene", codigo=1)

    cuerpo = ansi_a_html(buf.getvalue()).strip("\n")
    utc = ahora.astimezone(datetime.timezone.utc)
    dias = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    meses = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
    legible = f"{dias[ahora.weekday()]} {ahora.day} {meses[ahora.month - 1]} {ahora.year} · {ahora:%H:%M} ET ({utc:%H:%M} UTC)"
    pagina = (PLANTILLA.replace("__ACTUALIZADO__", legible).replace("__FRANJA__", franja)
              .replace("__ISO__", utc.isoformat()).replace("__CUERPO__", cuerpo))

    os.makedirs(SALIDA, exist_ok=True)
    with open(os.path.join(SALIDA, "index.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(pagina)
    with open(estado_ruta, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"fecha": ahora.date().isoformat(), "franja": franja, "utc": utc.isoformat()}, f)
    return salir(True, f"Pagina generada: docs/premarket/index.html ({legible}, franja {franja})")


def salir(generado, mensaje, codigo=0):
    print(mensaje)
    ruta = os.environ.get("GITHUB_OUTPUT")
    if ruta:
        with open(ruta, "a", encoding="utf-8") as f:
            f.write(f"generado={'true' if generado else 'false'}\n")
    return codigo


if __name__ == "__main__":
    sys.exit(main())
