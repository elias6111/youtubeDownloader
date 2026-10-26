@echo off
chcp 65001 >nul
title Descargador de YouTube
python "%~dp0web.py"
pause
