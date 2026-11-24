# Team guide: "Where should HK$100k sit?"

## Who owns which file

Branches follow the plan: `feature/bank-data`, `feature/market-data`, `feature/financial-model`, `feature/dashboard`, `feature/email-reports`. Each one is merged into `main` through a pull request that another member reviews. **Merge order:** bank-data → financial-model → dashboard → email-reports. Each pull request shows a green tick when every member's tests pass.

| Member | Files |
|---|---|
| 1 – Bank data | `bank_data.py`, `test_bank_data.py`, `try_live.py` |
| 2 + 3 – FX, Treasury, ranking | `market_and_returns.py`, `data/market_data.db` |
| 4 – Dashboard | `dashboard.py` |
| 5 – Email and integration | `email_report.py`, `test_email_report.py`, `integration_check.py`, `data/broker_rates.json`, `live_ranking.py`, `.github/workflows/` |

`live_ranking.py` (the bank loader the email uses) joins Members 1–3 together; its content is unchanged from the version handed to Member 5. Change only your own files. If you need something changed in someone else's file, ask that person.

## Everyone: first setup (once)

```
cd path/to/the/project/folder
pip install -r requirements.txt matplotlib reportlab
python integration_check.py           # must end with ALL PASSED
```

## Everyone: what you can do

| I want to… | Command | Needs the Gmail password? |
|---|---|---|
| see the dashboard | `streamlit run dashboard.py` | no |
| see today's report without sending it | `python email_report.py --dry-run`, then open the `output/` folder | no |
| check that I did not break anything | `python integration_check.py` | no |
| get the daily email | send your address to Member 5 | no |

**Rule before you share a changed file:** run `python integration_check.py`. If it doesn't end with ALL PASSED, fix the problem first.

## Who sends the daily email

Only **one** setup should send the daily email, or everyone gets it twice. Choose one of these:

### Option A: Member 5's Mac (works now)

- `email_config.json` on Member 5's Mac holds the sender account and the list of recipients. To add a teammate, put their address in the `"to"` list:
  `"to": ["abdraimalua@gmail.com", "teammate2@...", "teammate3@..."]`
- cron sends the email at 08:00 (see `README_part5.md`, step 4).
- Downside: the Mac must be awake and online at 08:00.

### Option B: GitHub (recommended: no laptop needed, and anyone can send it)

1. The workflow is already in the repo at `.github/workflows/daily_report.yml`. Until the secrets below exist, each run builds the report without sending it.
   - `.gitignore` already keeps `email_config.json` (the password) out of the repo.
2. In the repo, open **Settings → Secrets and variables → Actions → New repository secret** and add three secrets:
   - `EMAIL_SENDER`: `pythonfintech3086@gmail.com`
   - `EMAIL_PASSWORD`: the 16-letter Gmail app password, without spaces
   - `EMAIL_TO`: every recipient, separated by commas, e.g. `abdraimalua@gmail.com, teammate2@..., teammate3@...`
3. Open the **Actions** tab, choose **Daily HK$100k report** and click **Run workflow**. That sends one email now.
   - It runs every day at about 08:00 Hong Kong time.
   - Each run first runs everyone's tests, then sends the email, then saves the updated FX data and ranking history (`data/rankings_history.csv`) back to the repo.
   - The PDF of each run is under that run's **Artifacts**.
   - If a run fails, GitHub emails the repo owner.
4. If you choose B, stop the Mac job so the email isn't sent twice: run `crontab -r` on Member 5's Mac.

Note: some bank websites may refuse GitHub's servers. The email still goes out with the banks that loaded, and its Data section lists the banks that failed.

## Passwords

- The Gmail app password lives only in `email_config.json` on the sending Mac, or in the GitHub secret.
- Never put it in the group chat, a slide, the report or the repo.
- If it leaks, delete it at myaccount.google.com/apppasswords and make a new one.

## Before the demo

1. Check the IBKR rates in `data/broker_rates.json` against IBKR's interest-rate page and update `fetched_at`.
2. Run `python integration_check.py`.
3. Send one email manually (with the **Run workflow** button, or `python email_report.py --send`) and check that everyone received it.
