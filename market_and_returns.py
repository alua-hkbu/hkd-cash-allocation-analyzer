# =============================================================================
# market_and_returns.py - Modules 2 + 3 of "Where should HK$100k sit?"
# ECON3086 group project. ONE file to hand on to the dashboard / email modules.
#
#   PART 1  Module 2 - FX & Treasury data       written by Member 2
#           (one fix by Member 3, marked "FIX (Member 3)")
#   PART 2  Module 3 - returns and ranking      written by Member 3
#   PART 3  The one call for Members 4 and 5:   get_ranking(...)
#   PART 4  Self-tests, no internet:            python market_and_returns.py test
#
# Quick start
#   pip install pandas requests
#   python market_and_returns.py backfill --years 5   # once: load history
#   python market_and_returns.py update               # every morning
#   python market_and_returns.py rank --principal 100000 --months 12
#   python market_and_returns.py test                 # run before every merge
# =============================================================================

"""
Module 2 - FX & Treasury Data Processing (Member 2)

What it does
------------
1. Fetches USD/HKD exchange rates from the Frankfurter API.
2. Fetches US Treasury par yield curve rates (T-bill tenors 1M/2M/3M/4M/6M/1Y,
   plus longer tenors) from the U.S. Department of the Treasury CSV feed.
3. Stores everything in SQLite (duplicate dates are overwritten, not duplicated).
4. Handles API failures (retries, fallback to the host mirror, fall back to
   the data already in the database) and missing values.
5. Exposes simple functions for other modules:
       get_latest_fx()            -> dict
       get_latest_yields()        -> dict
       get_fx_history(start, end) -> pandas.DataFrame
       get_yield_history(...)     -> pandas.DataFrame (wide, one row per date)
       get_daily_dataset(...)     -> pandas.DataFrame (FX + yields, forward-filled)

Command line
------------
    python market_and_returns.py init                 # create tables
    python market_and_returns.py backfill --years 5   # load history from the start
    python market_and_returns.py update               # refresh latest data (run daily)
    python market_and_returns.py latest               # print latest FX + yields
    python market_and_returns.py export               # write CSVs for Member 3

Conventions (agree these with Members 1 & 3 - see CONFIG below)
---------------------------------------------------------------
    dates       : ISO text 'YYYY-MM-DD'
    fx rate     : 1 USD = <rate> HKD   (base=USD, quote=HKD), float
    yields      : percent per annum, e.g. 4.35 means 4.35 %   (NOT 0.0435)
    tenor names : '1M','2M','3M','4M','6M','1Y','2Y','3Y','5Y','7Y','10Y','20Y','30Y'
"""

from __future__ import annotations

import argparse
import io
import logging
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import pandas as pd
import requests

# --------------------------------------------------------------------------- #
# CONFIG (shared conventions - change here, everyone follows)
# --------------------------------------------------------------------------- #
DB_PATH = Path("data/market_data.db")
EXPORT_DIR = Path("data/export")

BASE_CCY = "USD"
QUOTE_CCY = "HKD"

# Frankfurter: primary host first, mirror second (they serve the same data).
FRANKFURTER_HOSTS = [
    "https://api.frankfurter.dev/v1",
    "https://api.frankfurter.app",
]

TREASURY_CSV_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all"
    "?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
)

# Treasury column name -> our tenor name
TENOR_MAP = {
    "1 Mo": "1M", "2 Mo": "2M", "3 Mo": "3M", "4 Mo": "4M", "6 Mo": "6M",
    "1 Yr": "1Y", "2 Yr": "2Y", "3 Yr": "3Y", "5 Yr": "5Y", "7 Yr": "7Y",
    "10 Yr": "10Y", "20 Yr": "20Y", "30 Yr": "30Y",
}
BILL_TENORS = ["1M", "2M", "3M", "4M", "6M", "1Y"]  # T-bill part of the curve

REQUEST_TIMEOUT = 20      # seconds
MAX_RETRIES = 3
BACKOFF_SECONDS = 2       # waits 2s, 4s, 8s ...
USER_AGENT = "course-project-fx-treasury/1.0"

log = logging.getLogger("fx_treasury")


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS fx_rates (
    date        TEXT NOT NULL,              -- YYYY-MM-DD
    base        TEXT NOT NULL,              -- USD
    quote       TEXT NOT NULL,              -- HKD
    rate        REAL NOT NULL,              -- 1 base = rate quote
    source      TEXT NOT NULL DEFAULT 'frankfurter',
    fetched_at  TEXT NOT NULL,              -- UTC ISO timestamp
    PRIMARY KEY (date, base, quote)         -- prevents duplicate dates
);

CREATE TABLE IF NOT EXISTS treasury_yields (
    date        TEXT NOT NULL,              -- YYYY-MM-DD
    tenor       TEXT NOT NULL,              -- 1M, 3M, 6M, 1Y ...
    yield_pct   REAL NOT NULL,              -- percent per annum
    source      TEXT NOT NULL DEFAULT 'us_treasury',
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (date, tenor)               -- prevents duplicate dates
);

