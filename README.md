# hkd-cash-allocation-analyzer

**Where should HK$100,000 sit for 1, 3, 6 or 12 months?**

A decision-support app (ECON3086, Group K) that compares HKD and USD time deposits at 5 banks, Interactive Brokers (IBKR) cash and US Treasury bills, after FX costs and fees. It ranks every route by the HK$ you end with, shows the FX break-even rate for each USD route, and checks whether a move inside the HKMA band (7.75–7.85) could wipe out the gain. Results are shown on an interactive dashboard and sent as a daily email with a PDF report.

## How it fits together

```
Bank data (Member 1) ─┐
                      ├─► Financial model (Member 3): get_ranking() ─┬─► Dashboard (Member 4)
Market data (Member 2)┘        one ranked table, best first          └─► Daily email (Member 5)
```

| Module | What it does | Main file |
|---|---|---|
| 1. Bank data | Downloads HKD and USD time-deposit rates from eleBank (JSON), HSBC, Standard Chartered, BEA and Mox (web scraping) | `bank_data.py` |
| 2. Market data | Downloads USD/HKD (Frankfurter API) and US Treasury yields (Treasury CSV) into a SQLite database | `market_and_returns.py`, Part 1 |
| 3. Financial model | Calculates each route, the FX break-even and the HKMA band check, then ranks | `market_and_returns.py`, Parts 2–3 |
| 4. Dashboard | Streamlit + Plotly website with amount, period and currency filters | `dashboard.py` |
| 5. Email report | Daily email with key findings and a PDF, scheduled on GitHub Actions | `email_report.py` |

The dashboard and the email never redo the maths: both call `get_ranking()`, so they always show the same numbers. The data formats between modules are agreed in [INTERFACES.md](INTERFACES.md).

## Quick start

Needs Python 3. Live data needs the internet; the tests do not.

```bash
git clone https://github.com/alua-hkbu/hkd-cash-allocation-analyzer
cd hkd-cash-allocation-analyzer
pip install -r requirements.txt
python integration_check.py            # should end with ALL PASSED
```

| Task | Command |
|---|---|
| Load FX and Treasury history (first time) | `python market_and_returns.py backfill --years 5` |
| Refresh FX and Treasury data | `python market_and_returns.py update` |
| Open the dashboard | `streamlit run dashboard.py` |
| Show the ranking in the terminal | `python live_ranking.py --principal 100000 --months 12` |
| Build the email without sending it | `python email_report.py --dry-run` (files appear in `output/`) |
| Build and send the email | `python email_report.py --send` |

## The daily email

Every day at 08:00 Hong Kong time, GitHub Actions ([daily_report.yml](.github/workflows/daily_report.yml)) runs all tests, refreshes the data, ranks every route for 1, 3, 6 and 12 months, and emails the key findings with a PDF. It also saves the day's ranking to `data/rankings_history.csv`, for the return-gap-over-time chart.

- **Send one now:** Actions → *Daily HK$100k report* → *Run workflow*.
- **Settings:** the repo needs three secrets under Settings → Secrets and variables → Actions: `EMAIL_SENDER`, `EMAIL_PASSWORD` (a Gmail app password) and `EMAIL_TO` (addresses separated by commas). Without them the job builds the report but does not send it.
- **From a laptop:** copy `email_config.example.json` to `email_config.json` and fill it in. That file is in `.gitignore`; never commit it.

## How the calculation works

1. **HKD route:** principal × (1 + rate × months / 12), simple interest.
2. **USD route (carry):** buy USD at the provider's selling price, earn USD interest, sell back at its buying price; the spread is the FX cost.
3. **IBKR:** the same, minus USD 2 per conversion and USD 5 per T-bill; the first USD 10,000 (HKD 78,000) of cash earns nothing.
4. **Amount bands:** the highest band you qualify for is used; a missing rate is skipped, never treated as 0%.
5. **FX break-even:** the exit USD/HKD rate at which a USD route only matches the best HKD route.
6. **HKMA band check:** each USD route is tested at 7.75 and 7.85: *cannot lose inside the band*, *can lose inside the band*, or *loses even at 7.85*.

**Checked by hand** (HK$100,000, eleBank, 12 months): 100,000 ÷ 7.85717 = US$12,727.23 → + 4.2% interest = US$13,261.77 → × 7.83243 = **HK$103,871.89**, against HK$103,400.00 for staying in HKD at 3.4%. Break-even: 103,400 ÷ 13,261.77 = **7.7968**. This example is one of the tests.

## Testing

`python integration_check.py` runs every test in the project and checks that the modules fit together. Every pull request into `main` runs it automatically ([tests.yml](.github/workflows/tests.yml)).

| Command | What it checks |
|---|---|
| `python test_bank_data.py` | Each bank's page is read correctly: rates, tenors, amount bands, conditions |
| `python market_and_returns.py test` | 25 tests: FX and Treasury parsing, no duplicate dates, old data kept when a source fails, every calculation, the hand check, the HKMA verdicts, bad input caught |
| `python test_email_report.py` | 10 tests: the email uses the model's numbers unchanged, findings, PDF, history, sending with retries |

## Files

| File | Purpose |
|---|---|
| `bank_data.py`, `test_bank_data.py`, `try_live.py` | Bank data, its tests, and a debug script that tries each bank live |
| `market_and_returns.py` | Market data, financial model, `get_ranking()` and self-tests |
| `live_ranking.py` | Loads all 5 banks in one call and ranks them |
| `dashboard.py` | Streamlit dashboard |
| `email_report.py`, `test_email_report.py` | Daily email report and its tests |
| `integration_check.py` | Runs every module's tests and the integration checks |
| `data/market_data.db` | FX rates and Treasury yields (SQLite) |
| `data/broker_rates.json` | IBKR cash rates, typed in by hand: check them against IBKR's rates page |
| `data/rankings_history.csv` | One ranking per day, written by the daily email |
| `INTERFACES.md` | Agreed data formats between modules |
| `TEAM_GUIDE.md`, `README_part5.md` | Team workflow notes and email setup details |

## Working on the code

Each member works on their own branch (`feature/bank-data`, `feature/market-data`, `feature/financial-model`, `feature/dashboard`, `feature/email-reports`) and opens a pull request into `main`. Another member reviews it, the tests must pass, and only then is it merged. Nobody pushes to `main` directly. Update your branch from `main` before starting new work.

## Assumptions and limits

- The exit FX rate cannot be predicted; the break-even and the HKMA band check show the risk instead.
- FX spreads are estimates from the week 5 class table (eleBank 0.315%, HSBC and unknown banks 0.637%, IBKR 0.003%), set in one place in `market_and_returns.py`.
- Promotional rates (BEA, Mox) have conditions, such as new funds or online placement.
- IBKR rates are typed in by hand in `data/broker_rates.json`.
- If a bank changes its website, it stops loading until its parser is updated; the report lists it under failed banks.
- This is a student project for comparison, not financial advice.
