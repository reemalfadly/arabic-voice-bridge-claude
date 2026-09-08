@echo off
chcp 65001 >nul
title اعداد الجسر الصوتي
setlocal
echo.
echo   ========  اعداد الجسر الصوتي  ========
echo.
set "BOT="
set "CHAT="
set "CLD="
set "WS="
set /p BOT=[1/4] الصق توكن البوت من BotFather: 
set /p CHAT=[2/4] الصق رقم محادثتك chat id: 
set /p CLD=[3/4] الصق توكن كلود من claude setup-token: 
set /p WS=[4/4] مسار مجلد العمل (Enter للافتراضي Downloads): 
if "%WS%"=="" set "WS=%USERPROFILE%\Downloads"
(
  echo @echo off
  echo chcp 65001 ^>nul
  echo set "TELEGRAM_BOT_TOKEN=%BOT%"
  echo set "TELEGRAM_ALLOWED_CHAT_ID=%CHAT%"
  echo set "CLAUDE_CODE_OAUTH_TOKEN=%CLD%"
  echo set "BRIDGE_WORKSPACE=%WS%"
  echo set "BRIDGE_UV_PATH=uv"
  echo cd /d "%%~dp0"
  echo "%%BRIDGE_UV_PATH%%" run --script bridge.py
  echo if /I not "%%~1"=="silent" pause
) > run-bridge.bat
echo.
echo   تم انشاء run-bridge.bat . انقره نقرتين لتشغيل الجسر.
echo.
pause
