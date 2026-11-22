# 🚁 FPV-Video-Sortierer

Ein Windows-Programm, das **FPV-Drohnenvideos automatisch analysiert und sortiert** – in *Behalten*, *Anschauen* und *Zum Löschen*.

Wer viel fliegt, hat schnell hunderte Clips auf der Platte: Fehlstarts, Crashs nach 5 Sekunden, verrauschte Abendflüge. Der Sortierer schaut sich jedes Video an und nimmt einem die Vorsortierung ab.

## Was es macht

- **Flugzeit erkennen:** Misst, wie lange am Stück tatsächlich geflogen wird (mind. 30 s, kurze ruhige Momente zählen mit).
- **Bildqualität prüfen:** Helligkeit, Rauschen und Schärfe – zu dunkle, verrauschte oder unscharfe Clips fliegen raus.
- **Bericht mit Kontaktbögen:** Pro Video 12 Vorschaubilder, eine Leiste für die Flugzeit und der Grund für die Einstufung.
- **Ordentlich einsortieren:** Nach Monat und Tag, mit Uhrzeit vor dem Dateinamen, z. B.
  `1_Behalten\2025-06\2025-06-14\14-32-05_DJI_0042.MP4`
- **Begleitdateien** (`.SRT`, `.LRF`, `.THM`, `.LRV`) wandern mit dem Video mit.

## Sicherheit

- Das Programm **löscht nie etwas** – es verschiebt nur.
- **Rückgängig** holt die letzte Einsortierung komplett zurück.
- Abbrechen jederzeit möglich, beim nächsten Start geht es dort weiter.

## Download & Start

1. Unter [**Releases**](../../releases/latest) die Datei `DrohnenSortierer.zip` herunterladen und entpacken.
2. Doppelklick auf **`Start.bat`**.
   Beim ersten Start werden Python und die benötigten Bausteine automatisch installiert (Internet nötig).
3. Falls Windows „Der Computer wurde durch Windows geschützt“ meldet: *Weitere Informationen → Trotzdem ausführen*.

Die ausführliche Anleitung steht in [`LIESMICH.txt`](LIESMICH.txt).

## Technik

Python · Tkinter (Oberfläche) · NumPy & Pillow (Bildanalyse) · FFmpeg über `imageio-ffmpeg`

Die Grenzwerte lassen sich in `einstellungen.json` anpassen (wird beim ersten Start angelegt).
