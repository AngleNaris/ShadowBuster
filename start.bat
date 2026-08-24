@echo off
chcp 65001 >nul
title Soren 母带工坊
cd /d D:\_3.AI\audio_upscale\SorenStudio
D:\_3.AI\audio_upscale\UniverSR\.venv\Scripts\python.exe main.py
if errorlevel 1 pause
