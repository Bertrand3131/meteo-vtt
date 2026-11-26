@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Preparation de la nature du sol (a faire une seule fois, quelques minutes)
echo.
set PY=py
where py >nul 2>nul || set PY=python
%PY% preparer_sol.py
echo.
if errorlevel 1 (
  echo Il y a eu un probleme : fais une capture de cette fenetre.
) else (
  echo Termine. Le fichier sol.json est pret.
)
pause
