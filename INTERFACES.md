# How our modules connect — "Where should HK$100k sit?"

Agree this file **before** writing more code. If you need to change a format,
change it here first, tell the group, and update the tests that use it. Then run
`python integration_check.py`.

```
Member 1 (banks, IB)  ──► bank_rows, broker_rates ─┐
                                                   ├─► Member 3: compare_options() ──► one ranked DataFrame
Member 2 (FX, T-bill) ──► fx, tbill_yields ────────┘                                     │
                                                       Member 4 (Streamlit + Plotly) ◄───┤
                                                       Member 5 (email, schedule)    ◄───┘
```

The one call everyone downstream uses. Modules 2 and 3 are combined in `market_and_returns.py`:

```python
from market_and_returns import get_ranking, read_bank_csv, read_json

table, warnings = get_ranking(100000, 12, bank_rows, broker_rates)   # Member 1's data
# table: pandas DataFrame, best route first (columns: section 5), or None if an input is unusable
# warnings: text to show the user, e.g. "FX rate is from 2026-11-20, 6 days old"
```

Every morning, before ranking: `python market_and_returns.py update`. Before every merge: `python market_and_returns.py test`.

## Units everyone uses

| What | Rule | Example | The mistake it prevents |
|---|---|---|---|
| Interest rates | **percent number** | `4.2` means 4.2% | `0.042` or `"4.20%"` silently gives 100× too little, or a crash |
| FX rate | **HKD per 1 USD** | `7.8448` | `0.1275` is the pair the wrong way up |
| Horizon | whole **months**, 1, 3, 6 or 12 | `12` | "1Y" vs "12 months" vs `365` days |
| Money | float, rounded to the cent at each step (week 2) | `177572.39` | off-by-a-cent mismatches between modules |
| Currency codes | `"HKD"` or `"USD"`, upper case | `"USD"` | `"usd"` matches nothing |
| Dates | ISO text | `"2026-11-21"`, `"2026-11-21T08:00:00"` | mixing `21/11` and `11/21` |
| Provider names | spelled the same everywhere | `"eleBank"`, `"HSBC"`, `"IB"` | `"Elebank"` gets no FX quote and no spread |

## 1. Member 1 → `bank_rows` (bank deposits)

`load_bank_deposits()` returns a **list of dictionaries**, one per bank, currency, tenor **and tier**:

| key | type | example | note |
|---|---|---|---|
| bank | str | `"eleBank"` | same spelling as in `fx["quotes"]` |
| currency | str | `"USD"` | `"HKD"` or `"USD"` only |
| tenor_label | str | `"12 months"` | as the bank writes it |
| tenor_months | float | `12.0` | 1 week → agree a convention, e.g. `0.25` |
| rate_pct | float | `4.2` | percent |
| min_amount | float or None | `100000.0` | in the row's own currency. `None` = any amount. One row per tier |
| conditions | str | `"new funds only"` | `""` if none |
| source_url, source_type | str | `"json"` / `"html"` | |
| fetched_at | str | `"2026-11-21T08:00:00"` | |

**When loading from CSV**, convert `tenor_months`, `rate_pct` and `min_amount` to float, and an empty `min_amount` to `None`. A CSV gives back only text; `read_bank_csv()` in `market_and_returns.py` shows how.

## 2. Member 1 → `broker_rates` (IB cash)

Proposed owner: Member 1, using the week 5 `ib_rate()` scraper. **Please confirm.**

```python
[{"provider": "IB",
  "HKD": {"rate_pct": 1.971, "zero_interest_up_to": 78000.0},
  "USD": {"rate_pct": 3.38, "zero_interest_up_to": 10000.0},
  "source_url": "...", "fetched_at": "2026-11-21T08:00:00"}]
```

`zero_interest_up_to` is the part of the balance that earns nothing ("first USD 10k earns 0").

## 3. Member 2 → FX (Part 1 of `market_and_returns.py`)

`get_latest_fx()` returns `{"date": "2026-11-20", "base": "USD", "quote": "HKD", "rate": 7.7791}`, or `None` if the database is empty.

`fx_from_module2()` in `market_and_returns.py` turns it into what `compare_options` reads:

```python
{"pair": "USD/HKD", "mid": 7.7791, "date": "2026-11-20", "source": "frankfurter (Module 2)"}
```

