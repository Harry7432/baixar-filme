@echo off
title Central de Downloads - YouTube e Filmes
cd /d "%~dp0"
echo Iniciando servidor em http://localhost:8000 ...
start "" http://localhost:8000
.\.venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000
pause

