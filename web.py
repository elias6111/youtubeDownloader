# Versión web del descargador: python web.py  →  abre http://localhost:8765
# Solo escucha en este ordenador (127.0.0.1). Usa yt-dlp y ffmpeg.
import json
import os
import subprocess
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PUERTO = 8765
AQUI = os.path.dirname(os.path.abspath(__file__))
DESTINO = os.path.join(os.path.expanduser("~"), "Downloads", "YouTube")
CONFIG = os.path.join(AQUI, "config.json")
trabajos = {}


def asegurar_yt_dlp():
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        print("Instalando yt-dlp...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"])


def leer_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"carpeta": DESTINO}


def guardar_config(c):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)


# YouTube necesita ejecutar JavaScript para dar todas las calidades: se usa el Node.js instalado
BASE = {"quiet": True, "no_warnings": True, "js_runtimes": {"node": {}}}


def info(enlace):
    import yt_dlp

    with yt_dlp.YoutubeDL({**BASE, "skip_download": True, "noplaylist": True, "extract_flat": "in_playlist"}) as ydl:
        i = ydl.extract_info(enlace, download=False)
    if i.get("_type") == "playlist":
        return {"lista": True, "titulo": i.get("title"), "num": len(i.get("entries") or []), "alturas": [2160, 1440, 1080, 720, 480, 360]}
    alturas = sorted({f["height"] for f in i.get("formats", []) if f.get("height") and f.get("vcodec") != "none"}, reverse=True)
    subs = sorted(set(i.get("subtitles") or {}) | set(i.get("automatic_captions") or {}))
    return {
        "lista": False,
        "titulo": i.get("title"),
        "canal": i.get("uploader"),
        "duracion": i.get("duration"),
        "miniatura": i.get("thumbnail"),
        "alturas": alturas,
        "subtitulos": [s for s in subs if s in ("es", "en", "es-ES", "en-US", "fr", "pt", "de", "it")],
    }