- `rate` comes from Frankfurter: the ECB reference rate, which is a **mid** rate (week 5 sample 1).
- Optional `"quotes": {"HSBC": {"buy_usd": ..., "sell_usd": ...}}`. `buy_usd` is what the **customer pays** for 1 USD (the higher price). A provider without a quote gets an estimated spread from `FX_SPREAD_PCT`.

## 4. Member 2 → T-bill yields (Part 1 of `market_and_returns.py`)

`get_latest_yields()` returns `{"date": "2026-11-20", "yields": {"1M": 3.73, "3M": 3.74, "6M": 3.79, "1Y": 3.8, ...}}`, or `None`.

`tbill_from_module2()` keeps the four horizons, `"1M"`→1, `"3M"`→3, `"6M"`→6, `"1Y"`→12:

```python
{"date": "2026-11-20", "source": "US Treasury (Module 2)", "yields_pct": {1: 3.73, 3: 3.74, 6: 3.79, 12: 3.8}}
```

- A tenor that is blank on the latest date is left out, so that horizon has no T-bill route. It is never 0.
- If Module 2's update fails, it keeps the old data (correct). `stale_data_warnings()` says so when the data is more than 5 days old.
- For the "gap over time" chart, Member 4 can use `get_daily_dataset()`: FX plus bill yields, one row per day, weekends filled forward.

## 5. Member 3 → everyone: the ranking DataFrame

`compare_options()` returns **one pandas DataFrame**, best first, or `None` if an input breaks the rules above (the reasons are printed). **Do not redo any calculation in the dashboard or the email.** Display these columns:

| column | meaning |
|---|---|
| rank | 1 = most HK$ at the end. Ties share a rank |
| option | e.g. `"eleBank (carry)"`, `"IB (carry, T-bill)"` |
| provider, kind | kind: `hkd_deposit`, `usd_deposit`, `hkd_cash`, `usd_cash`, `tbill` |
| currency | `"HKD"` (no FX risk) or `"USD"` (FX risk) — the dashboard's currency filter |
| rate_pct | the rate used |
| hkd_end, earned, return_pct | HK$ at the end, gain in HK$, gain in % (not annualised) |
| vs_best_hkd, best_hkd_option | gap to the best no-FX-risk route |
| fx_risk | True for USD routes |
| breakeven_vs_best_hkd | the exit rate at which a USD route only matches the best HKD route |
| breakeven_same_provider | the same, against the same provider's HKD rate (the week 5 table) |
| hkd_if_exit_at_7.75 / 7.85 | the result at each edge of the HKMA band |
| band_verdict | `cannot lose inside the band` / `can lose inside the band` / `loses even at 7.85` / `no FX risk` |
| usd_end | USD held at the end, after fees |
| principal, months, fx_mid, fx_date, tbill_date | the inputs, for the timestamps in the dashboard and the email |

Empty values are `NaN` in the DataFrame (pandas turns `None` into `NaN`). Check them with `pd.isna(x)`.

**Gap over time:** Member 5's daily job appends each day's table to `data/rankings_history.csv`, with the run date. Member 4's Plotly chart reads that file. Bank rates have no history before we start collecting, so start the job early.

## 6. Shared assumptions (in `market_and_returns.py`, one place only)

| assumption | value | source |
|---|---|---|
| HKMA band | 7.75 – 7.85 | HKMA; week 2 |
| FX spreads, when not fetched | eleBank 0.315%, HSBC 0.637%, IB 0.003%; unknown bank 0.637% | week 5 class table — **estimates** |
| IB FX commission | USD 2 each way | week 5 class table; check IB's fee page |
| IB T-bill commission | USD 5 | week 5 class table; check IB's fee page |
| Interest | simple, `rate × months/12` | week 2 `simple_interest` |
| Exit rate in the ranking | today's rate | the future rate is unknown — the break-even shows the risk |

## 7. Rules for merging (GitHub)

1. One branch per module, with a pull request into `main`. Someone else reviews every pull request.
2. **Before** opening a pull request, run your own tests **and** `python integration_check.py`.
3. A format change updates this file and the tests in the **same** pull request.
4. Never commit `.env` or keys: add `.env` to `.gitignore` on day 1 (week 6).
5. Each module has its own tests with `assert` on saved inputs, including at least one number checked by hand.