CREATE TABLE IF NOT EXISTS update_log (
    run_at   TEXT NOT NULL,
    dataset  TEXT NOT NULL,                 -- fx / treasury
    status   TEXT NOT NULL,                 -- ok / failed
    rows     INTEGER,
    message  TEXT
);
"""


def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # FIX (Member 3): create the tables on every connection. Before this,
    # get_latest_fx() on a brand-new database raised "no such table" instead
    # of returning None, so the app crashed before the first update.
    # CREATE TABLE IF NOT EXISTS keeps existing tables and data (week 9).
    conn.executescript(SCHEMA)
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
    log.info("Database ready at %s", db_path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log_run(conn, dataset: str, status: str, rows: int, message: str = "") -> None:
    conn.execute(
        "INSERT INTO update_log VALUES (?,?,?,?,?)",
        (_now(), dataset, status, rows, message),
    )


# --------------------------------------------------------------------------- #
# HTTP helper with retries
# --------------------------------------------------------------------------- #
def _get(url: str, params: dict | None = None) -> requests.Response:
    """GET with retries + exponential backoff. Raises the last error if all fail."""
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url, params=params, timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
            resp.raise_for_status()
            return resp
        except requests.RequestException as err:
            last_err = err
            wait = BACKOFF_SECONDS * (2 ** (attempt - 1))
            log.warning("GET %s failed (attempt %d/%d): %s", url, attempt, MAX_RETRIES, err)
            if attempt < MAX_RETRIES:
                time.sleep(wait)
    raise RuntimeError(f"All {MAX_RETRIES} attempts failed for {url}") from last_err


# --------------------------------------------------------------------------- #
# FX: Frankfurter
# --------------------------------------------------------------------------- #
def parse_frankfurter(payload: dict) -> pd.DataFrame:
    """Turn a Frankfurter JSON payload (latest OR time series) into a clean DataFrame.

    Columns: date, base, quote, rate
    """
    base = payload.get("base", BASE_CCY)
    rows = []

    if "rates" not in payload or not isinstance(payload["rates"], dict):
        raise ValueError("Frankfurter payload has no 'rates'")

    rates = payload["rates"]
    if QUOTE_CCY in rates:  # single-day response: {"date": ..., "rates": {"HKD": 7.8}}
        rows.append((payload["date"], base, QUOTE_CCY, rates[QUOTE_CCY]))
    else:                   # time series: {"rates": {"2024-01-02": {"HKD": 7.8}, ...}}
        for d, per_day in rates.items():
            if isinstance(per_day, dict) and QUOTE_CCY in per_day:
                rows.append((d, base, QUOTE_CCY, per_day[QUOTE_CCY]))

    df = pd.DataFrame(rows, columns=["date", "base", "quote", "rate"])
    return clean_fx(df)


def clean_fx(df: pd.DataFrame) -> pd.DataFrame:
    """Fix types, drop missing / non-positive values, drop duplicate dates."""
    if df.empty:
        return df
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["rate"] = pd.to_numeric(df["rate"], errors="coerce")
    before = len(df)
    df = df.dropna(subset=["date", "rate"])
    df = df[df["rate"] > 0]
    df = df.drop_duplicates(subset=["date", "base", "quote"], keep="last")
    dropped = before - len(df)
    if dropped:
        log.info("FX cleaning removed %d bad/duplicate rows", dropped)
    return df.sort_values("date").reset_index(drop=True)


def fetch_fx(start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """Fetch USD/HKD. No dates -> latest only. With dates -> time series."""
    if start is None:
        path = "/latest"
    else:
        end = end or date.today()
        path = f"/{start.isoformat()}..{end.isoformat()}"
    params = {"base": BASE_CCY, "symbols": QUOTE_CCY}

    last_err: Exception | None = None
    for host in FRANKFURTER_HOSTS:           # fall back to mirror on failure
        try:
            payload = _get(host + path, params).json()
            return parse_frankfurter(payload)
        except Exception as err:             # network, JSON, or format error
            last_err = err
            log.warning("Frankfurter host %s failed: %s", host, err)
    raise RuntimeError("Frankfurter unavailable on all hosts") from last_err


def save_fx(df: pd.DataFrame, db_path: Path = DB_PATH) -> int:
    if df.empty:
        return 0
    stamp = _now()
    rows = [(r.date, r.base, r.quote, float(r.rate), "frankfurter", stamp)
            for r in df.itertuples()]
    with get_conn(db_path) as conn:
        conn.executemany(
            """INSERT INTO fx_rates (date, base, quote, rate, source, fetched_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(date, base, quote) DO UPDATE SET
                   rate=excluded.rate, source=excluded.source,
                   fetched_at=excluded.fetched_at""",
            rows,
        )
    return len(rows)


# --------------------------------------------------------------------------- #
# Treasury yields
# --------------------------------------------------------------------------- #
def parse_treasury_csv(text: str) -> pd.DataFrame:
    """Parse the Treasury CSV into LONG format: date, tenor, yield_pct."""
    raw = pd.read_csv(io.StringIO(text))
    raw.columns = [c.strip() for c in raw.columns]
    if "Date" not in raw.columns:
        raise ValueError(f"Unexpected Treasury CSV columns: {list(raw.columns)[:6]}")

    keep = [c for c in TENOR_MAP if c in raw.columns]
    if not keep:
        raise ValueError("No known tenor columns in Treasury CSV")

    long = raw.melt(id_vars="Date", value_vars=keep,
                    var_name="tenor_raw", value_name="yield_pct")
    long["date"] = pd.to_datetime(long["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    long["tenor"] = long["tenor_raw"].map(TENOR_MAP)
    return clean_yields(long[["date", "tenor", "yield_pct"]])


def clean_yields(df: pd.DataFrame) -> pd.DataFrame:
    """Drop missing/blank yields (e.g. 2M/4M before 2018), dedupe dates."""
    if df.empty:
        return df
    df = df.copy()
    df["yield_pct"] = pd.to_numeric(df["yield_pct"], errors="coerce")
    before = len(df)
    df = df.dropna(subset=["date", "tenor", "yield_pct"])
    df = df[(df["yield_pct"] >= 0) & (df["yield_pct"] < 100)]  # sanity range
    df = df.drop_duplicates(subset=["date", "tenor"], keep="last")
    dropped = before - len(df)
    if dropped:
        log.info("Treasury cleaning removed %d missing/duplicate rows", dropped)
    return df.sort_values(["date", "tenor"]).reset_index(drop=True)


def fetch_treasury_year(year: int) -> pd.DataFrame:
    url = TREASURY_CSV_URL.format(year=year)
    resp = _get(url)
    return parse_treasury_csv(resp.text)


def save_yields(df: pd.DataFrame, db_path: Path = DB_PATH) -> int:
    if df.empty:
        return 0
    stamp = _now()
    rows = [(r.date, r.tenor, float(r.yield_pct), "us_treasury", stamp)
            for r in df.itertuples()]
    with get_conn(db_path) as conn:
        conn.executemany(
            """INSERT INTO treasury_yields (date, tenor, yield_pct, source, fetched_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(date, tenor) DO UPDATE SET
                   yield_pct=excluded.yield_pct, source=excluded.source,
                   fetched_at=excluded.fetched_at""",
            rows,
        )
    return len(rows)


# --------------------------------------------------------------------------- #
# Orchestration: backfill + update (never crash the whole run on one failure)
# --------------------------------------------------------------------------- #
def backfill(years: int = 5, db_path: Path = DB_PATH) -> None:
    """Load history from `years` ago until today."""
    init_db(db_path)
    today = date.today()
    start = date(today.year - years + 1, 1, 1)

    # --- FX (one request per calendar year keeps responses small) ---
    fx_total = 0
    try:
        for year in range(start.year, today.year + 1):
            y_start = max(start, date(year, 1, 1))
            y_end = min(today, date(year, 12, 31))
            fx_total += save_fx(fetch_fx(y_start, y_end), db_path)
        with get_conn(db_path) as conn:
            _log_run(conn, "fx", "ok", fx_total, "backfill")
        log.info("FX backfill: %d rows", fx_total)
    except Exception as err:
        log.error("FX backfill failed: %s", err)
        with get_conn(db_path) as conn:
            _log_run(conn, "fx", "failed", fx_total, str(err))

    # --- Treasury (one CSV per year) ---
    ty_total = 0
    try:
        for year in range(start.year, today.year + 1):
            try:
                ty_total += save_yields(fetch_treasury_year(year), db_path)
            except Exception as err:   # one bad year should not stop the rest
                log.error("Treasury %d failed: %s", year, err)
        with get_conn(db_path) as conn:
            _log_run(conn, "treasury", "ok" if ty_total else "failed", ty_total, "backfill")
        log.info("Treasury backfill: %d rows", ty_total)
    except Exception as err:
        log.error("Treasury backfill failed: %s", err)


def update(db_path: Path = DB_PATH) -> None:
    """Daily refresh. Pulls the last ~10 days so missed days get filled in."""
    init_db(db_path)
    today = date.today()

    try:
        n = save_fx(fetch_fx(today - timedelta(days=10), today), db_path)
        with get_conn(db_path) as conn:
            _log_run(conn, "fx", "ok", n, "update")
        log.info("FX updated: %d rows", n)
    except Exception as err:
        log.error("FX update failed, keeping existing data: %s", err)
        with get_conn(db_path) as conn:
            _log_run(conn, "fx", "failed", 0, str(err))

    try:
        n = save_yields(fetch_treasury_year(today.year), db_path)
        with get_conn(db_path) as conn:
            _log_run(conn, "treasury", "ok", n, "update")
        log.info("Treasury updated: %d rows", n)
    except Exception as err:
        log.error("Treasury update failed, keeping existing data: %s", err)
        with get_conn(db_path) as conn:
            _log_run(conn, "treasury", "failed", 0, str(err))


# --------------------------------------------------------------------------- #
# Read API for other modules (Member 3 etc.)
# --------------------------------------------------------------------------- #
def get_latest_fx(db_path: Path = DB_PATH) -> dict | None:
    """{'date': '2026-11-20', 'base': 'USD', 'quote': 'HKD', 'rate': 7.78} or None."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT date, base, quote, rate FROM fx_rates "
            "WHERE base=? AND quote=? ORDER BY date DESC LIMIT 1",
            (BASE_CCY, QUOTE_CCY),
        ).fetchone()
    return dict(row) if row else None


def get_latest_yields(db_path: Path = DB_PATH) -> dict | None:
    """{'date': ..., 'yields': {'1M': 4.3, '3M': 4.2, ...}} (percent p.a.) or None."""
    with get_conn(db_path) as conn:
        latest = conn.execute("SELECT MAX(date) AS d FROM treasury_yields").fetchone()["d"]
        if latest is None:
            return None
        rows = conn.execute(
            "SELECT tenor, yield_pct FROM treasury_yields WHERE date=?", (latest,)
        ).fetchall()
    return {"date": latest, "yields": {r["tenor"]: r["yield_pct"] for r in rows}}


def get_fx_history(start: str | None = None, end: str | None = None,
                   db_path: Path = DB_PATH) -> pd.DataFrame:
    q = "SELECT date, base, quote, rate FROM fx_rates WHERE base=? AND quote=?"
    params: list = [BASE_CCY, QUOTE_CCY]
    if start:
        q += " AND date>=?"; params.append(start)
    if end:
        q += " AND date<=?"; params.append(end)
    with get_conn(db_path) as conn:
        return pd.read_sql_query(q + " ORDER BY date", conn, params=params)


def get_yield_history(start: str | None = None, end: str | None = None,
                      bills_only: bool = False, db_path: Path = DB_PATH) -> pd.DataFrame:
    """WIDE format: one row per date, one column per tenor (e.g. '3M', '6M')."""
    q = "SELECT date, tenor, yield_pct FROM treasury_yields WHERE 1=1"
    params: list = []
    if start:
        q += " AND date>=?"; params.append(start)
    if end:
        q += " AND date<=?"; params.append(end)
    with get_conn(db_path) as conn:
        long = pd.read_sql_query(q, conn, params=params)
    if long.empty:
        return long
    wide = long.pivot(index="date", columns="tenor", values="yield_pct").reset_index()
    wide.columns.name = None
    if bills_only:
        wide = wide[["date"] + [t for t in BILL_TENORS if t in wide.columns]]
    return wide.sort_values("date").reset_index(drop=True)


def get_daily_dataset(start: str | None = None, end: str | None = None,
                      db_path: Path = DB_PATH) -> pd.DataFrame:
    """FX + T-bill yields on one table. Weekend/holiday gaps are forward-filled
    (a rate stays valid until the next published one)."""
    fx = get_fx_history(start, end, db_path)[["date", "rate"]].rename(
        columns={"rate": "usd_hkd"})
    ty = get_yield_history(start, end, bills_only=True, db_path=db_path)
    if fx.empty and ty.empty:
        return pd.DataFrame()
    merged = fx.merge(ty, on="date", how="outer").sort_values("date")
    merged["date"] = pd.to_datetime(merged["date"])
    merged = merged.set_index("date").asfreq("D").ffill().reset_index()
    merged["date"] = merged["date"].dt.strftime("%Y-%m-%d")
    return merged


def export_csv(db_path: Path = DB_PATH, out_dir: Path = EXPORT_DIR) -> None:
    """Write flat CSVs so Member 3 can load them without touching SQLite."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    get_fx_history(db_path=db_path).to_csv(out_dir / "fx_usd_hkd.csv", index=False)
    get_yield_history(db_path=db_path).to_csv(out_dir / "treasury_yields.csv", index=False)
    get_daily_dataset(db_path=db_path).to_csv(out_dir / "daily_dataset.csv", index=False)
    log.info("CSV files written to %s", out_dir)




# =============================================================================
# PART 2 - MODULE 3: RETURNS, BREAK-EVEN AND RANKING (Member 3)
# =============================================================================
# ECON3086 Group Project - Module 3: Financial calculations and ranking
# Owner: Member 3 (Maxim SURIN)
#
# The question: "I have HK$100,000 to park. Where should it go?"
#
# This module takes the numbers the other modules collect, works out what the
# money becomes on each route, and ranks the routes. It does NOT download
# anything: every number arrives as a parameter, so every function can be
# tested on saved inputs, as in weeks 5 and 6.
#
#   Member 1 gives:  bank_rows       (bank deposit rates, the agreed row format)
#                    broker_rates    (IB cash rates - see INTERFACES.md)
#   Member 2 gives:  fx              (USD/HKD from Frankfurter)
#                    tbill_yields    (Treasury bill yields, from assignment 2's code)
#   Members 4 and 5 call compare_options() and get one ranked pandas DataFrame.
#
# The arithmetic is the week 2 / week 5 carry trade (w5-6-carry-today.py):
#   1. stay in HKD                              -> hkd_if_you_stayed
#   2. change to USD at the SELL rate, earn USD interest, change back at the
#      BUY rate                                  -> hkd_after_usd_route
#   3. how bad must the exit rate be to lose     -> break-even exit rate
#   4. can the HKMA band 7.75-7.85 make it lose  -> band check
#
# Install once:  pip install pandas

# pandas is imported at the top of the file, in Part 1

# ---------------------------------------------------------------------------
# ASSUMPTIONS - every number here is a choice. Each says where it came from.
# Change them here, in ONE place, never inside a function.
# ---------------------------------------------------------------------------
BAND_STRONG_SIDE = 7.75     # HKMA buys HKD here: the strongest HKD can get
BAND_WEAK_SIDE = 7.85       # HKMA sells HKD here: the weakest HKD can get

# A bank's FX charge is its spread (week 2 slide 10). Banks rarely publish it,
# so these are ESTIMATES from the week 5 class table (29 Sep 2026). A quote
# Member 2 actually fetched always replaces the estimate (see fx_quotes_for).
FX_SPREAD_PCT = {"eleBank": 0.315, "HSBC": 0.637, "IB": 0.003}
DEFAULT_SPREAD_PCT = 0.637  # an unknown bank: assume the widest spread we know

# IB charges a commission on each currency conversion and on buying a T-bill.
# Fixed amounts from the week 5 class example; check IB's fee page before the demo.
IB_FX_FEE_USD = 2.0         # each conversion, HKD->USD and USD->HKD
IB_TBILL_FEE_USD = 5.0      # buying the T-bill

# IB pays nothing on the first part of a cash balance (week 5 slide 6:
# "first USD10k earns 0"). Member 1 can send the real numbers in broker_rates.
IB_ZERO_INTEREST_UP_TO = {"USD": 10000.0, "HKD": 78000.0}

HORIZONS_MONTHS = [1, 3, 6, 12]   # the horizons the app offers


# ---------------------------------------------------------------------------
# Week 2's own functions, unchanged. Money is rounded to the cent at each step.
# ---------------------------------------------------------------------------
def usd_to_hkd(usd, rate):
    return round(usd * rate, 2)


def hkd_to_usd(hkd, rate):
    return round(hkd / rate, 2)


def simple_interest(principal, annual_rate_pct, years):
    return round(principal * annual_rate_pct / 100 * years, 2)


def mid_rate(sell_rate, buy_rate):
    return round((sell_rate + buy_rate) / 2, 4)


def spread_pct(sell_rate, buy_rate):
    return round((sell_rate - buy_rate) / mid_rate(sell_rate, buy_rate) * 100, 4)


# ---------------------------------------------------------------------------
# FX: the two prices a provider gives you
# ---------------------------------------------------------------------------
def quotes_from_mid(mid, spread):
    """The two prices around a mid rate, for a spread in percent.

    Return: {"buy_usd": what YOU pay for 1 USD (the higher price),
             "sell_usd": what you GET for 1 USD (the lower price)}
    so that spread_pct(buy_usd, sell_usd) is the spread again.
    """
    half = spread / 100 / 2
    return {"buy_usd": round(mid * (1 + half), 5), "sell_usd": round(mid * (1 - half), 5)}


def fx_quotes_for(provider, fx):
    # A real quote that Member 2 fetched wins. Otherwise estimate from the mid.
    quotes = fx.get("quotes") or {}
    if provider in quotes:
        return quotes[provider]
    return quotes_from_mid(fx["mid"], FX_SPREAD_PCT.get(provider, DEFAULT_SPREAD_PCT))


def spread_used(provider, fx):
    quote = fx_quotes_for(provider, fx)
    return spread_pct(quote["buy_usd"], quote["sell_usd"])


# ---------------------------------------------------------------------------
# One route each. Every function returns money, never prints it.
# ---------------------------------------------------------------------------
def hkd_if_you_stayed(hkd, hkd_rate_pct, years, zero_interest_up_to=0.0):
    """HKD in, HKD out, no FX. zero_interest_up_to is for IB's cash tier:
    that first part of the balance earns nothing."""
    earning = hkd - zero_interest_up_to
    if earning < 0:
        earning = 0.0
    return round(hkd + simple_interest(earning, hkd_rate_pct, years), 2)


def usd_at_maturity(hkd, buy_usd, usd_rate_pct, years,
                    fee_in_usd=0.0, product_fee_usd=0.0, fee_out_usd=0.0,
                    zero_interest_up_to=0.0):
    """USD held at the end, after every fee, before changing back.

    1. change HKD into USD at buy_usd (you BUY USD at the higher price)
    2. pay the conversion fee and the product fee (e.g. the T-bill commission)
    3. earn simple interest; the first zero_interest_up_to earns nothing
    4. pay the fee for changing back
    The order of 1-4 matches the week 5 class table to the cent.
    """
    usd = hkd_to_usd(hkd, buy_usd) - fee_in_usd - product_fee_usd
    earning = usd - zero_interest_up_to
    if earning < 0:
        earning = 0.0
    usd = round(usd + simple_interest(earning, usd_rate_pct, years), 2)
    return round(usd - fee_out_usd, 2)


def breakeven_exit_rate(hkd_to_beat, usd_held):
    # The exit rate at which usd_held changes back into exactly hkd_to_beat.
    # Week 5: usd_held * exit_rate == what staying would have given you.
    return round(hkd_to_beat / usd_held, 4)


def band_verdict(usd_held, hkd_to_beat, spread):
    """Can the HKMA band make this USD route lose to hkd_to_beat?

    The band limits the MARKET (mid) rate. You change back at the provider's
    price below the mid, so the spread is applied at each edge too.
    """
    worst = usd_to_hkd(usd_held, quotes_from_mid(BAND_STRONG_SIDE, spread)["sell_usd"])
    best = usd_to_hkd(usd_held, quotes_from_mid(BAND_WEAK_SIDE, spread)["sell_usd"])
    if worst >= hkd_to_beat:
        verdict = "cannot lose inside the band"
    elif best < hkd_to_beat:
        verdict = "loses even at 7.85"
    else:
        verdict = "can lose inside the band"
    return {"hkd_if_exit_at_7.75": worst, "hkd_if_exit_at_7.85": best, "band_verdict": verdict}


# ---------------------------------------------------------------------------
# Choosing the right rate from Member 1's rows
# ---------------------------------------------------------------------------
def pick_rate(bank_rows, bank, currency, months, amount):
    """The deposit rate this bank pays on `amount` for `months`, or None.

    Tiers: a bank may list several rows for one tenor with different
    min_amount. The right one is the highest min_amount you still qualify
    for (week 5c: best so far). A row with no min_amount qualifies for any
    amount. If nothing fits, None - never 0, because 0% would be a real rate.
    """
    best_row = None
    for row in bank_rows:
        if row["bank"] != bank or row["currency"] != currency:
            continue
        if row["tenor_months"] != months:
            continue
        minimum = row["min_amount"]
        if minimum == None:
            minimum = 0.0
        if minimum > amount:
            continue                       # you do not qualify for this tier
        if best_row == None or minimum > best_row_minimum:
            best_row = row
            best_row_minimum = minimum
    if best_row == None:
        return None
    return best_row["rate_pct"]


def tbill_yield_for(tbill_yields, months):
    # JSON keys are always text: json.load turns {12: 3.47} into {"12": 3.47}.
    # Accept both, so a file Member 2 saved works the same as a dict in memory.
    yields = tbill_yields["yields_pct"]
    if months in yields:
        return yields[months]
    return yields.get(str(months))


def bank_names(bank_rows):
    # Every bank once, in the order Member 1 listed them.
    names = []
    for row in bank_rows:
        if row["bank"] not in names:
            names.append(row["bank"])
    return names


# ---------------------------------------------------------------------------
# Every route for one amount and one horizon
# ---------------------------------------------------------------------------
def build_options(principal, months, bank_rows, broker_rates, fx, tbill_yields):
    """One dictionary per route that has the numbers it needs.

    A route with a missing number (no tenor, no USD rate, no T-bill yield) is
    left out and printed - it is not given a made-up rate.
    """
    years = months / 12
    options = []

    # 1. Each bank: its HKD deposit, and the USD round trip through it.
    for bank in bank_names(bank_rows):
        hkd_rate = pick_rate(bank_rows, bank, "HKD", months, principal)
        if hkd_rate == None:
            print(f"{bank}: no HKD {months}-month rate for HK${principal:,.0f}, skipped")
        else:
            options.append(make_option(bank + " (HKD)", bank, "hkd_deposit", principal,
                                       hkd_if_you_stayed(principal, hkd_rate, years), hkd_rate))

        quote = fx_quotes_for(bank, fx)
        usd_amount = hkd_to_usd(principal, quote["buy_usd"])
        usd_rate = pick_rate(bank_rows, bank, "USD", months, usd_amount)
        if usd_rate == None:
            print(f"{bank}: no USD {months}-month rate for US${usd_amount:,.0f}, skipped")
        else:
            usd_end = usd_at_maturity(principal, quote["buy_usd"], usd_rate, years)
            options.append(make_option(bank + " (carry)", bank, "usd_deposit", principal,
                                       usd_to_hkd(usd_end, quote["sell_usd"]), usd_rate,
                                       usd_end, hkd_rate, years))

    # 2. The broker: HKD cash, USD cash, and a T-bill bought there.
    for broker in broker_rates:
        name = broker["provider"]
        quote = fx_quotes_for(name, fx)
        hkd_cash = broker.get("HKD")
        if hkd_cash != None:
            options.append(make_option(name + " (HKD)", name, "hkd_cash", principal,
                                       hkd_if_you_stayed(principal, hkd_cash["rate_pct"], years,
                                                         hkd_cash["zero_interest_up_to"]),
                                       hkd_cash["rate_pct"]))
        hkd_cash_rate = None
        if hkd_cash != None:
            hkd_cash_rate = hkd_cash["rate_pct"]

        usd_cash = broker.get("USD")
        if usd_cash != None:
            usd_end = usd_at_maturity(principal, quote["buy_usd"], usd_cash["rate_pct"], years,
                                      IB_FX_FEE_USD, 0.0, IB_FX_FEE_USD,
                                      usd_cash["zero_interest_up_to"])
            option = make_option(name + " (carry, cash)", name, "usd_cash", principal,
                                 usd_to_hkd(usd_end, quote["sell_usd"]), usd_cash["rate_pct"],
                                 usd_end, hkd_cash_rate, years, hkd_cash)
            options.append(option)

        tbill_rate = tbill_yield_for(tbill_yields, months)
        if tbill_rate == None:
            print(f"No {months}-month T-bill yield, T-bill skipped")
        else:
            usd_end = usd_at_maturity(principal, quote["buy_usd"], tbill_rate, years,
                                      IB_FX_FEE_USD, IB_TBILL_FEE_USD, IB_FX_FEE_USD)
            options.append(make_option(name + " (carry, T-bill)", name, "tbill", principal,
                                       usd_to_hkd(usd_end, quote["sell_usd"]), tbill_rate,
                                       usd_end, hkd_cash_rate, years, hkd_cash))
    return options


def make_option(option, provider, kind, principal, hkd_end, rate_pct,
                usd_end=None, same_provider_hkd_rate=None, years=None, hkd_cash=None):
    """One row of the answer. USD routes also keep usd_end, for the break-even,
    and the break-even against the SAME provider's HKD (the week 5 table)."""
    row = {"option": option, "provider": provider, "kind": kind,
           "currency": "HKD", "rate_pct": rate_pct,
           "hkd_end": hkd_end, "earned": round(hkd_end - principal, 2),
           "return_pct": round((hkd_end - principal) / principal * 100, 4),
           "fx_risk": usd_end != None, "usd_end": usd_end,
           "breakeven_same_provider": None}
    if usd_end != None:
        row["currency"] = "USD"
        if same_provider_hkd_rate != None:
            zero = 0.0
            if hkd_cash != None:
                zero = hkd_cash["zero_interest_up_to"]
            stay = hkd_if_you_stayed(principal, same_provider_hkd_rate, years, zero)
            row["breakeven_same_provider"] = breakeven_exit_rate(stay, usd_end)
    return row


def add_comparisons(options, fx):
    """Compare every route with the BEST HKD route - what you actually give up
    by taking FX risk - then add the break-even and the band check."""
    best_hkd = None                       # week 5c: best so far
    for row in options:
        if not row["fx_risk"]:
            if best_hkd == None or row["hkd_end"] > best_hkd["hkd_end"]:
                best_hkd = row
    for row in options:
        row["best_hkd_option"] = None
        row["vs_best_hkd"] = None
        row["breakeven_vs_best_hkd"] = None
        row["hkd_if_exit_at_7.75"] = None
        row["hkd_if_exit_at_7.85"] = None
        row["band_verdict"] = "no FX risk"
        if best_hkd == None:
            if row["fx_risk"]:
                row["band_verdict"] = "no HKD option to compare with"
            continue
        row["best_hkd_option"] = best_hkd["option"]
        row["vs_best_hkd"] = round(row["hkd_end"] - best_hkd["hkd_end"], 2)
        if row["fx_risk"]:
            row["breakeven_vs_best_hkd"] = breakeven_exit_rate(best_hkd["hkd_end"], row["usd_end"])
            band = band_verdict(row["usd_end"], best_hkd["hkd_end"], spread_used(row["provider"], fx))
            row.update(band)
    return options


# ---------------------------------------------------------------------------
# Reading Module 2 (Part 1 of this file, first written as fx_treasury.py)
#   get_latest_fx()     -> {"date": "2026-11-20", "base": "USD", "quote": "HKD", "rate": 7.7791}
#   get_latest_yields() -> {"date": "2026-11-20", "yields": {"1M": 3.73, "3M": 3.74, "1Y": 3.8, ...}}
# Both return None when the database has nothing yet.
# ---------------------------------------------------------------------------
MODULE2_TENOR_MONTHS = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12}


