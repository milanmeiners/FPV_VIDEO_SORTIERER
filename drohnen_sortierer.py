# -*- coding: utf-8 -*-
"""
Drohnen-Video-Sortierer
=======================
Analysiert FPV-Drohnenvideos und sortiert sie in
  1_Behalten  /  2_Anschauen  /  3_Zum_Loeschen

Regeln:
  * Qualitaet: zu dunkel, zu verrauscht oder zu unscharf -> Loeschen
  * Flugzeit: mindestens 30 s Flug AM STUECK, sonst -> Loeschen
  * Grenzfaelle -> Anschauen
Behalten/Anschauen werden nach Monat/Tag sortiert, Uhrzeit vor dem Dateinamen.
Es wird nie etwas geloescht - nur verschoben (und das ist rueckgaengig machbar).
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import queue
import html
import io
import hashlib
from datetime import datetime, timezone

import numpy as np
from PIL import Image, ImageDraw, ImageFont

APP_NAME = "Drohnen-Video-Sortierer"
VERSION = "1.0"

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".mts", ".m2ts", ".3gp", ".wmv"}
# Begleitdateien (DJI .SRT/.LRF, GoPro .THM/.LRV), die mit dem Video mitwandern
COMPANION_EXT = {".srt", ".lrf", ".thm", ".lrv", ".ass"}

OUT_DIR = "_FPV_Sortierung"
CAT_DIRS = {"behalten": "1_Behalten", "anschauen": "2_Anschauen", "loeschen": "3_Zum_Loeschen"}
CAT_LABEL = {"behalten": "Behalten", "anschauen": "Anschauen", "loeschen": "Zum Löschen"}

# Analyse-Raster (fest, damit Messwerte vergleichbar bleiben)
SAMPLE_FPS = 2
W, H = 480, 270
BLOCK = 8
ANALYSIS_VERSION = 1

DEFAULT_SETTINGS = {
    "min_flug_sekunden": 30,        # Mindestflug am Stück
    "flug_grenzbereich_sekunden": 4,  # knapp darunter -> Anschauen statt Löschen
    "pause_toleranz_sekunden": 3,   # kurze ruhige Momente unterbrechen den Flug nicht
    "bewegung_schwelle": 4.0,       # ab hier gilt ein Moment als "fliegt"
    "helligkeit_schlecht": 40,      # 0-255; darunter -> Löschen
    "helligkeit_grenz": 60,         # darunter -> Anschauen
    "rauschen_schlecht": 6.0,       # darüber -> Löschen
    "rauschen_grenz": 4.0,          # darüber -> Anschauen
    "schaerfe_schlecht": 20.0,      # darunter -> Löschen
    "schaerfe_grenz": 50.0,         # darunter -> Anschauen
    "testlauf_anzahl": 20,
    "letzter_ordner": "",
}

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ----------------------------------------------------------------------------
# Hilfsfunktionen
# ----------------------------------------------------------------------------

def app_dir():
    return os.path.dirname(os.path.abspath(sys.argv[0] if sys.argv and sys.argv[0] else __file__))


def load_settings():
    path = os.path.join(app_dir(), "einstellungen.json")
    s = dict(DEFAULT_SETTINGS)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                s.update({k: v for k, v in json.load(f).items() if k in DEFAULT_SETTINGS})
        except Exception:
            pass
    else:
        save_settings(s)
    return s


def save_settings(s):
    path = os.path.join(app_dir(), "einstellungen.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def keep_awake(on):
    """Verhindert unter Windows den Ruhezustand, solange die Analyse läuft."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:
        pass


def fmt_dur(sec):
    sec = int(round(sec or 0))
    return f"{sec // 60}:{sec % 60:02d}"


def file_key(path):
    """Erkennt ein Video auch nach dem Einsortieren wieder (Name ohne Uhrzeit-Präfix + Größe)."""
    st = os.stat(path)
    name = re.sub(r"^\d{2}-\d{2}-\d{2}_", "", os.path.basename(path)).lower()
    return f"{name}|{st.st_size}"


# ----------------------------------------------------------------------------
# Videos finden, Infos, Datum
# ----------------------------------------------------------------------------

def find_videos(root):
    vids = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != OUT_DIR and not d.startswith(".")]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in VIDEO_EXT and not fn.startswith("._"):
                vids.append(os.path.join(dirpath, fn))
    vids.sort(key=lambda p: p.lower())
    return vids


