@echo off
rem Paper AI launcher. First run: installs Python (if missing) and the needed packages.
rem Command lines stay ASCII only (cmd misreads multibyte text inside blocks under UTF-8).
cd /d "%~dp0"
title PaperAI

if exist ".venv\Scripts\python.exe" goto claude

chcp 65001 >nul
echo.
echo  ============================================================
echo   논문 AI를 처음 설치해요. 아래 프로그램이 함께 설치돼요.
echo.
echo    1. 파이썬 (Python 3.12) - 앱을 돌리는 프로그램. 이미 있으면 건너뛰어요
echo    2. 앱에 필요한 파이썬 부품들 - 이 폴더 안(.venv)에만 설치돼요
echo    3. Claude Code (Anthropic 공식) - 내 Claude 구독으로 앱을 쓰기 위해 필요해요
echo.
echo   모두 무료이고, 관리자 권한 없이 내 계정에만 설치돼요. (10분 안팎)
echo  ============================================================
echo.
echo  설치하려면 아무 키나 누르세요. 그만두려면 이 창을 닫으세요.
pause >nul
chcp 949 >nul

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

:claude
rem Claude Code: lets the app use the user's own Claude subscription instead of a paid API key
if exist "%USERPROFILE%\.local\bin\claude.exe" goto run
if exist "%APPDATA%\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe" goto run
if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\claude.exe" goto run
where claude >nul 2>nul && goto run
if exist ".venv\claude-skip" goto run
chcp 65001 >nul
echo.
echo  Claude Code를 설치할게요. (내 Claude 구독으로 앱을 쓰기 위해 필요해요)
echo.
chcp 949 >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
if exist "%USERPROFILE%\.local\bin\claude.exe" goto claude_login
winget install --id Anthropic.ClaudeCode -e --accept-source-agreements --accept-package-agreements --silent
if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\claude.exe" goto claude_login
chcp 65001 >nul
echo.
echo  [!] Claude Code를 설치하지 못했어요. 앱은 API 키로도 쓸 수 있어요.
echo      나중에 다시 시도하려면 .venv 폴더 안의 claude-skip 파일을 지우고 start.bat을 실행하세요.
echo skip> ".venv\claude-skip"
timeout /t 5 >nul
goto run

:claude_login
chcp 65001 >nul
echo.
echo  설치 완료! 새로 뜨는 창에서 Claude 계정으로 로그인해 주세요.
echo  (로그인 방식을 고르면 브라우저가 열려요. 로그인 후 '허용'. 끝나면 그 창은 닫아도 돼요)
echo.
set "CLAUDE_EXE=%USERPROFILE%\.local\bin\claude.exe"
if not exist "%CLAUDE_EXE%" set "CLAUDE_EXE=%LOCALAPPDATA%\Microsoft\WinGet\Links\claude.exe"
start "Claude Login" cmd /k ""%CLAUDE_EXE%""
timeout /t 5 >nul

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