def fx_from_module2(latest_fx):
    """Module 2's latest FX row -> the fx dictionary compare_options uses.
    Frankfurter's rate is the ECB reference rate: a MID rate."""
    if latest_fx == None:
        return None
    if latest_fx.get("base") != "USD" or latest_fx.get("quote") != "HKD":
        print("Module 2 FX is not USD/HKD:", latest_fx)
        return None
    return {"pair": "USD/HKD", "mid": latest_fx["rate"], "date": latest_fx["date"],
            "source": "frankfurter (Module 2)"}


def tbill_from_module2(latest_yields):
    """Module 2's latest yields -> {months: yield_pct} for the four horizons.
    A tenor missing on that date (a blank in the Treasury file) is left out,
    so that horizon has no T-bill route rather than a made-up yield."""
    if latest_yields == None:
        return None
    yields = {}
    for tenor in MODULE2_TENOR_MONTHS:
        if tenor in latest_yields["yields"]:
            yields[MODULE2_TENOR_MONTHS[tenor]] = latest_yields["yields"][tenor]
    return {"date": latest_yields["date"], "source": "US Treasury (Module 2)",
            "yields_pct": yields}


def stale_data_warnings(fx, tbill_yields, today, max_age_days=5):
    """Module 2 keeps the OLD data when a source is down - correct, but the
    ranking must then say how old its numbers are. today: "YYYY-MM-DD".
    5 days allows for a weekend plus a holiday. Return a list of warnings."""
    warnings = []
    for name, data in [("FX rate", fx), ("T-bill yields", tbill_yields)]:
        if data == None or data.get("date") == None:
            continue
        age = (date.fromisoformat(today) - date.fromisoformat(data["date"][:10])).days
        if age > max_age_days:
            warnings.append(f"{name} is from {data['date']}, {age} days old - check Module 2's update")
    return warnings


