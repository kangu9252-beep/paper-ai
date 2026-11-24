@echo off
chcp 65001 >nul
title 논문 AI 웹 공유
cd /d "%~dp0"

rem 앱이 꺼져 있으면 먼저 켠다
curl -s -o nul http://127.0.0.1:8765/api/auth/info
if errorlevel 1 (
  echo  앱을 먼저 켜는 중이에요...
  start "논문 AI" /min cmd /c start.bat
  timeout /t 10 >nul
)

echo.
echo  논문 AI를 웹 주소로 엽니다.
echo  아래 화면의 Forwarding 옆 https:// 주소가 친구들이 들어올 주소예요. (매번 같은 주소)
echo  초대 코드는 앱의 [설정 - 친구 초대] 칸에 있어요.
echo  ** 이 창을 닫으면 웹 주소가 꺼져요 **
echo.
timeout /t 3 >nul
"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe" http 8765
pause
