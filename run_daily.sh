#!/bin/sh
# cron runs this every morning (see README_part5.md).
cd "$(dirname "$0")"
mkdir -p output
python3 email_report.py --send >> output/daily_log.txt 2>&1
