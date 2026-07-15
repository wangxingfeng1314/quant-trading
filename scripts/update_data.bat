@echo off
cd /d "E:\wxf\claude\quant-trading"
set LOG="E:\wxf\claude\quant-trading\logs\scheduler.log"
echo [%date% %time%] start >> %LOG%

"%USERPROFILE%\AppData\Local\Programs\Python\Python311\python.exe" "E:\wxf\claude\quant-trading\scripts\init_data.py" --update --days 3 --watchlist >> %LOG% 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [%date% %time%] failed >> %LOG%
    exit /b 1
)
echo [%date% %time%] done >> %LOG%
exit /b 0
