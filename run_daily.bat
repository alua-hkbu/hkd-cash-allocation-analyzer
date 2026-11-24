@echo off
REM Windows Task Scheduler runs this every morning (see README_part5.md).
cd /d "%~dp0"
python email_report.py --send >> output\daily_log.txt 2>&1
