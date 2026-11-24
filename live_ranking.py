"""The combined call for Member 5. Do not recalculate returns.

Wires the three finished parts together:

    Member 1  bank_data.py          -> bank_rows
    Member 2  market_and_returns.py -> FX and T-bill database
    Member 3  market_and_returns.py -> get_ranking()

Member 1 did not supply IB cash rates, so broker_rates is [].

    from live_ranking import rank_live

    table, warnings = rank_live(100000, 12)

table is a pandas DataFrame, best route first, or None if an input is unusable.
warnings is a list of strings. Append table yourself if you keep daily history;
add a run_date column. This file does not write rankings_history.csv.

Command line, from this folder:

    python live_ranking.py
    python live_ranking.py --principal 100000 --months 12
    python live_ranking.py --update
"""

from pathlib import Path

from bank_data import (
    fetch_bea,
    fetch_elebank,
    fetch_hsbc,
    fetch_mox,
    fetch_sc,
    now_text,
    parse_bea,
    parse_elebank,
    parse_hsbc,
    parse_mox,
    parse_sc,
)
from market_and_returns import get_market_inputs, get_ranking, update

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "market_data.db"
BROKER_RATES = []
HORIZONS = [1, 3, 6, 12]

SOURCES = [
    ("eleBank", fetch_elebank, parse_elebank),
    ("HSBC", fetch_hsbc, parse_hsbc),
    ("Standard Chartered", fetch_sc, parse_sc),
    ("BEA", fetch_bea, parse_bea),
    ("Mox", fetch_mox, parse_mox),
]


def load_bank_rows():
    """Call Member 1 for all five banks. Return (rows, failed bank names)."""
    fetched_at = now_text()
    rows = []
    failed = []
    for name, fetch, parse in SOURCES:
        try:
            raw = fetch()
            if raw is None:
                failed.append(name)
                continue
            parsed = parse(raw, fetched_at)
            if not parsed:
                failed.append(name + " (page loaded, but no HKD/USD rows)")
                continue
            rows.extend(parsed)
        except Exception as exc:
            failed.append(name + " (" + str(exc) + ")")
    return rows, failed


def rank_live(principal=100000, months=12, refresh_market=False):
    """Rank today's bank routes. Return (table, warnings).

    refresh_market=True downloads the latest FX rate and Treasury yields first.
    """
    if months not in HORIZONS:
        raise ValueError("months must be 1, 3, 6 or 12")
    if refresh_market:
        update(DB_PATH)
    bank_rows, failed = load_bank_rows()
    table, warnings = get_ranking(
        principal, months, bank_rows, BROKER_RATES, db_path=DB_PATH
    )
    for name in failed:
        warnings.append("Could not load rates for " + name)
    return table, warnings


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Rank live bank routes for Member 5")
    parser.add_argument("--principal", type=float, default=100000.0)
    parser.add_argument("--months", type=int, default=12, choices=HORIZONS)
    parser.add_argument("--update", action="store_true", help="refresh FX and Treasury data first")
    args = parser.parse_args()

    fx, tbill = get_market_inputs(DB_PATH)
    if args.update or fx is None or tbill is None:
        update(DB_PATH)

    table, warnings = rank_live(args.principal, args.months)
    for warning in warnings:
        print("WARNING:", warning)
    if table is None:
        print("No ranking. See the messages above.")
        return
    columns = ["rank", "option", "provider", "currency", "rate_pct", "hkd_end", "earned", "band_verdict"]
    print(table[columns].to_string(index=False))
    print("rows:", len(table), "| banks loaded into the ranking, not IB")


if __name__ == "__main__":
    main()
