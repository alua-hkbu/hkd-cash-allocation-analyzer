# Part 5: Daily email report and final integration

## What Part 5 adds

| File | What it does |
|---|---|
| `email_report.py` | Every morning it refreshes FX and Treasury data, loads bank rates once and ranks every route for 1, 3, 6 and 12 months with Member 3's `get_ranking()`. It then writes the key findings, builds a text + HTML email with a PDF attached, saves the day to `data/rankings_history.csv` and sends the email. |
| `test_email_report.py` | 10 offline tests: the numbers are Member 3's unchanged, the findings, history, PDF, email parts, sending with retries, settings and a whole run. |
| `integration_check.py` | Final integration check: runs every member's tests, then checks that the modules fit together. |
| `data/broker_rates.json` | The IBKR cash rates, typed in by hand (see "Integration changes"). |
| `email_config.example.json` | A template for the email settings. |
| `run_daily.bat` / `run_daily.sh` | What the Windows or Mac scheduler runs every morning. |

The email module **does not recalculate any return**. Every number comes from `get_ranking()`.

## 1. Install

```
pip install -r requirements.txt matplotlib reportlab
python market_and_returns.py backfill --years 1     # once, if data/market_data.db is empty
python integration_check.py                             # should end with ALL PASSED
```

## 2. Email settings (Gmail)

1. Turn on 2-Step Verification for the Google account that will send the email.
2. Create an **App password** (Google Account → Security → App passwords). It is 16 letters long.
   Your normal Gmail password will not work.
3. Copy `email_config.example.json` to `email_config.json` and fill in the sender, the app password and the recipients.
   `email_config.json` is in `.gitignore`. **Never commit it or put it in the report.**

You can use environment variables instead of the file: `EMAIL_SENDER`, `EMAIL_PASSWORD`, `EMAIL_TO` (comma-separated), `SMTP_HOST` and `SMTP_PORT`.
For Outlook, use `smtp.office365.com` and port `587`.

## 3. Run it

```
python email_report.py --dry-run      # builds output/report_<date>.html, .eml and .pdf; sends nothing
python email_report.py --send         # the real thing
python email_report.py --send --principal 250000
```

Open the `.eml` file to see exactly what the email will look like before you send it.

## 4. Schedule the morning email (08:00 Hong Kong time)

**Windows (Task Scheduler).** Run once in Command Prompt, using your own path:
```
schtasks /Create /SC DAILY /ST 08:00 /TN "HK100k daily report" /TR "C:\path\to\project\run_daily.bat"
```

**Mac / Linux (cron).** Run `crontab -e` and add:
```
0 8 * * * /path/to/project/run_daily.sh
```

**No scheduler.** Leave this running in a terminal; it sends every day at 08:00:
```
python email_report.py --send --daily-at 08:00
```

The computer must be on and online at 08:00. The output of each run goes to `output/daily_log.txt`.

## 5. What the email contains

- **Subject:** for example, `HK$100k ranking 24 Nov 2026: best 12m = IB (carry, T-bill) (+HK$4,433.79)`
- **Key findings:**
  - the best route and its gain
  - for a USD route: the gain over the best HKD route, the FX break-even rate and today's mid rate
  - the HKMA band verdict in plain words
  - whether the top rate is promotional
  - the best route for each horizon
  - what changed since the last report
- **The top 5 routes for each horizon,** with break-even and band verdict.
- **Data notes:**
  - FX and T-bill dates
  - how many banks loaded and which ones failed
  - stale-data warnings
  - the reminder to re-check the IB rates
- **The PDF:**
  - findings
  - a chart of the 12-month gain per route and of USD/HKD inside the 7.75–7.85 band
  - the full ranking for each horizon, with the 7.75 and 7.85 exit values and each rate's conditions
  - an explanation of the FX columns

If every bank fails, the email still goes out, and its subject says "no ranking today - data problem". A silent morning would look the same as a broken scheduler.

## Integration notes (teammates' files are unchanged)

All teammates' files are exactly as they were handed over. Part 5 only adds new files.

1. **IBKR rates.** No one supplied `broker_rates`, so the IB cash and **T-bill** routes never appear in `live_ranking.py` or the dashboard.
   - The email loads the IBKR rates itself from `data/broker_rates.json` (INTERFACES.md format), so it ranks them.
   - As a result, **the email ranks IB routes and the dashboard does not**, until Member 1 or Member 4 decides to use the same file.
   - USD 3.38% was checked against IBKR's page. HKD 1.971% is the week 5 figure. **Check both before the demo** and update `fetched_at`.
2. **History.** `email_report.py` writes `data/rankings_history.csv`, with one block of rows per day.
   - The dashboard does not read it yet. Member 4 can use it for "historical charts showing return gaps over time".

## Final testing done

- `integration_check.py`: bank_data tests, 25 market/ranking tests, 10 email tests and the integration checks all pass.
- **End-to-end run** on the real `data/market_data.db` (FX and Treasury up to 20 Nov 2026) with sample bank rows:
  - It produced the email, PDF and history.
  - Its 12-month figure for IB T-bill (HK$104,433.79) is identical to calling `get_ranking()` directly.
- **Still to do on a home network:** `python try_live.py` and `python email_report.py --dry-run` with the real bank websites. The bank websites were not reachable from the machine I built this on.