def descargar(tid, p):
    import yt_dlp

    t = trabajos[tid]

    def gancho(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            t.update(estado="descargando", pct=round(d.get("downloaded_bytes", 0) * 100 / total, 1) if total else None,
                     velocidad=d.get("speed"), eta=d.get("eta"), archivo=os.path.basename(d.get("filename", "")))
        elif d["status"] == "finished":
            t.update(estado="procesando", pct=100)

    carpeta = p.get("carpeta") or DESTINO
    os.makedirs(carpeta, exist_ok=True)
    plantilla = "%(playlist_index)03d - %(title)s.%(ext)s" if p.get("lista") else "%(title)s.%(ext)s"
    o = {
        **BASE,
        "outtmpl": os.path.join(carpeta, plantilla),
        "windowsfilenames": True,
        "noplaylist": not p.get("lista"),
        "progress_hooks": [gancho],
        "noprogress": True,
        "postprocessors": [],
    }
    if p.get("modo") == "audio":
        o["format"] = "bestaudio/best"
        o["postprocessors"].append({"key": "FFmpegExtractAudio", "preferredcodec": p.get("formato_audio", "mp3"),
                                    "preferredquality": str(p.get("bitrate", "192"))})
    else:
        h = p.get("altura")
        tope = f"[height<={int(h)}]" if h else ""
        fmt = p.get("formato_video", "mp4")
        if fmt == "mp4":  # H.264 + AAC para que se vea en cualquier sitio
            o["format"] = f"bestvideo{tope}[vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo{tope}+bestaudio/best{tope}"
        else:
            o["format"] = f"bestvideo{tope}+bestaudio/best{tope}"
        o["merge_output_format"] = fmt
        if p.get("subtitulos"):
            o.update(writesubtitles=True, writeautomaticsub=True, subtitleslangs=[p["subtitulos"]])
            o["postprocessors"].append({"key": "FFmpegEmbedSubtitle"})
    recorte = p.get("inicio") or p.get("fin")
    if p.get("miniatura") and not (recorte and p.get("modo") != "audio"):  # con recorte, el vídeo pierde la portada
        o["writethumbnail"] = True
        o["postprocessors"].append({"key": "EmbedThumbnail"})
    if recorte:
        from yt_dlp.utils import download_range_func, parse_duration

        ini = parse_duration(p.get("inicio") or "0") or 0
        fin = parse_duration(p.get("fin") or "") or float("inf")
        o["download_ranges"] = download_range_func(None, [(ini, fin)])
        o["force_keyframes_at_cuts"] = True
    try:
        with yt_dlp.YoutubeDL(o) as ydl:
            ydl.download([p["enlace"]])
        t.update(estado="listo", pct=100, carpeta=carpeta)
    except Exception as e:
        t.update(estado="error", error=str(e).replace("ERROR: ", ""))


def carpetas_rapidas():
    # Rutas reales de Windows (pueden estar movidas a OneDrive u otra unidad)
    import winreg

    r = {}
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
    except OSError:
        return r
    for nombre, clave in (("Descargas", "{374DE290-123F-4565-9164-39C4925E467B}"), ("Escritorio", "Desktop"),
                          ("Vídeos", "My Video"), ("Música", "My Music")):
        try:
            ruta = os.path.expandvars(winreg.QueryValueEx(k, clave)[0])
            if os.path.isdir(ruta):
                r[nombre] = ruta
        except OSError:
            pass
    return r


def elegir_carpeta(actual):
    # Diálogo de carpetas de Windows colgado de una ventana "siempre encima": si no, se abre detrás del navegador
    ps = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$f=New-Object System.Windows.Forms.Form -Property @{TopMost=$true;ShowInTaskbar=$false;StartPosition='CenterScreen';"
        "Width=1;Height=1;FormBorderStyle='None';Opacity=0};$f.Show();$f.Activate();"
        "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description='¿Dónde guardo las descargas?';$d.ShowNewFolderButton=$true;$d.SelectedPath=$env:ZP_ACTUAL;"
        "if($d.ShowDialog($f) -eq 'OK'){$d.SelectedPath};$f.Close()"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, text=True, encoding="utf-8",
                           env={**os.environ, "ZP_ACTUAL": actual}, creationflags=subprocess.CREATE_NO_WINDOW)
        return os.path.normpath(r.stdout.strip()) if r.stdout.strip() else ""
    except Exception:
        return ""


def fijar_carpeta(ruta):
    ruta = os.path.expandvars(os.path.expanduser(str(ruta).strip().strip('"')))
    if not os.path.isabs(ruta):
        raise ValueError("Escribe la ruta completa, por ejemplo C:\\Users\\Elias\\Videos")
    try:
        os.makedirs(ruta, exist_ok=True)
    except OSError:
        raise ValueError("No puedo usar esa carpeta (no existe la unidad o no hay permiso)")
    c = leer_config()
    c["carpeta"] = os.path.normpath(ruta)
    guardar_config(c)
    return c


class Manejador(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def responder(self, datos, codigo=200, tipo="application/json"):
        cuerpo = datos if isinstance(datos, bytes) else json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        if self.path == "/":
            with open(os.path.join(AQUI, "web.html"), "rb") as f:
                return self.responder(f.read(), tipo="text/html")
        if self.path == "/api/config":
            return self.responder({**leer_config(), "rapidas": carpetas_rapidas()})
        if self.path.startswith("/api/trabajo/"):
            t = trabajos.get(self.path.rsplit("/", 1)[1])
            return self.responder(t or {"estado": "error", "error": "No existe"}, 200 if t else 404)
        self.responder({"error": "No encontrado"}, 404)

    def do_POST(self):
        # Solo peticiones de la propia página (evita que otra web abierta use el descargador)
        origen = self.headers.get("Origin", "")
        if origen and origen not in (f"http://localhost:{PUERTO}", f"http://127.0.0.1:{PUERTO}"):
            return self.responder({"error": "Origen no permitido"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            p = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/info":
                return self.responder(info(p["enlace"]))
            if self.path == "/api/descargar":
                tid = uuid.uuid4().hex[:8]
                trabajos[tid] = {"estado": "empezando", "pct": 0}
                threading.Thread(target=descargar, args=(tid, p), daemon=True).start()
                return self.responder({"id": tid})
            if self.path == "/api/carpeta":
                if p.get("carpeta"):  # escrita o elegida con un botón rápido
                    return self.responder(fijar_carpeta(p["carpeta"]))
                nueva = elegir_carpeta(leer_config().get("carpeta", DESTINO))
                return self.responder(fijar_carpeta(nueva) if nueva else {**leer_config(), "cancelado": True})
            if self.path == "/api/abrir":
                carpeta = p.get("carpeta") or leer_config().get("carpeta", DESTINO)
                os.makedirs(carpeta, exist_ok=True)
                os.startfile(carpeta)
                return self.responder({"ok": True})
        except Exception as e:
            return self.responder({"error": str(e).replace("ERROR: ", "")}, 400)
        self.responder({"error": "No encontrado"}, 404)


if __name__ == "__main__":
    asegurar_yt_dlp()
    servidor = ThreadingHTTPServer(("127.0.0.1", PUERTO), Manejador)
    print(f"Descargador abierto en http://localhost:{PUERTO}  (cierra esta ventana para pararlo)")
    if "--sin-navegador" not in sys.argv:
        webbrowser.open(f"http://localhost:{PUERTO}")
    servidor.serve_forever()
