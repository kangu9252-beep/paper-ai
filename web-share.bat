@echo off
rem Paper AI web share: start the app if needed, then open the fixed ngrok address.
rem (Command lines are ASCII only: cmd misreads multibyte text in blocks under UTF-8.)
cd /d "%~dp0"
title PaperAI-Web

curl -s -o nul http://127.0.0.1:8765/api/auth/info
if not errorlevel 1 goto app_ready

start "PaperAI" /min "%~dp0start.bat"
set /a tries=0
:wait_app
timeout /t 2 /nobreak >nul
curl -s -o nul http://127.0.0.1:8765/api/auth/info
if not errorlevel 1 goto app_ready
set /a tries+=1
if %tries% lss 30 goto wait_app
chcp 65001 >nul
echo.
echo  [!] 앱이 켜지지 않았어요. 바탕화면 '논문 AI' 아이콘을 먼저 눌러 주세요.
pause
exit /b 1

:app_ready
chcp 65001 >nul
echo.
echo  앱이 켜져 있어요. 웹 주소를 엽니다.
echo  Forwarding 옆 https:// 주소가 친구들이 들어올 주소예요. (매번 같은 주소)
echo  초대 코드는 앱의 [설정 - 친구 초대] 칸에 있어요.
echo  ** 이 창을 닫으면 웹 주소가 꺼져요 **
echo.
timeout /t 3 /nobreak >nul
"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe" http 8765
pause
