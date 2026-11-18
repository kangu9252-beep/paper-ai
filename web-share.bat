@echo off
chcp 65001 >nul
title 논문 AI 웹 공유
echo.
echo  논문 AI를 웹 주소로 여는 중이에요... (10초쯤 걸려요)
echo  먼저 바탕화면 '논문 AI' 아이콘으로 앱을 켜 두세요.
echo.
echo  아래 https://....trycloudflare.com 주소를 친구들에게 보내 주세요.
echo  초대 코드는 앱의 [설정 - 친구 초대] 칸에 있어요.
echo  ** 이 창을 닫으면 웹 주소가 꺼져요. 켤 때마다 주소가 바뀌어요. **
echo.
"C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --no-autoupdate --url http://127.0.0.1:8765 2>&1 | findstr /i /c:"trycloudflare.com"
pause
