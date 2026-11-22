@echo off
title Drohnen-Video-Sortierer
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python ist noch nicht installiert und wird jetzt installiert ...
  echo.
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  if errorlevel 1 (
    echo.
    echo Die automatische Installation hat nicht geklappt.
    echo Bitte Python von https://www.python.org/downloads/ installieren
    echo und dabei "Add python.exe to PATH" anhaken. Danach diese Datei erneut starten.
    pause
    exit /b
  )
  echo.
  echo Python ist installiert. Bitte diese Datei jetzt NOCH EINMAL starten.
  pause
  exit /b
)

if not exist ".bereit" (
  echo Erster Start: benoetigte Bausteine werden installiert - einmalig, ca. 1-2 Minuten ...
  echo.
  py -m pip install --upgrade --user --disable-pip-version-check numpy pillow imageio-ffmpeg
  if errorlevel 1 (
    echo.
    echo Installation fehlgeschlagen. Bitte Internetverbindung pruefen und erneut starten.
    pause
    exit /b
  )
  py -c "import imageio_ffmpeg; imageio_ffmpeg.get_ffmpeg_exe()" && echo ok> .bereit
)

echo Programm laeuft. Dieses Fenster bitte offen lassen.
py drohnen_sortierer.py
if errorlevel 1 pause
