"""Member 4 dashboard: show Member 3's ranking. Do not recalculate returns.

Bank rows come from Member 1's bank_data.py at runtime.
Member 1 did not supply IB broker rates, so broker_rates is always [].
Member 5's rankings_history.csv is not used. The charts read Member 2's
FX and Treasury database (get_daily_dataset).
"""

from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

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
from market_and_returns import get_daily_dataset, get_market_inputs, get_ranking, update

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "market_data.db"
BROKER_RATES = []

SOURCES = [
    ("eleBank", fetch_elebank, parse_elebank),
    ("HSBC", fetch_hsbc, parse_hsbc),
    ("Standard Chartered", fetch_sc, parse_sc),
    ("BEA", fetch_bea, parse_bea),
    ("Mox", fetch_mox, parse_mox),
]

DISPLAY_COLUMNS = [
    "rank", "option", "provider", "currency", "rate_pct",
    "hkd_end", "earned", "return_pct",
    "breakeven_vs_best_hkd", "band_verdict",
]
COLUMN_NAMES = {
    "rank": "Rank",
    "option": "Investment option",
    "provider": "Provider",
    "currency": "Currency",
    "rate_pct": "Rate (% p.a.)",
    "hkd_end": "Final value (HKD)",
    "earned": "Net gain (HKD)",
    "return_pct": "Return (%)",
    "breakeven_vs_best_hkd": "FX break-even vs best HKD",
    "band_verdict": "HKMA band assessment",
}


@st.cache_data(ttl=30 * 60, show_spinner=False)
def load_bank_rows():
    """Same bank load Member 5 uses in live_ranking.py. Cached for 30 minutes."""
    return fetch_bank_rows()


def market_chart(daily):
    """USD/HKD reference rate from Member 2. HKMA band is drawn as two lines."""
    fx = daily.dropna(subset=["usd_hkd"])
    fig = px.line(fx, x="date", y="usd_hkd", markers=True, title="USD/HKD reference rate")
    fig.add_hline(y=7.75, line_dash="dash", line_color="gray")
    fig.add_hline(y=7.85, line_dash="dash", line_color="gray")
    fig.update_layout(
        xaxis_title="Date",
        yaxis_title="HKD per 1 USD",
        hovermode="x unified",
    )
    return fig


def yield_chart(daily):
    """T-bill yields from Member 2, for the four horizons the ranking uses."""
    tenors = [name for name in ["1M", "3M", "6M", "1Y"] if name in daily.columns]
    long = daily.melt(
        id_vars="date", value_vars=tenors, var_name="tenor", value_name="yield_pct"
    ).dropna(subset=["yield_pct"])
    fig = px.line(
        long, x="date", y="yield_pct", color="tenor", markers=True,
        title="US Treasury bill yields",
    )
    fig.update_layout(
        xaxis_title="Date",
        yaxis_title="Yield (% p.a.)",
        hovermode="x unified",
        legend_title_text="Tenor",
    )
    return fig


st.set_page_config(page_title="Where Should HK$100k Sit?", page_icon="💰", layout="wide")
st.title("Where Should HK$100k Sit This Month?")

st.sidebar.header("Investment settings")
principal = st.sidebar.number_input(
    "Investment amount (HKD)", min_value=1000.0, max_value=100_000_000.0,
    value=100_000.0, step=1000.0,
)
months = st.sidebar.selectbox("Investment period (months)", [1, 3, 6, 12], index=3)
currency_filter = st.sidebar.selectbox("Currency filter", ["All", "HKD", "USD"])

st.sidebar.divider()
if st.sidebar.button("Refresh bank rates"):
    load_bank_rows.clear()
    st.rerun()
if st.sidebar.button("Update FX and Treasury data"):
    with st.spinner("Downloading the latest FX rate and Treasury yields..."):
        update(DB_PATH)
    st.rerun()

with st.spinner("Loading bank deposit rates from Member 1..."):
    bank_rows, failed_banks = load_bank_rows()

for name in failed_banks:
    st.warning(f"Could not load rates for {name}.")

if not bank_rows:
    st.error("No bank rows were loaded. Check the network, then press Refresh bank rates.")
    st.stop()