# ---------------------------------------------------------------------------
# Check the inputs FIRST, so a teammate's format change is caught here with a
# clear message, not turned silently into a wrong ranking.
# ---------------------------------------------------------------------------
BANK_ROW_KEYS = ["bank", "currency", "tenor_label", "tenor_months", "rate_pct",
                 "min_amount", "conditions", "source_url", "source_type", "fetched_at"]


def check_inputs(principal, months, bank_rows, broker_rates, fx, tbill_yields):
    """Return a list of problems. An empty list means the inputs are usable."""
    problems = []
    if type(principal) not in [int, float] or principal <= 0:
        problems.append("principal must be a positive number")
    if months not in HORIZONS_MONTHS:
        problems.append(f"months must be one of {HORIZONS_MONTHS}, got {months}")

    for i in range(len(bank_rows)):
        row = bank_rows[i]
        for key in BANK_ROW_KEYS:
            if key not in row:
                problems.append(f"bank row {i}: missing '{key}'")
        if "currency" in row and row["currency"] not in ["HKD", "USD"]:
            problems.append(f"bank row {i}: currency must be HKD or USD, got {row['currency']}")
        if "rate_pct" in row:
            rate = row["rate_pct"]
            if type(rate) not in [int, float]:
                problems.append(f"bank row {i}: rate_pct must be a number like 4.2, got {rate!r}")
            elif rate < 0 or rate >= 20:
                problems.append(f"bank row {i}: rate_pct {rate} is not a believable percent")
        if "tenor_months" in row and type(row["tenor_months"]) not in [int, float]:
            problems.append(f"bank row {i}: tenor_months must be a number, got {row['tenor_months']!r}")
        if "min_amount" in row and row["min_amount"] != None and type(row["min_amount"]) not in [int, float]:
            # a CSV gives text: "" or "10000" must become None or 10000.0 when it is loaded
            problems.append(f"bank row {i}: min_amount must be a number or None, got {row['min_amount']!r}")

    for broker in broker_rates:
        for currency in ["HKD", "USD"]:
            part = broker.get(currency)
            if part != None and (type(part.get("rate_pct")) not in [int, float]
                                 or type(part.get("zero_interest_up_to")) not in [int, float]):
                problems.append(f"{broker.get('provider')} {currency}: needs rate_pct and zero_interest_up_to as numbers")

    if fx == None:
        problems.append("fx is missing: Module 2 has no FX data yet (run: python market_and_returns.py update)")
    elif type(fx.get("mid")) not in [int, float]:
        problems.append("fx['mid'] must be a number")
    elif fx["mid"] < 7.70 or fx["mid"] > 7.90:
        # 0.1275 means the pair is upside down (HKD/USD instead of USD/HKD)
        problems.append(f"fx['mid'] {fx['mid']} is not HKD per 1 USD near the 7.75-7.85 band")

    if tbill_yields == None:
        problems.append("tbill_yields is missing: Module 2 has no yields yet (run: python market_and_returns.py update)")
    elif type(tbill_yields.get("yields_pct")) != dict:
        problems.append("tbill_yields['yields_pct'] must be {months: yield_pct}")
    return problems


