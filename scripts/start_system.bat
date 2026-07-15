@echo off
chcp 65001 >nul
title A股量化交易系统
cd /d "E:\wxf\claude\quant-trading"
echo [%date% %time%] 启动量化交易系统...
python run.py
