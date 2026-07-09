@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 爆款监控
start "" pythonw -X utf8 desktop.py