# ---------------------------------------------------------------------------
# The one function the dashboard (Member 4) and the email (Member 5) call
# ---------------------------------------------------------------------------
RANKING_COLUMNS = ["rank", "option", "provider", "kind", "currency", "rate_pct",
                   "hkd_end", "earned", "return_pct", "vs_best_hkd", "best_hkd_option",
                   "fx_risk", "breakeven_vs_best_hkd", "breakeven_same_provider",
                   "hkd_if_exit_at_7.75", "hkd_if_exit_at_7.85", "band_verdict",
                   "usd_end", "principal", "months", "fx_mid", "fx_date", "tbill_date"]


def rank_options(options):
    # pandas does the ranking, as in week 10: highest HK$ first, ties share a rank.
    table = pd.DataFrame(options)
    if len(table) == 0:
        return pd.DataFrame(columns=RANKING_COLUMNS)
    table = table.sort_values("hkd_end", ascending=False, kind="stable")
    table["rank"] = table["hkd_end"].rank(ascending=False, method="min").astype(int)
    return table.reset_index(drop=True)


def compare_options(principal, months, bank_rows, broker_rates, fx, tbill_yields):
    """The whole of Module 3 in one call.

    Return: a pandas DataFrame, one row per route, best first, with the
    columns in RANKING_COLUMNS - or None if the inputs have a problem.
    Every problem is printed, so a bad input is never silent.
    """
    problems = check_inputs(principal, months, bank_rows, broker_rates, fx, tbill_yields)
    if len(problems) > 0:
        print("compare_options cannot run:")
        for problem in problems:
            print("  -", problem)
        return None
    options = build_options(principal, months, bank_rows, broker_rates, fx, tbill_yields)
    options = add_comparisons(options, fx)
    table = rank_options(options)
    table["principal"] = principal
    table["months"] = months
    table["fx_mid"] = fx["mid"]
    table["fx_date"] = fx.get("date")
    table["tbill_date"] = tbill_yields.get("date")
    return table[RANKING_COLUMNS]


# =============================================================================
# PART 3 - THE ONE CALL FOR THE DASHBOARD (Member 4) AND THE EMAIL (Member 5)
# =============================================================================
#
#   table, warnings = get_ranking(100000, 12, bank_rows, broker_rates)
#
# bank_rows and broker_rates come from Member 1 (formats: INTERFACES.md).
# FX and T-bill yields are read from Module 2's database - run "update" first.
# table is a pandas DataFrame, best route first (columns: RANKING_COLUMNS),
# or None if an input is unusable - the reasons are printed.
# warnings is a list of text to show the user (e.g. "FX rate is 6 days old").

# Until Member 1's scraper is merged: the week 5 class rates (29 Sep 2026).
# They are SAMPLES - never show them to a user as today's rates.
SAMPLE_BANK_ROWS = [
    {"bank": "HSBC", "currency": "HKD", "tenor_label": "12 months", "tenor_months": 12.0,
     "rate_pct": 0.15, "min_amount": None, "conditions": "", "source_url": "week 5 class table",
     "source_type": "sample", "fetched_at": "2026-09-29T12:16:00"},
    {"bank": "HSBC", "currency": "USD", "tenor_label": "12 months", "tenor_months": 12.0,
     "rate_pct": 0.225, "min_amount": None, "conditions": "", "source_url": "week 5 class table",
     "source_type": "sample", "fetched_at": "2026-09-29T12:16:00"},
    {"bank": "eleBank", "currency": "HKD", "tenor_label": "12 months", "tenor_months": 12.0,
     "rate_pct": 3.4, "min_amount": None, "conditions": "", "source_url": "week 5 class table",
     "source_type": "sample", "fetched_at": "2026-09-29T12:16:00"},
    {"bank": "eleBank", "currency": "USD", "tenor_label": "12 months", "tenor_months": 12.0,
     "rate_pct": 4.2, "min_amount": None, "conditions": "", "source_url": "week 5 class table",
     "source_type": "sample", "fetched_at": "2026-09-29T12:16:00"}]
SAMPLE_BROKER_RATES = [
    {"provider": "IB",
     "HKD": {"rate_pct": 1.971, "zero_interest_up_to": 78000.0},
     "USD": {"rate_pct": 3.38, "zero_interest_up_to": 10000.0},
     "source_url": "week 5 class table", "fetched_at": "2026-09-29T12:20:00"}]


def get_market_inputs(db_path=DB_PATH):
    # Module 2's latest numbers, in the format Module 3 reads.
    fx = fx_from_module2(get_latest_fx(db_path))
    tbill_yields = tbill_from_module2(get_latest_yields(db_path))
    return fx, tbill_yields


def get_ranking(principal, months, bank_rows, broker_rates, today=None, db_path=DB_PATH):
    """Rank every route for one amount and horizon. Return (table, warnings)."""
    fx, tbill_yields = get_market_inputs(db_path)
    if today == None:
        today = date.today().isoformat()
    warnings = stale_data_warnings(fx, tbill_yields, today)
    for row in bank_rows:
        if row.get("source_type") == "sample":
            warnings.append("Bank rates are class SAMPLES, not today's rates - waiting for Module 1")
            break
    table = compare_options(principal, months, bank_rows, broker_rates, fx, tbill_yields)
    return table, warnings


