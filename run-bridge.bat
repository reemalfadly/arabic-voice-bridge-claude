@echo off
chcp 65001 >nul
REM ============================================================
REM  مشغّل الجسر الصوتي (تلغرام <-> كلود)
REM  عبّئ القيم الثلاث ثم احفظ الملف. لا تشارك هذا الملف.
REM ============================================================

set "TELEGRAM_BOT_TOKEN=ضع_توكن_البوت_من_BotFather"
set "TELEGRAM_ALLOWED_CHAT_ID=ضع_رقم_محادثتك"
set "CLAUDE_CODE_OAUTH_TOKEN=ضع_توكن_كلود_من_claude_setup-token"
set "BRIDGE_WORKSPACE=C:\Users\<اسمك>\Downloads"
set "BRIDGE_UV_PATH=uv"
REM اختياري: set "BRIDGE_MODEL=claude-sonnet-5"

cd /d "%~dp0"
"%BRIDGE_UV_PATH%" run --script bridge.py
if /I not "%~1"=="silent" pause
