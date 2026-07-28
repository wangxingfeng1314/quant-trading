@echo off
cd /d "E:\wxf\claude\quant-trading"
set LOG="E:\wxf\claude\quant-trading\logs\scheduler.log"
echo [%date% %time%] start >> %LOG%

"%USERPROFILE%\AppData\Local\Programs\Python\Python311\python.exe" -c "
import sys; sys.path.insert(0, 'E:\\wxf\\claude\\quant-trading')
from data.storage import update_lock
from scripts.init_data import run_update

with update_lock(timeout=300):
    run_update(days=3, watchlist=True)
" >> %LOG% 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [%date% %time%] failed >> %LOG%
    exit /b 1
)
echo [%date% %time%] done >> %LOG%
exit /b 0