def read_bank_csv(path):
    # Module 1's CSV -> bank_rows. A CSV holds only text: numbers become
    # floats and an empty min_amount becomes None (INTERFACES.md, section 1).
    import csv
    rows = []
    with open(path, newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            row["tenor_months"] = float(row["tenor_months"])
            row["rate_pct"] = float(row["rate_pct"])
            if row["min_amount"] == "":
                row["min_amount"] = None
            else:
                row["min_amount"] = float(row["min_amount"])
            rows.append(row)
    return rows


def read_json(path):
    import json
    with open(path, encoding="utf-8") as file:
        return json.load(file)


# =============================================================================
# PART 4 - SELF-TESTS. No internet: every input is written here.
#   python market_and_returns.py test
# Module 3 tests first, then Module 2's, then the two joined together.
# =============================================================================


def bank_row(bank, currency, rate_pct, tenor_months=12.0, min_amount=None):
    # One row in the format agreed with Member 1 (INTERFACES.md, section 1).
    return {"bank": bank, "currency": currency,
            "tenor_label": str(int(tenor_months)) + " months", "tenor_months": tenor_months,
            "rate_pct": rate_pct, "min_amount": min_amount, "conditions": "",
            "source_url": "saved copy", "source_type": "json",
            "fetched_at": "2026-09-29T12:16:00"}


# ---------------------------------------------------------------------------
# The week 5 class inputs (slide 2, 29 Sep 2026): HK$170,000 for 12 months
# ---------------------------------------------------------------------------
WEEK5_BANKS = [bank_row("HSBC", "HKD", 0.15), bank_row("HSBC", "USD", 0.225),
               bank_row("eleBank", "HKD", 3.4), bank_row("eleBank", "USD", 4.2)]
WEEK5_BROKER = [{"provider": "IB",
                 "HKD": {"rate_pct": 1.971, "zero_interest_up_to": 78000.0},
                 "USD": {"rate_pct": 3.38, "zero_interest_up_to": 10000.0},
                 "fetched_at": "2026-09-29T12:20:00"}]
WEEK5_FX = {"pair": "USD/HKD", "mid": 7.8448, "date": "2026-09-28", "source": "frankfurter",
            "quotes": {"eleBank": {"buy_usd": 7.85717, "sell_usd": 7.83243},
                       "HSBC": {"buy_usd": 7.86980, "sell_usd": 7.81980},
                       "IB": {"buy_usd": 7.84490, "sell_usd": 7.84470}}}
WEEK5_TBILL = {"date": "2026-09-25", "source": "FRED DGS1", "yields_pct": {12: 4.5}}


def test_week5_table():
    table = compare_options(170000, 12, WEEK5_BANKS, WEEK5_BROKER, WEEK5_FX, WEEK5_TBILL)
    assert list(table.columns) == RANKING_COLUMNS
    # Henry's order, 1 to 7
    assert list(table["option"]) == ["IB (carry, T-bill)", "eleBank (carry)", "eleBank (HKD)",
                                     "IB (carry, cash)", "IB (HKD)", "HSBC (HKD)", "HSBC (carry)"]
    assert list(table["rank"]) == [1, 2, 3, 4, 5, 6, 7]
    end = {}
    for i in range(len(table)):
        end[table["option"][i]] = table["hkd_end"][i]
    # Six amounts match Henry's table to the cent
    assert end["IB (carry, T-bill)"] == 177572.39
    assert end["eleBank (carry)"] == 176582.21
    assert end["eleBank (HKD)"] == 175780.00
    assert end["IB (HKD)"] == 171813.32
    assert end["HSBC (HKD)"] == 170255.00
    assert end["HSBC (carry)"] == 169300.00
    # The seventh: Henry shows 173,058.40. Ours is HK$0.32 lower (USD 0.04 of
    # interest) - a rounding step in his script we cannot see. Its break-even
    # below matches exactly, so the USD held is the same to within 4 decimals.
    assert abs(end["IB (carry, cash)"] - 173058.40) < 0.50
    # All four break-evens match Henry's "b-even" column exactly
    be = {}
    for i in range(len(table)):
        be[table["option"][i]] = table["breakeven_same_provider"][i]
    assert be["IB (carry, T-bill)"] == 7.5903
    assert be["eleBank (carry)"] == 7.7968
    assert be["IB (carry, cash)"] == 7.7883
    assert be["HSBC (carry)"] == 7.8639
    # "earned" and the gap to HSBC, as in his table
    assert table["earned"][0] == 7572.39
    assert round(end["IB (carry, T-bill)"] - end["HSBC (HKD)"], 2) == 7317.39


def test_week5_band():
    # Against the BEST HKD route (eleBank 3.4%), the T-bill can lose: its
    # break-even 7.7655 is inside the band. Henry's same-bank figure (7.5903)
    # hid this, because IB's own HKD rate is low.
    table = compare_options(170000, 12, WEEK5_BANKS, WEEK5_BROKER, WEEK5_FX, WEEK5_TBILL)
    row = table[table["option"] == "IB (carry, T-bill)"].iloc[0]
    assert row["best_hkd_option"] == "eleBank (HKD)"
    assert row["breakeven_vs_best_hkd"] == 7.7655
    assert row["band_verdict"] == "can lose inside the band"
    hsbc = table[table["option"] == "HSBC (carry)"].iloc[0]
    assert hsbc["band_verdict"] == "loses even at 7.85"
    stay = table[table["option"] == "eleBank (HKD)"].iloc[0]
    assert stay["band_verdict"] == "no FX risk" and stay["vs_best_hkd"] == 0.0


# ---------------------------------------------------------------------------
# The hand check for the presentation: HK$100,000, eleBank, 12 months
#   1. 100,000 / 7.85717           = 12,727.2288   -> US$12,727.23
#   2. 12,727.23 x 4.2%            =    534.5437   -> US$534.54
#   3. 12,727.23 + 534.54          = US$13,261.77
#   4. 13,261.77 x 7.83243         = 103,871.8852  -> HK$103,871.89
#   5. stay in HKD: 100,000 x 1.034 = HK$103,400.00, so the carry wins by 471.89
#   6. break-even: 103,400 / 13,261.77 = 7.7968
# (Done on a calculator first, then checked by the code - not the other way round.)
# ---------------------------------------------------------------------------
def test_hand_check_100k():
    usd = usd_at_maturity(100000, 7.85717, 4.2, 1)
    assert hkd_to_usd(100000, 7.85717) == 12727.23
    assert simple_interest(12727.23, 4.2, 1) == 534.54
    assert usd == 13261.77
    assert usd_to_hkd(usd, 7.83243) == 103871.89
    assert hkd_if_you_stayed(100000, 3.4, 1) == 103400.0
    assert breakeven_exit_rate(103400.0, usd) == 7.7968


# ---------------------------------------------------------------------------
# Each function, each branch
# ---------------------------------------------------------------------------
def test_horizons():
    # 3 months is a quarter of a year of simple interest
    assert hkd_if_you_stayed(100000, 3.4, 3 / 12) == 100850.0
    assert hkd_if_you_stayed(100000, 3.4, 1 / 12) == 100283.33
    assert hkd_if_you_stayed(100000, 0.0, 1) == 100000.0      # 0% is a real rate


def test_horizon_reaches_the_ranking():
    # The 3-month row is used, and interest is a quarter of a year's
    rows = [bank_row("A", "HKD", 3.4, 3.0), bank_row("A", "HKD", 9.0, 12.0)]
    table = compare_options(170000, 3, rows, [], {"mid": 7.8}, {"yields_pct": {}})
    assert table["hkd_end"][0] == 171445.0              # 170,000 x 3.4% x 3/12 = 1,445
    assert table["months"][0] == 3


def test_zero_interest_tier():
    assert hkd_if_you_stayed(100000, 1.971, 1, 78000) == 100433.62   # only 22,000 earns
    assert hkd_if_you_stayed(78000, 1.971, 1, 78000) == 78000.0      # boundary: nothing earns
    assert hkd_if_you_stayed(50000, 1.971, 1, 78000) == 50000.0      # below: still nothing, never negative
    assert usd_at_maturity(78000, 7.8, 3.38, 1, 0, 0, 0, 10000) == 10000.0


def test_fees_order():
    # IB: 2 in, T-bill 5, then interest, then 2 out (matches the week 5 table)
    assert usd_at_maturity(170000, 7.84490, 4.5, 1, 2.0, 5.0, 2.0) == 22635.97
    assert usd_at_maturity(170000, 7.84490, 4.5, 1) == 22645.29      # no fees: more


def test_quotes():
    q = quotes_from_mid(7.8448, 0.315)
    assert q == {"buy_usd": 7.85716, "sell_usd": 7.83244}           # Henry's: 7.85717 / 7.83243
    assert q["buy_usd"] > 7.8448 > q["sell_usd"]                     # you buy high, sell low
    assert spread_pct(7.85717, 7.83243) == 0.3154
    assert quotes_from_mid(7.8448, 0) == {"buy_usd": 7.8448, "sell_usd": 7.8448}
    # a fetched quote wins over the estimate; an unknown bank gets the widest spread
    assert fx_quotes_for("HSBC", WEEK5_FX) == {"buy_usd": 7.86980, "sell_usd": 7.81980}
    assert fx_quotes_for("ZA Bank", {"mid": 7.8}) == quotes_from_mid(7.8, 0.637)


def test_band_verdict_three_branches():
    # usd_held 13,261.77 vs HK$103,400, spread 0:  7.75 -> 102,778.72, 7.85 -> 104,104.89
    assert band_verdict(13261.77, 103400.0, 0)["band_verdict"] == "can lose inside the band"
    assert band_verdict(13261.77, 102000.0, 0)["band_verdict"] == "cannot lose inside the band"
    assert band_verdict(13261.77, 104200.0, 0)["band_verdict"] == "loses even at 7.85"
    # with eleBank's 0.315% spread you change back BELOW the band edge:
    # 7.75 -> sell 7.73779, 7.85 -> sell 7.83764 (worse than the mid at each edge)
    band = band_verdict(13261.77, 103400.0, 0.315)
    assert band["hkd_if_exit_at_7.75"] == 102616.79
    assert band["hkd_if_exit_at_7.85"] == 103940.98
    # boundary: exactly equal at 7.75 counts as cannot lose (you are not behind)
    assert band_verdict(10000.0, 77500.0, 0)["band_verdict"] == "cannot lose inside the band"


def test_pick_rate_tiers_and_missing():
    rows = [bank_row("A", "HKD", 2.0, 12.0, None),        # any amount
            bank_row("A", "HKD", 2.5, 12.0, 100000.0),    # HK$100,000 and above
            bank_row("A", "HKD", 3.0, 12.0, 500000.0),    # HK$500,000 and above
            bank_row("A", "HKD", 1.0, 3.0, None),
            bank_row("A", "USD", 4.0, 12.0, 20000.0)]
    assert pick_rate(rows, "A", "HKD", 12, 50000) == 2.0
    assert pick_rate(rows, "A", "HKD", 12, 100000) == 2.5          # boundary: qualifies
    assert pick_rate(rows, "A", "HKD", 12, 99999.99) == 2.0
    assert pick_rate(rows, "A", "HKD", 12, 600000) == 3.0
    assert pick_rate(rows, "A", "HKD", 3, 100000) == 1.0
    assert pick_rate(rows, "A", "HKD", 6, 100000) == None          # no 6-month tenor
    assert pick_rate(rows, "A", "USD", 12, 12727.23) == None       # below the USD minimum
    assert pick_rate(rows, "B", "HKD", 12, 100000) == None         # no such bank
    assert pick_rate([], "A", "HKD", 12, 100000) == None


def test_tbill_keys_from_json():
    # json.load turns the key 12 into "12": both must work
    assert tbill_yield_for({"yields_pct": {12: 4.5}}, 12) == 4.5
    assert tbill_yield_for({"yields_pct": {"12": 4.5}}, 12) == 4.5
    assert tbill_yield_for({"yields_pct": {"3": 4.1}}, 12) == None


def test_missing_routes_are_skipped_not_zero():
    # A bank with only an HKD rate gives one route, not a carry at 0%
    rows = [bank_row("A", "HKD", 2.0)]
    options = build_options(100000, 12, rows, [], {"mid": 7.8}, {"yields_pct": {}})
    names = []
    for row in options:
        names.append(row["option"])
    assert names == ["A (HKD)"]
    # no T-bill yield for this horizon: the T-bill route is left out
    options = build_options(100000, 6, [], WEEK5_BROKER, WEEK5_FX, WEEK5_TBILL)
    names = []
    for row in options:
        names.append(row["option"])
    assert names == ["IB (HKD)", "IB (carry, cash)"]


def test_ties_share_a_rank():
    rows = [bank_row("A", "HKD", 2.0), bank_row("B", "HKD", 2.0), bank_row("C", "HKD", 1.0)]
    table = compare_options(100000, 12, rows, [], {"mid": 7.8}, {"yields_pct": {}})
    assert list(table["rank"]) == [1, 1, 3]
    assert list(table["option"]) == ["A (HKD)", "B (HKD)", "C (HKD)"]   # input order kept


def test_no_routes():
    table = compare_options(100000, 12, [], [], {"mid": 7.8}, {"yields_pct": {}})
    assert len(table) == 0 and list(table.columns) == RANKING_COLUMNS
    # only USD routes: nothing to compare with, said plainly
    rows = [bank_row("A", "USD", 4.0)]
    table = compare_options(100000, 12, rows, [], {"mid": 7.8}, {"yields_pct": {}})
    assert table["band_verdict"][0] == "no HKD option to compare with"


# ---------------------------------------------------------------------------
# The integration checks: what goes wrong when two modules disagree
# ---------------------------------------------------------------------------
def test_check_inputs_catches_format_mistakes():
    good = check_inputs(100000, 12, WEEK5_BANKS, WEEK5_BROKER, WEEK5_FX, WEEK5_TBILL)
    assert good == []
    bad_row = bank_row("A", "HKD", "4.20%")                  # text, not a number
    problems = check_inputs(100000, 12, [bad_row], [], WEEK5_FX, WEEK5_TBILL)
    assert len(problems) == 1 and "rate_pct must be a number" in problems[0]
    bad_row = bank_row("A", "HKD", 420.0)                    # 4.2 typed as basis points
    assert "not a believable percent" in check_inputs(100000, 12, [bad_row], [], WEEK5_FX, WEEK5_TBILL)[0]
    bad_row = bank_row("A", "hkd", 4.2)                      # lower case
    assert "currency must be HKD or USD" in check_inputs(100000, 12, [bad_row], [], WEEK5_FX, WEEK5_TBILL)[0]
    bad_row = bank_row("A", "HKD", 4.2, "12")                # tenor as text, from a CSV
    assert "tenor_months must be a number" in check_inputs(100000, 12, [bad_row], [], WEEK5_FX, WEEK5_TBILL)[0]
    bad_row = bank_row("A", "HKD", 4.2, 12.0, "")            # min_amount as text, from a CSV
    assert "min_amount must be a number or None" in check_inputs(100000, 12, [bad_row], [], WEEK5_FX, WEEK5_TBILL)[0]
    bad_row = bank_row("A", "HKD", 4.2)
    del bad_row["fetched_at"]
    assert "missing 'fetched_at'" in check_inputs(100000, 12, [bad_row], [], WEEK5_FX, WEEK5_TBILL)[0]
    # the FX pair upside down: 1 HKD = 0.1275 USD
    assert "not HKD per 1 USD" in check_inputs(100000, 12, [], [], {"mid": 0.1275}, WEEK5_TBILL)[0]
    assert "months must be one of" in check_inputs(100000, 2, [], [], WEEK5_FX, WEEK5_TBILL)[0]
    assert "principal" in check_inputs(-5, 12, [], [], WEEK5_FX, WEEK5_TBILL)[0]
    # a bad input gives None and a printed reason, never a wrong ranking
    assert compare_options(100000, 12, [], [], {"mid": 0.1275}, WEEK5_TBILL) is None


# ---------------------------------------------------------------------------
# Module 2 (fx_treasury.py): its REAL output, copied from a run on 22 Nov 2026
# ---------------------------------------------------------------------------
M2_FX = {"date": "2026-11-20", "base": "USD", "quote": "HKD", "rate": 7.7791}
M2_YIELDS = {"date": "2026-11-20", "yields": {"10Y": 4.39, "1M": 3.73, "1Y": 3.8, "20Y": 4.97,
             "2M": 3.72, "2Y": 3.88, "30Y": 4.99, "3M": 3.74, "3Y": 3.9, "4M": 3.73,
             "5Y": 4.01, "6M": 3.79, "7Y": 4.2}}


def test_module2_adapters():
    assert fx_from_module2(M2_FX) == {"pair": "USD/HKD", "mid": 7.7791, "date": "2026-11-20",
                                      "source": "frankfurter (Module 2)"}
    assert tbill_from_module2(M2_YIELDS)["yields_pct"] == {1: 3.73, 3: 3.74, 6: 3.79, 12: 3.8}
    # a tenor blank on the latest date is left out, not set to 0
    partial = {"date": "2026-11-18", "yields": {"1M": 3.74, "3M": 3.75}}
    assert tbill_from_module2(partial)["yields_pct"] == {1: 3.74, 3: 3.75}
    # Module 2 returns None on an empty database: passed on, then reported
    assert fx_from_module2(None) == None and tbill_from_module2(None) == None
    assert fx_from_module2({"date": "2026-11-20", "base": "HKD", "quote": "USD", "rate": 0.1286}) == None
    problems = check_inputs(100000, 12, [], [], None, None)
    assert "fx is missing" in problems[0] and "tbill_yields is missing" in problems[1]
    assert len(problems) == 2                                       # each problem said once
    assert compare_options(100000, 12, [], [], None, None) is None


def test_module2_end_to_end():
    # Module 2's real output through the whole engine: 12 months, HK$100,000
    table = compare_options(100000, 12, WEEK5_BANKS, WEEK5_BROKER,
                            fx_from_module2(M2_FX), tbill_from_module2(M2_YIELDS))
    row = table[table["option"] == "IB (carry, T-bill)"].iloc[0]
    # by hand: IB buy = 7.7791 x 1.000015 = 7.77922; 100,000 / 7.77922 = 12,854.76
    #   - 2 - 5 = 12,847.76;  x 3.8% = 488.21;  13,335.97 - 2 = 13,333.97
    #   IB sell = 7.7791 x 0.999985 = 7.77898;  13,333.97 x 7.77898 = 103,724.69
    assert row["usd_end"] == 13333.97
    assert row["hkd_end"] == 103724.69
    assert row["fx_date"] == "2026-11-20" and row["tbill_date"] == "2026-11-20"


def test_stale_data_warnings():
    fx = fx_from_module2(M2_FX)
    tb = tbill_from_module2(M2_YIELDS)
    assert stale_data_warnings(fx, tb, "2026-11-22") == []               # a weekend: fine
    assert stale_data_warnings(fx, tb, "2026-11-25") == []               # 5 days: still fine
    warnings = stale_data_warnings(fx, tb, "2026-11-26")                 # 6 days: warn
    assert len(warnings) == 2 and "6 days old" in warnings[0]
    assert stale_data_warnings(None, None, "2026-11-26") == []           # missing is check_inputs' job


M3_TESTS = [test_week5_table, test_week5_band, test_hand_check_100k, test_horizons, test_horizon_reaches_the_ranking,
         test_zero_interest_tier, test_fees_order, test_quotes,
         test_band_verdict_three_branches, test_pick_rate_tiers_and_missing,
         test_tbill_keys_from_json, test_missing_routes_are_skipped_not_zero,
         test_ties_share_a_rank, test_no_routes, test_check_inputs_catches_format_mistakes,
         test_module2_adapters, test_module2_end_to_end, test_stale_data_warnings]



# -----------------------------------------------------------------------------
# Module 2 tests: saved answers in the real formats, a throw-away database
# -----------------------------------------------------------------------------
# Frankfurter v1 answers: /latest gives one day, a date range gives one per day.
SAVED_FX_LATEST = {"amount": 1.0, "base": "USD", "date": "2026-11-20", "rates": {"HKD": 7.7791}}
SAVED_FX_SERIES = {"amount": 1.0, "base": "USD", "start_date": "2026-11-16", "end_date": "2026-11-20",
                   "rates": {"2026-11-16": {"HKD": 7.7770}, "2026-11-17": {"HKD": 7.7775},
                             "2026-11-18": {"HKD": 7.7780}, "2026-11-19": {"HKD": 7.7785},
                             "2026-11-20": {"HKD": 7.7791}}}
# Treasury CSV: US-style dates, quoted headers, a "1.5 Month" column we do not
# use, and two blank cells (missing yields must be dropped, never stored as 0).
SAVED_TREASURY_CSV = (
    'Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo","6 Mo","1 Yr","2 Yr","3 Yr","5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"\n'
    '11/20/2026,3.73,3.71,3.72,3.74,3.73,3.79,3.80,3.88,3.90,4.01,4.20,4.39,4.97,4.99\n'
    '11/19/2026,3.74,3.72,3.73,3.75,3.74,3.80,3.81,3.89,3.91,4.02,4.21,4.40,4.98,5.00\n'
    '11/18/2026,3.74,3.72,,3.75,,3.80,3.81,3.89,3.91,4.02,4.21,4.40,4.98,5.00\n')
TEST_DB = Path("data/_selftest.db")


class SavedResponse:
    # Stands in for a requests response: text, status, .json(), raise_for_status().
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def json(self):
        import json
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def run_with_saved_answers(fx_ok, treasury_ok, function, *arguments, fx_hosts_ok=None):
    """Run function with requests.get and time.sleep replaced.
    fx_hosts_ok: which Frankfurter hosts answer (default: all if fx_ok).
    Return: (result, list of URLs asked)."""
    import json
    asked = []

    def fake_get(url, params=None, timeout=None, headers=None):
        asked.append(url)
        if "frankfurter" in url:
            host_ok = fx_ok
            if fx_hosts_ok != None:
                host_ok = False
                for host in fx_hosts_ok:
                    if url.startswith(host):
                        host_ok = True
            if not host_ok:
                raise requests.ConnectionError("Frankfurter is down")
            if url.endswith("/latest"):
                return SavedResponse(json.dumps(SAVED_FX_LATEST))
            return SavedResponse(json.dumps(SAVED_FX_SERIES))
        if "treasury.gov" in url:
            if not treasury_ok:
                return SavedResponse("Service Unavailable", 503)
            return SavedResponse(SAVED_TREASURY_CSV)
        raise AssertionError("unexpected URL " + url)

    real_get, real_sleep, was_disabled = requests.get, time.sleep, log.disabled
    requests.get = fake_get
    time.sleep = lambda seconds: None
    log.disabled = True                  # the failures here are on purpose: keep output quiet
    try:
        result = function(*arguments)
    finally:
        requests.get, time.sleep, log.disabled = real_get, real_sleep, was_disabled
    return result, asked


def fresh_test_db():
    if TEST_DB.exists():
        TEST_DB.unlink()
    return TEST_DB


def test_m2_parse_frankfurter():
    latest = parse_frankfurter(SAVED_FX_LATEST)
    assert latest.to_dict("records") == [{"date": "2026-11-20", "base": "USD", "quote": "HKD", "rate": 7.7791}]
    series = parse_frankfurter(SAVED_FX_SERIES)
    assert len(series) == 5 and list(series["date"])[0] == "2026-11-16"
    # a day without HKD, and a zero rate, are both dropped
    odd = {"base": "USD", "rates": {"2026-11-16": {"EUR": 0.95}, "2026-11-17": {"HKD": 0},
                                    "2026-11-18": {"HKD": 7.778}}}
    assert list(parse_frankfurter(odd)["date"]) == ["2026-11-18"]
    try:
        parse_frankfurter({"message": "not found"})
        assert False, "a payload without rates must raise"
    except ValueError:
        pass


def test_m2_parse_treasury_csv():
    long = parse_treasury_csv(SAVED_TREASURY_CSV)
    assert len(long) == 3 * 13 - 2                  # 13 known tenors x 3 days, minus 2 blanks
    assert "1.5M" not in list(long["tenor"])        # the unused column is ignored
    assert sorted(set(long["date"])) == ["2026-11-18", "2026-11-19", "2026-11-20"]   # ISO dates
    day = long[long["date"] == "2026-11-18"]
    assert "2M" not in list(day["tenor"]) and "4M" not in list(day["tenor"])        # blanks dropped
    one_year = long[(long["date"] == "2026-11-20") & (long["tenor"] == "1Y")]
    assert list(one_year["yield_pct"]) == [3.8]
    try:
        parse_treasury_csv("<html>Service Unavailable</html>")
        assert False, "an HTML page must raise"
    except ValueError:
        pass


def test_m2_fresh_database_returns_none():
    # The bug Member 3 fixed: these crashed with "no such table".
    db = fresh_test_db()
    assert get_latest_fx(db) == None
    assert get_latest_yields(db) == None
    assert len(get_fx_history(db_path=db)) == 0
    table, warnings = get_ranking(100000, 12, SAMPLE_BANK_ROWS, SAMPLE_BROKER_RATES, "2026-11-22", db)
    assert table is None                            # said clearly, no crash
    db.unlink()


def test_m2_update_no_duplicates():
    db = fresh_test_db()
    run_with_saved_answers(True, True, update, db)
    run_with_saved_answers(True, True, update, db)  # twice: still one row per date
    with get_conn(db) as conn:
        fx_rows = conn.execute("SELECT COUNT(*), COUNT(DISTINCT date) FROM fx_rates").fetchone()
        yield_rows = conn.execute("SELECT COUNT(*) FROM treasury_yields").fetchone()[0]
    assert tuple(fx_rows) == (5, 5)
    assert yield_rows == 37
    assert get_latest_fx(db) == {"date": "2026-11-20", "base": "USD", "quote": "HKD", "rate": 7.7791}
    assert get_latest_yields(db)["yields"]["1Y"] == 3.8
    db.unlink()


def test_m2_keeps_old_data_when_sources_fail():
    db = fresh_test_db()
    run_with_saved_answers(True, True, update, db)
    result, asked = run_with_saved_answers(False, False, update, db)
    # 2 Frankfurter hosts x 3 attempts, plus 3 Treasury attempts
    assert len(asked) == 9
    assert get_latest_fx(db)["rate"] == 7.7791      # yesterday's data is still there
    with get_conn(db) as conn:
        statuses = conn.execute("SELECT dataset, status FROM update_log ORDER BY rowid").fetchall()
    assert [tuple(row) for row in statuses] == [("fx", "ok"), ("treasury", "ok"),
                                                ("fx", "failed"), ("treasury", "failed")]
    db.unlink()


def test_m2_frankfurter_mirror():
    # the first host is down, the mirror answers
    result, asked = run_with_saved_answers(True, True, fetch_fx, fx_hosts_ok=[FRANKFURTER_HOSTS[1]])
    assert list(result["rate"]) == [7.7791]
    assert asked[-1].startswith(FRANKFURTER_HOSTS[1])


def test_modules_joined():
    # Module 2's database -> Module 3's ranking, the way Members 4 and 5 call it
    db = fresh_test_db()
    run_with_saved_answers(True, True, update, db)
    table, warnings = get_ranking(100000, 12, SAMPLE_BANK_ROWS, SAMPLE_BROKER_RATES, "2026-11-22", db)
    tbill = table[table["option"] == "IB (carry, T-bill)"].iloc[0]
    assert tbill["hkd_end"] == 103724.69            # the hand check in test_module2_end_to_end
    assert tbill["fx_mid"] == 7.7791 and tbill["fx_date"] == "2026-11-20"
    assert warnings == ["Bank rates are class SAMPLES, not today's rates - waiting for Module 1"]
    # ten days later with no update: the ranking says its numbers are old
    table, warnings = get_ranking(100000, 12, SAMPLE_BANK_ROWS, SAMPLE_BROKER_RATES, "2026-11-30", db)
    assert len(warnings) == 3 and "10 days old" in warnings[0]
    db.unlink()


M2_TESTS = [test_m2_parse_frankfurter, test_m2_parse_treasury_csv,
            test_m2_fresh_database_returns_none, test_m2_update_no_duplicates,
            test_m2_keeps_old_data_when_sources_fail, test_m2_frankfurter_mirror,
            test_modules_joined]


def run_self_tests():
    for test in M3_TESTS + M2_TESTS:
        test()
        print("passed:", test.__name__)
    print("All", len(M3_TESTS + M2_TESTS), "tests passed.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="FX & Treasury data module")
    sub = p.add_subparsers(dest="cmd", required=False)
    sub.add_parser("init")
    b = sub.add_parser("backfill"); b.add_argument("--years", type=int, default=5)
    sub.add_parser("update")
    sub.add_parser("latest")
    sub.add_parser("export")
    # Added by Member 3: rank today's options, and run the self-tests
    r = sub.add_parser("rank")
    r.add_argument("--principal", type=float, default=100000.0)
    r.add_argument("--months", type=int, default=12, choices=HORIZONS_MONTHS)
    r.add_argument("--banks", help="Module 1's bank CSV (default: class samples)")
    r.add_argument("--broker", help="Module 1's broker JSON (default: class samples)")
    sub.add_parser("test")
    args = p.parse_args()

    # No subcommand (e.g. `python market_and_returns.py`) -> refresh data and print latest.
    if args.cmd is None:
        update()
        print("FX    :", get_latest_fx())
        print("Yields:", get_latest_yields())
        return

    if args.cmd == "init":
        init_db()
    elif args.cmd == "backfill":
        backfill(args.years)
    elif args.cmd == "update":
        update()
    elif args.cmd == "latest":
        print("FX    :", get_latest_fx())
        print("Yields:", get_latest_yields())
    elif args.cmd == "export":
        export_csv()
    elif args.cmd == "rank":
        bank_rows = SAMPLE_BANK_ROWS
        broker_rates = SAMPLE_BROKER_RATES
        if args.banks:
            bank_rows = read_bank_csv(args.banks)
        if args.broker:
            broker_rates = read_json(args.broker)
        table, warnings = get_ranking(args.principal, args.months, bank_rows, broker_rates)
        for warning in warnings:
            print("WARNING:", warning)
        if table is not None:
            print(table[["rank", "option", "hkd_end", "earned", "breakeven_vs_best_hkd",
                         "band_verdict"]].to_string(index=False))
    elif args.cmd == "test":
        run_self_tests()


if __name__ == "__main__":
    main()
