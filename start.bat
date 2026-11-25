@echo off
rem Paper AI launcher. First run: installs Python (if missing) and the needed packages.
rem Command lines stay ASCII only (cmd misreads multibyte text inside blocks under UTF-8).
cd /d "%~dp0"
title PaperAI

if exist ".venv\Scripts\python.exe" goto run

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python"
if defined PY goto make_venv

chcp 65001 >nul
echo.
echo  파이썬이 없어서 설치할게요. (처음 한 번만, 몇 분 걸려요)
echo.
chcp 949 >nul
winget install --id Python.Python.3.12 -e --scope user --accept-source-agreements --accept-package-agreements --silent
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%PY%" goto make_venv
chcp 65001 >nul
echo.
echo  [!] 파이썬을 자동으로 설치하지 못했어요.
echo      https://www.python.org/downloads/ 에서 설치한 뒤 이 파일을 다시 실행해 주세요.
echo      (설치 화면에서 "Add python.exe to PATH"에 꼭 체크)
pause
exit /b 1

:make_venv
chcp 65001 >nul
echo.
echo  처음 실행 준비 중이에요. 필요한 프로그램을 설치해요. (5~10분 걸릴 수 있어요)
echo.
%PY% -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 goto fail

:run
chcp 65001 >nul
echo.
echo  논문 AI를 켭니다. 브라우저가 자동으로 열려요.
echo  주소: http://127.0.0.1:8765
echo  ** 이 창을 닫으면 앱이 꺼져요 **
echo.
".venv\Scripts\python.exe" server.py
pause
exit /b 0

:fail
echo.
echo  [!] 설치 중에 문제가 생겼어요. 인터넷 연결을 확인하고, .venv 폴더를 지운 뒤 다시 실행해 주세요.
pause
exit /b 1