fx, tbill_yields = get_market_inputs(DB_PATH)
if fx is None or tbill_yields is None:
    st.error(
        "FX or Treasury data is not in the database yet. "
        "Press Update FX and Treasury data in the sidebar. That step needs the internet."
    )
    st.stop()

table, warnings = get_ranking(principal, months, bank_rows, BROKER_RATES, db_path=DB_PATH)
for warning in warnings:
    st.warning(warning)

if table is None:
    st.error(
        "Member 3's get_ranking() returned no table. "
        "The reason is printed in the terminal that is running Streamlit."
    )
    st.stop()

if table.empty:
    st.warning("No deposit route matches this amount and period.")
    st.stop()

visible = table if currency_filter == "All" else table[table["currency"] == currency_filter]

st.subheader("At a glance")
best = table.iloc[0]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Investment amount", f"HK${principal:,.2f}")
c2.metric("Investment period", f"{months} months")
c3.metric("Best option", str(best["option"]))
c4.metric("Expected net gain", f"HK${float(best['earned']):,.2f}")
st.caption(
    f"FX reference date: {best['fx_date']}  |  Treasury data date: {best['tbill_date']}  "
    f"|  {len(bank_rows)} bank rows loaded"
)

st.divider()
st.subheader("Investment ranking")
st.caption("Filtering by currency only hides rows. It does not recalculate the ranking.")

if visible.empty:
    st.info(f"No {currency_filter} routes in this ranking.")
else:
    show = visible[[c for c in DISPLAY_COLUMNS if c in visible.columns]].rename(columns=COLUMN_NAMES)
    st.dataframe(
        show,
        width="stretch",
        hide_index=True,
        column_config={
            "Rate (% p.a.)": st.column_config.NumberColumn(format="%.3f"),
            "Final value (HKD)": st.column_config.NumberColumn(format="%.2f"),
            "Net gain (HKD)": st.column_config.NumberColumn(format="%.2f"),
            "Return (%)": st.column_config.NumberColumn(format="%.3f"),
            "FX break-even vs best HKD": st.column_config.NumberColumn(format="%.4f"),
        },
    )

st.divider()
st.subheader("FX break-even and HKMA band")
usd_routes = table[table["fx_risk"] == True]
if usd_routes.empty:
    st.info("No USD deposit routes are available for these settings.")
else:
    risk_cols = [
        c for c in [
            "option", "fx_mid", "breakeven_vs_best_hkd", "breakeven_same_provider",
            "hkd_if_exit_at_7.75", "hkd_if_exit_at_7.85", "band_verdict",
        ] if c in usd_routes.columns
    ]
    risk = usd_routes[risk_cols].rename(columns={
        "option": "USD investment",
        "fx_mid": "Current FX (HKD per USD)",
        "breakeven_vs_best_hkd": "Break-even vs best HKD",
        "breakeven_same_provider": "Break-even vs same provider",
        "hkd_if_exit_at_7.75": "Final HKD at 7.75",
        "hkd_if_exit_at_7.85": "Final HKD at 7.85",
        "band_verdict": "HKMA band assessment",
    })
    st.dataframe(risk, width="stretch", hide_index=True)
st.caption("The HKMA band is HK$7.75–7.85 per USD. The assessment is Member 3's calculation.")

st.divider()
st.subheader("FX and Treasury history")
st.caption("From Member 2's database. This is not a history of the ranking.")
daily = get_daily_dataset(db_path=DB_PATH)
if daily.empty or "usd_hkd" not in daily.columns or daily["usd_hkd"].dropna().empty:
    st.info("No FX history yet. Press Update FX and Treasury data.")
else:
    left, right = st.columns(2)
    with left:
        st.plotly_chart(market_chart(daily), width="stretch")
    yield_tenors = [name for name in ["1M", "3M", "6M", "1Y"] if name in daily.columns]
    with right:
        if yield_tenors and daily[yield_tenors].notna().any().any():
            st.plotly_chart(yield_chart(daily), width="stretch")
        else:
            st.info("No Treasury bill yields in the database yet.")
