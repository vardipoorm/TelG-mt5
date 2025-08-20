@echo off
echo =========================
echo Running Python script...
echo to stop it use (Ctrl+C)
echo =========================
echo.
:: این دستور فایل پایتون را از همان پوشه فعلی اجرا می‌کند
python "%~dp0bot_script.py"

echo Script finished. Press any key to close.
pause
