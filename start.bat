@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 논문 AI
if not exist .venv\Scripts\python.exe (
  echo 처음 실행 준비 중... (몇 분 걸릴 수 있어요)
  python -m venv .venv
  .venv\Scripts\python -m pip install -q -r requirements.txt
)
echo 논문 AI를 켭니다. 브라우저가 자동으로 열립니다.  이 창을 닫으면 앱이 꺼집니다.
echo 주소: http://127.0.0.1:8765
.venv\Scripts\python server.py
pause
