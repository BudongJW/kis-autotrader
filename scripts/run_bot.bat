@echo off
cd /d C:\Users\wodnj\Downloads\kis-autotrader
set PYTHONPATH=.
set PYTHONIOENCODING=utf-8
.venv\Scripts\python.exe -m src.bot.runner --strategy volatility_breakout --symbol 005930 --live --auto-confirm >> logs\bot_%date:~0,4%%date:~5,2%%date:~8,2%.log 2>&1