def probe(path):
    """Dauer, Auflösung, fps und creation_time per ffmpeg -i."""
    p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path],
                       capture_output=True, creationflags=NO_WINDOW)
    txt = p.stderr.decode("utf-8", errors="replace")
    info = {"dauer": 0.0, "breite": 0, "hoehe": 0, "fps": 0.0, "creation_time": None}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", txt)
    if m:
        info["dauer"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", txt)
    if m:
        info["breite"], info["hoehe"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", txt)
    if m:
        info["fps"] = float(m.group(1))
    m = re.search(r"creation_time\s*:\s*(\S+)", txt)
    if m:
        info["creation_time"] = m.group(1)
    return info


_NAME_DATE = re.compile(r"(20[1-3]\d)[-_]?([01]\d)[-_]?([0-3]\d)[-_ T]?([0-2]\d)[-_.]?([0-5]\d)[-_.]?([0-5]\d)")


def recording_date(path, info):
    """Aufnahmezeit: 1. aus dem Dateinamen, 2. aus den Videodaten, 3. Änderungsdatum."""
    m = _NAME_DATE.search(os.path.basename(path))
    if m:
        try:
            return datetime(*map(int, m.groups())), "Dateiname"
        except ValueError:
            pass
    ct = info.get("creation_time")
    if ct:
        try:
            d = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            if d.tzinfo is not None:
                d = d.astimezone().replace(tzinfo=None)  # in lokale Zeit
            if d.year >= 2010:  # 1970/1904 = Kamera ohne Uhr
                return d, "Videodaten"
        except ValueError:
            pass
    return datetime.fromtimestamp(os.path.getmtime(path)), "Dateidatum"


# ----------------------------------------------------------------------------
# Analyse
# ----------------------------------------------------------------------------

def _frame_metrics(f):
    """f: uint8 HxW Graubild -> (Helligkeit, Schärfe, Rauschen, kleines Bild)."""
    g = f.astype(np.float32)
    bright = float(g.mean())
    lap = g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:] - 4 * g[1:-1, 1:-1]
    sharp = float(lap.var())
    # Rauschschätzung nach Immerkaer, robust über Median
    c = (g[:-2, :-2] - 2 * g[:-2, 1:-1] + g[:-2, 2:]
         - 2 * g[1:-1, :-2] + 4 * g[1:-1, 1:-1] - 2 * g[1:-1, 2:]
         + g[2:, :-2] - 2 * g[2:, 1:-1] + g[2:, 2:])
    noise = float(np.median(np.abs(c)) / 4.047)
    hh, ww = (H // BLOCK) * BLOCK, (W // BLOCK) * BLOCK
    small = g[:hh, :ww].reshape(hh // BLOCK, BLOCK, ww // BLOCK, BLOCK).mean(axis=(1, 3))
    return bright, sharp, noise, small


def analyze_frames(path, stop_event=None, progress=None, duration=0.0):
    vf = f"fps={SAMPLE_FPS},scale={W}:{H}:flags=area,format=gray"
    base = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin"]
    tail = ["-i", path, "-an", "-sn", "-dn", "-vf", vf, "-f", "rawvideo", "-"]
    for attempt in (["-hwaccel", "auto"], []):
        res = _run_analysis(base + attempt + tail, stop_event, progress, duration)
        if res is None:
            return None  # abgebrochen
        if res["helligkeit"]:
            return res
    return res


def _run_analysis(cmd, stop_event, progress, duration):
    out = {"helligkeit": [], "schaerfe": [], "rauschen": [], "bewegung": []}
    fsize = W * H
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=NO_WINDOW)
    prev = None
    n = 0
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                proc.kill()
                return None
            buf = proc.stdout.read(fsize)
            if len(buf) < fsize:
                break
            f = np.frombuffer(buf, dtype=np.uint8).reshape(H, W)
            b, s, nz, small = _frame_metrics(f)
            mv = 0.0 if prev is None else float(np.abs(small - prev).mean())
            prev = small
            out["helligkeit"].append(round(b, 1))
            out["schaerfe"].append(round(s, 1))
            out["rauschen"].append(round(nz, 2))
            out["bewegung"].append(round(mv, 2))
            n += 1
            if progress and duration > 0 and n % 10 == 0:
                progress(min(1.0, n / SAMPLE_FPS / duration))
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()
    if len(out["bewegung"]) > 1:
        out["bewegung"][0] = out["bewegung"][1]
    return out


def grab_thumbs(path, duration, count=12, width=320):
    """count Farbbilder gleichmäßig über das Video verteilt."""
    thumbs = []
    if duration <= 0:
        return thumbs
    for i in range(count):
        t = duration * (i + 0.5) / count
        cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin",
               "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
               "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "-"]
        p = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
        try:
            img = Image.open(io.BytesIO(p.stdout)).convert("RGB")
            thumbs.append((t, img))
        except Exception:
            thumbs.append((t, None))
    return thumbs


def _font(size):
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def make_contact_sheet(path, info, thumbs, motion, motion_thr, out_path):
    cols, tw = 4, 320
    th = 180
    for _, im in thumbs:
        if im is not None:
            th = int(im.height * tw / im.width)
            break
    rows = max(1, (len(thumbs) + cols - 1) // cols)
    pad, head, bar = 6, 40, 34
    Wc = cols * tw + (cols + 1) * pad
    Hc = head + rows * (th + pad) + pad + bar
    sheet = Image.new("RGB", (Wc, Hc), (24, 24, 28))
    d = ImageDraw.Draw(sheet)
    f_big, f_small = _font(18), _font(13)
    title = f"{os.path.basename(path)}   ·   {fmt_dur(info['dauer'])} min   ·   {info['breite']}x{info['hoehe']}"
    d.text((pad + 2, 10), title, fill=(235, 235, 235), font=f_big)
    for i, (t, im) in enumerate(thumbs):
        x = pad + (i % cols) * (tw + pad)
        y = head + (i // cols) * (th + pad)
        if im is not None:
            sheet.paste(im.resize((tw, th)), (x, y))
        else:
            d.rectangle([x, y, x + tw, y + th], fill=(50, 50, 50))
        lbl = fmt_dur(t)
        d.rectangle([x, y + th - 20, x + 46, y + th], fill=(0, 0, 0))
        d.text((x + 4, y + th - 18), lbl, fill=(255, 255, 255), font=f_small)
    # Flug-Leiste: grün = Bewegung (fliegt), grau = Stillstand
    by = Hc - bar + 6
    d.text((pad + 2, by), "Flug:", fill=(200, 200, 200), font=f_small)
    x0, x1 = pad + 46, Wc - pad
    if motion:
        n = len(motion)
        for i, mv in enumerate(motion):
            a = x0 + (x1 - x0) * i / n
            b = x0 + (x1 - x0) * (i + 1) / n
            col = (60, 200, 90) if mv >= motion_thr else (80, 80, 85)
            d.rectangle([int(a), by + 2, max(int(a), int(b) - 0), by + 16], fill=col)
    sheet.save(out_path, "JPEG", quality=82)


# ----------------------------------------------------------------------------
# Bewertung
# ----------------------------------------------------------------------------

def longest_flight(motion, thr, gap_tol_s):
    """Längster Flug am Stück in Sekunden (kurze Pausen <= gap_tol_s zählen mit)."""
    gap_tol = int(round(gap_tol_s * SAMPLE_FPS))
    best = (0, -1, -1)
    start = last = None
    for i, mv in enumerate(motion):
        if mv >= thr:
            if start is None or (i - last - 1) > gap_tol:
                start = i
            last = i
            length = last - start + 1
            if length > best[0]:
                best = (length, start, last)
    return best[0] / SAMPLE_FPS, best[1], best[2]


def classify(a, s):
    m = a["messung"]
    motion = m["bewegung"]
    flug, i0, i1 = longest_flight(motion, s["bewegung_schwelle"], s["pause_toleranz_sekunden"])
    sel = slice(i0, i1 + 1) if i0 >= 0 else slice(None)
    hell = float(np.median(m["helligkeit"][sel])) if m["helligkeit"] else 0.0
    rau = float(np.median(m["rauschen"][sel])) if m["rauschen"] else 99.0
    sch = float(np.median(m["schaerfe"][sel])) if m["schaerfe"] else 0.0

    loesch, grenz = [], []
    minf = s["min_flug_sekunden"]
    if flug < minf - s["flug_grenzbereich_sekunden"]:
        loesch.append(f"nur {flug:.0f} s Flug am Stück")
    elif flug < minf:
        grenz.append(f"knapp: {flug:.0f} s Flug am Stück")
    if hell < s["helligkeit_schlecht"]:
        loesch.append("zu dunkel")
    elif hell < s["helligkeit_grenz"]:
        grenz.append("eher dunkel")
    if rau > s["rauschen_schlecht"]:
        loesch.append("stark verrauscht")
    elif rau > s["rauschen_grenz"]:
        grenz.append("etwas verrauscht")
    if sch < s["schaerfe_schlecht"]:
        loesch.append("unscharf")
    elif sch < s["schaerfe_grenz"]:
        grenz.append("etwas unscharf")

    if loesch:
        cat = "loeschen"
    elif grenz:
        cat = "anschauen"
    else:
        cat = "behalten"

    def clamp(x):
        return max(0.0, min(1.0, x))
    q = (clamp((hell - s["helligkeit_schlecht"]) / 50.0)
         + clamp((s["rauschen_schlecht"] - rau) / max(0.1, s["rauschen_schlecht"] - 1.0))
         + clamp(np.log(max(sch, 1) / s["schaerfe_schlecht"]) / np.log(10))
         + clamp(flug / 120.0))
    score = int(round(25 * q))
    return {"kategorie": cat, "gruende": loesch + grenz, "flug": flug,
            "helligkeit": hell, "rauschen": rau, "schaerfe": sch, "punkte": score}


# ----------------------------------------------------------------------------
# Cache
# ----------------------------------------------------------------------------

class Store:
    def __init__(self, root):
        self.root = root
        self.dir = os.path.join(root, OUT_DIR)
        self.sheets = os.path.join(self.dir, "kontaktboegen")
        os.makedirs(self.sheets, exist_ok=True)
        self.path = os.path.join(self.dir, "analyse.json")
        self.data = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, self.path)


def analyze_one(path, store, settings, stop_event=None, progress=None):
    key = file_key(path)
    old = store.data.get(key)
    if old and old.get("version") == ANALYSIS_VERSION and os.path.exists(
            os.path.join(store.sheets, old.get("bogen", ""))):
        return old, True
    info = probe(path)
    m = analyze_frames(path, stop_event, progress, info["dauer"])
    if m is None:
        return None, False
    if info["dauer"] <= 0 and m["helligkeit"]:
        info["dauer"] = len(m["helligkeit"]) / SAMPLE_FPS
    d, quelle = recording_date(path, info)
    thumbs = grab_thumbs(path, info["dauer"])
    sheet_name = re.sub(r"[^\w\-]+", "_", key.split("|")[0]) + f"_{hashlib.md5(key.encode('utf-8')).hexdigest()[:6]}.jpg"
    make_contact_sheet(path, info, thumbs, m["bewegung"], settings["bewegung_schwelle"],
                       os.path.join(store.sheets, sheet_name))
    entry = {"version": ANALYSIS_VERSION, "info": info, "messung": m,
             "datum": d.isoformat(timespec="seconds"), "datum_quelle": quelle,
             "bogen": sheet_name, "pfad": path}
    store.data[key] = entry
    store.save()
    return entry, False


# ----------------------------------------------------------------------------
# Bericht
# ----------------------------------------------------------------------------

def current_results(store, settings, videos):
    """Bewertet alle analysierten Videos neu (Einstellungen können sich geändert haben)."""
    rows = []
    for p in videos:
        try:
            k = file_key(p)
        except OSError:
            continue
        a = store.data.get(k)
        if not a:
            continue
        a["pfad"] = p
        r = classify(a, settings)
        r.update({"pfad": p, "name": os.path.basename(p), "dauer": a["info"]["dauer"],
                  "datum": a["datum"], "datum_quelle": a.get("datum_quelle", ""),
                  "bogen": a["bogen"]})
        rows.append(r)
    return rows


def write_report(store, rows, settings):
    # CSV (für Excel: Semikolon, Dezimalkomma)
    csv_path = os.path.join(store.dir, "bericht.csv")

    def n(x, d=1):
        return f"{x:.{d}f}".replace(".", ",")
    with open(csv_path, "w", encoding="utf-8-sig") as f:
        f.write("Datei;Kategorie;Gründe;Punkte;Flug am Stück (s);Dauer (s);Helligkeit;Rauschen;Schärfe;Aufnahmedatum;Datum aus;Pfad\n")
        for r in sorted(rows, key=lambda r: r["datum"]):
            f.write(";".join([r["name"], CAT_LABEL[r["kategorie"]], ", ".join(r["gruende"]),
                              str(r["punkte"]), n(r["flug"], 0), n(r["dauer"], 0),
                              n(r["helligkeit"]), n(r["rauschen"], 2), n(r["schaerfe"]),
                              r["datum"].replace("T", " "), r["datum_quelle"], r["pfad"]]) + "\n")

    # HTML
    counts = {c: sum(1 for r in rows if r["kategorie"] == c) for c in CAT_LABEL}
    parts = [f"""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<title>Drohnenvideos – Bericht</title><style>
body{{font-family:Segoe UI,Arial,sans-serif;background:#16161a;color:#e8e8ea;margin:0;padding:24px}}
h1{{margin:0 0 4px}} .sub{{color:#9a9aa3;margin-bottom:20px}}
nav a{{color:#fff;background:#2a2a31;padding:8px 14px;border-radius:8px;margin-right:8px;text-decoration:none;display:inline-block}}
h2{{margin-top:36px;border-bottom:1px solid #333;padding-bottom:6px}}
.card{{background:#202026;border-radius:10px;padding:12px;margin:14px 0}}
.card img{{width:100%;max-width:1300px;border-radius:6px;display:block}}
.meta{{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:8px;font-size:14px;color:#c9c9cf}}
.tag{{padding:2px 8px;border-radius:6px;font-weight:600}}
.behalten .tag{{background:#1f5f35}} .anschauen .tag{{background:#7a5a10}} .loeschen .tag{{background:#7a1f1f}}
.why{{color:#ffcf70}}
</style></head><body>
<h1>Drohnenvideos – Bericht</h1>
<div class="sub">{len(rows)} Videos analysiert · erstellt {datetime.now():%d.%m.%Y %H:%M} ·
Regeln: mind. {settings['min_flug_sekunden']} s Flug am Stück, ausreichende Bildqualität</div>
<nav>""" ]
    for c in ("loeschen", "anschauen", "behalten"):
        parts.append(f'<a href="#{c}">{CAT_LABEL[c]} ({counts[c]})</a>')
    parts.append("</nav>")
    for c in ("loeschen", "anschauen", "behalten"):
        parts.append(f'<h2 id="{c}">{CAT_LABEL[c]} ({counts[c]})</h2>')
        sel = [r for r in rows if r["kategorie"] == c]
        sel.sort(key=(lambda r: r["datum"]) if c != "behalten" else (lambda r: -r["punkte"]))
        for r in sel:
            why = ", ".join(r["gruende"]) or "alles in Ordnung"
            parts.append(f"""<div class="card {c}">
<img loading="lazy" src="kontaktboegen/{html.escape(r['bogen'])}">
<div class="meta"><span class="tag">{CAT_LABEL[c]}</span><b>{html.escape(r['name'])}</b>
<span class="why">{html.escape(why)}</span>
<span>Punkte: {r['punkte']}</span><span>Flug am Stück: {r['flug']:.0f} s</span>
<span>Dauer: {fmt_dur(r['dauer'])}</span><span>Helligkeit: {r['helligkeit']:.0f}</span>
<span>Rauschen: {r['rauschen']:.1f}</span><span>Schärfe: {r['schaerfe']:.0f}</span>
<span>Aufgenommen: {r['datum'].replace('T', ' ')} ({r['datum_quelle']})</span>
<span style="color:#777">{html.escape(r['pfad'])}</span></div></div>""")
    parts.append("</body></html>")
    html_path = os.path.join(store.dir, "bericht.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    return html_path, counts


# ----------------------------------------------------------------------------
# Sortieren / Rückgängig
# ----------------------------------------------------------------------------

def _unique(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{base}_{i}{ext}"):
        i += 1
    return f"{base}_{i}{ext}"


def _companions(path):
    d, fn = os.path.split(path)
    stem = os.path.splitext(fn)[0].lower()
    out = []
    try:
        for other in os.listdir(d):
            st, ext = os.path.splitext(other)
            if st.lower() == stem and ext.lower() in COMPANION_EXT:
                out.append(os.path.join(d, other))
    except OSError:
        pass
    return out


_TIME_PREFIX = re.compile(r"^\d{2}-\d{2}-\d{2}_")


def target_path(root, r):
    cat = r["kategorie"]
    name = r["name"]
    if cat == "loeschen":
        return os.path.join(root, CAT_DIRS[cat], _TIME_PREFIX.sub("", name))
    d = datetime.fromisoformat(r["datum"])
    clean = _TIME_PREFIX.sub("", name)
    return os.path.join(root, CAT_DIRS[cat], f"{d:%Y-%m}", f"{d:%Y-%m-%d}",
                        f"{d:%H-%M-%S}_{clean}")


def sort_videos(root, store, rows, log_cb=None):
    log_path = os.path.join(store.dir, "verschiebe_protokoll.json")
    moves = []
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8") as f:
            moves = json.load(f)
    batch = {"zeit": datetime.now().isoformat(timespec="seconds"), "dateien": []}
    done = 0
    for r in rows:
        src = r["pfad"]
        dst = target_path(root, r)
        if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
            continue
        dst = _unique(dst)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        comps = _companions(src)
        os.replace(src, dst)
        batch["dateien"].append([src, dst])
        dst_stem = os.path.splitext(dst)[0]
        for c in comps:
            cdst = _unique(dst_stem + os.path.splitext(c)[1])
            os.replace(c, cdst)
            batch["dateien"].append([c, cdst])
        done += 1
        if log_cb:
            log_cb(f"{CAT_LABEL[r['kategorie']]}: {os.path.relpath(dst, root)}")
    if batch["dateien"]:
        moves.append(batch)
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(moves, f, ensure_ascii=False, indent=1)
    _remove_empty_dirs(root)
    return done


def undo_last(root, store, log_cb=None):
    log_path = os.path.join(store.dir, "verschiebe_protokoll.json")
    if not os.path.exists(log_path):
        return 0
    with open(log_path, "r", encoding="utf-8") as f:
        moves = json.load(f)
    if not moves:
        return 0
    batch = moves.pop()
    n = 0
    for src, dst in reversed(batch["dateien"]):
        if os.path.exists(dst) and not os.path.exists(src):
            os.makedirs(os.path.dirname(src), exist_ok=True)
            os.replace(dst, src)
            n += 1
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(moves, f, ensure_ascii=False, indent=1)
    _remove_empty_dirs(root)
    if log_cb:
        log_cb(f"{n} Dateien zurückverschoben.")
    return n


def _remove_empty_dirs(root):
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if dirpath == root or OUT_DIR in dirpath.split(os.sep):
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass


def pick_test_set(videos, n):
    if n >= len(videos):
        return list(videos)
    step = len(videos) / n
    return [videos[int(i * step)] for i in range(n)]


# ----------------------------------------------------------------------------
# Kommandozeile (zum Testen ohne Oberfläche)
# ----------------------------------------------------------------------------

def run_cli(root, test=None):
    s = load_settings()
    store = Store(root)
    vids = find_videos(root)
    if test:
        vids = pick_test_set(vids, test)
    for i, p in enumerate(vids, 1):
        t = time.time()
        _, cached = analyze_one(p, store, s)
        print(f"[{i}/{len(vids)}] {os.path.basename(p)} {'(schon analysiert)' if cached else f'{time.time() - t:.1f}s'}")
    rows = current_results(store, s, vids)
    path, counts = write_report(store, rows, s)
    print(counts, path)
    return store, rows


# ----------------------------------------------------------------------------
# Oberfläche
# ----------------------------------------------------------------------------

def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    settings = load_settings()
    q = queue.Queue()
    state = {"stop": threading.Event(), "busy": False, "rows": None, "store": None}

    root_win = tk.Tk()
    root_win.title(f"{APP_NAME} {VERSION}")
    root_win.geometry("760x560")
    root_win.minsize(640, 480)
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass

    frm = ttk.Frame(root_win, padding=14)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text="Ordner mit deinen Drohnenvideos:", font=("Segoe UI", 10, "bold")).pack(anchor="w")
    row = ttk.Frame(frm)
    row.pack(fill="x", pady=(4, 10))
    folder = tk.StringVar(value=settings.get("letzter_ordner", ""))
    ttk.Entry(row, textvariable=folder).pack(side="left", fill="x", expand=True)

    def browse():
        d = filedialog.askdirectory(title="Ordner mit Drohnenvideos wählen")
        if d:
            folder.set(os.path.normpath(d))
    ttk.Button(row, text="Durchsuchen …", command=browse).pack(side="left", padx=(6, 0))

    opt = ttk.Frame(frm)
    opt.pack(fill="x")
    test_var = tk.BooleanVar(value=True)
    test_n = tk.IntVar(value=int(settings["testlauf_anzahl"]))
    ttk.Checkbutton(opt, text="Testlauf – nur", variable=test_var).pack(side="left")
    ttk.Spinbox(opt, from_=5, to=200, width=5, textvariable=test_n).pack(side="left", padx=4)
    ttk.Label(opt, text="Videos analysieren").pack(side="left")
    ttk.Label(opt, text=f"   Mindestflug am Stück: {settings['min_flug_sekunden']} s",
              foreground="#555").pack(side="left")

    btns = ttk.Frame(frm)
    btns.pack(fill="x", pady=12)
    b_start = ttk.Button(btns, text="1. Analyse starten")
    b_stop = ttk.Button(btns, text="Abbrechen", state="disabled")
    b_report = ttk.Button(btns, text="2. Bericht ansehen", state="disabled")
    b_sort = ttk.Button(btns, text="3. Videos einsortieren", state="disabled")
    b_undo = ttk.Button(btns, text="Rückgängig")
    for b in (b_start, b_stop, b_report, b_sort, b_undo):
        b.pack(side="left", padx=(0, 6))

    pb_all = ttk.Progressbar(frm, maximum=1000)
    pb_all.pack(fill="x")
    pb_one = ttk.Progressbar(frm, maximum=1000)
    pb_one.pack(fill="x", pady=(4, 0))
    status = tk.StringVar(value="Bereit.")
    ttk.Label(frm, textvariable=status).pack(anchor="w", pady=(6, 4))

    logbox = tk.Text(frm, height=14, wrap="word", font=("Consolas", 9))
    logbox.pack(fill="both", expand=True)

    def log(msg):
        q.put(("log", msg))

    def set_busy(b):
        state["busy"] = b
        b_start.config(state="disabled" if b else "normal")
        b_stop.config(state="normal" if b else "disabled")
        b_sort.config(state="disabled" if b or not state["rows"] else "normal")
        b_report.config(state="disabled" if b or not state["rows"] else "normal")
        b_undo.config(state="disabled" if b else "normal")

    def worker(rootdir, test):
        keep_awake(True)
        try:
            s = load_settings()
            store = Store(rootdir)
            state["store"] = store
            vids_all = find_videos(rootdir)
            if not vids_all:
                log("Keine Videos gefunden.")
                return
            vids = pick_test_set(vids_all, test) if test else vids_all
            total_bytes = sum(os.path.getsize(p) for p in vids) or 1
            done_bytes = 0
            t0 = time.time()
            log(f"{len(vids)} von {len(vids_all)} Videos werden analysiert "
                f"({total_bytes / 1e9:.1f} GB).")
            for i, p in enumerate(vids, 1):
                if state["stop"].is_set():
                    break
                size = os.path.getsize(p)
                q.put(("status", f"Video {i}/{len(vids)}: {os.path.basename(p)}"))

                def prog(fr, _db=done_bytes, _sz=size):
                    q.put(("one", fr))
                    q.put(("all", (_db + _sz * fr) / total_bytes))
                try:
                    entry, cached = analyze_one(p, store, s, state["stop"], prog)
                except Exception as e:
                    log(f"FEHLER bei {os.path.basename(p)}: {e}")
                    entry, cached = None, False
                if entry is None and state["stop"].is_set():
                    break
                done_bytes += size
                q.put(("all", done_bytes / total_bytes))
                if entry:
                    r = classify(entry, s)
                    log(f"{'↺ ' if cached else ''}{os.path.basename(p)}: {CAT_LABEL[r['kategorie']]}"
                        f" – Flug {r['flug']:.0f} s" + (f" ({', '.join(r['gruende'])})" if r['gruende'] else ""))
                el = time.time() - t0
                if done_bytes and not cached:
                    rest = el / done_bytes * (total_bytes - done_bytes)
                    q.put(("eta", rest))
            rows = current_results(store, s, vids_all)
            state["rows"] = rows
            path, counts = write_report(store, rows, s)
            log("")
            log(f"Fertig. Behalten: {counts['behalten']}  ·  Anschauen: {counts['anschauen']}"
                f"  ·  Zum Löschen: {counts['loeschen']}")
            log("Als Nächstes: '2. Bericht ansehen' und prüfen, ob die Einschätzungen passen.")
            q.put(("status", "Analyse abgeschlossen." if not state["stop"].is_set()
                   else "Abgebrochen – beim nächsten Start geht es dort weiter."))
        except Exception as e:
            log(f"FEHLER: {e}")
        finally:
            keep_awake(False)
            q.put(("done", None))

    def start():
        d = folder.get().strip()
        if not d or not os.path.isdir(d):
            messagebox.showwarning(APP_NAME, "Bitte zuerst einen gültigen Ordner wählen.")
            return
        s = load_settings()
        s["letzter_ordner"] = d
        s["testlauf_anzahl"] = int(test_n.get())
        save_settings_with_folder(s)
        state["stop"].clear()
        set_busy(True)
        logbox.delete("1.0", "end")
        threading.Thread(target=worker, args=(d, int(test_n.get()) if test_var.get() else None),
                         daemon=True).start()

    def save_settings_with_folder(s):
        path = os.path.join(app_dir(), "einstellungen.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def stop():
        state["stop"].set()
        status.set("Wird abgebrochen …")

    def open_report():
        if state["store"]:
            os.startfile(os.path.join(state["store"].dir, "bericht.html"))

    def do_sort():
        rows = state["rows"]
        if not rows:
            return
        counts = {c: sum(1 for r in rows if r["kategorie"] == c) for c in CAT_LABEL}
        if not messagebox.askyesno(APP_NAME,
                                   f"{len(rows)} analysierte Videos jetzt einsortieren?\n\n"
                                   f"Behalten: {counts['behalten']}\nAnschauen: {counts['anschauen']}\n"
                                   f"Zum Löschen: {counts['loeschen']}\n\n"
                                   "Es wird nichts gelöscht, nur verschoben. "
                                   "Mit 'Rückgängig' kannst du alles zurückholen."):
            return
        try:
            n = sort_videos(folder.get(), state["store"], rows, log)
            log(f"{n} Videos einsortiert.")
            log(f"Prüfe den Ordner '{CAT_DIRS['loeschen']}' und lösche ihn danach selbst im Explorer.")
            state["rows"] = None
            set_busy(False)
            os.startfile(folder.get())
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Fehler beim Verschieben:\n{e}")

    def do_undo():
        d = folder.get().strip()
        if not d or not os.path.isdir(d):
            return
        if messagebox.askyesno(APP_NAME, "Die letzte Einsortierung rückgängig machen?"):
            n = undo_last(d, Store(d), log)
            if n == 0:
                log("Nichts zum Rückgängigmachen gefunden.")

    b_start.config(command=start)
    b_stop.config(command=stop)
    b_report.config(command=open_report)
    b_sort.config(command=do_sort)
    b_undo.config(command=do_undo)

    def poll():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    logbox.insert("end", val + "\n")
                    logbox.see("end")
                elif kind == "status":
                    status.set(val)
                elif kind == "one":
                    pb_one["value"] = val * 1000
                elif kind == "all":
                    pb_all["value"] = val * 1000
                elif kind == "eta":
                    m = int(val // 60)
                    root_win.title(f"{APP_NAME} – noch ca. {m // 60} h {m % 60} min"
                                   if m >= 60 else f"{APP_NAME} – noch ca. {m} min")
                elif kind == "done":
                    root_win.title(f"{APP_NAME} {VERSION}")
                    pb_one["value"] = 0
                    set_busy(False)
        except queue.Empty:
            pass
        root_win.after(150, poll)

    log("So geht's:\n"
        "  1. Ordner wählen und 'Analyse starten' (erst als Testlauf).\n"
        "  2. Im Bericht prüfen, ob die Einschätzungen passen.\n"
        "  3. 'Videos einsortieren' – verschiebt in Behalten / Anschauen / Zum Löschen.\n"
        "Es wird nie etwas gelöscht. Eine abgebrochene Analyse macht beim nächsten Mal weiter.\n")
    poll()
    root_win.mainloop()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--cli":
        run_cli(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else None)
    else:
        run_gui()
