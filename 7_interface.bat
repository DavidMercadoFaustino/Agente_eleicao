@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Abrindo a interface no navegador (feche esta janela para encerrar)...
".venv\Scripts\python.exe" -m streamlit run app.py
pause
