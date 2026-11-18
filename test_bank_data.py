from bank_data import *

KEYS = ["bank", "currency", "product", "rate_type", "tenor_label", "tenor_months", "rate_pct",
        "min_amount", "max_amount", "conditions", "source_url", "source_type", "fetched_at", "as_of"]
TS = "2026-11-17T16:00:00"


def check_rows(rows, bank, source_type):       # every row of every bank must look the same
    for r in rows:
        assert list(r.keys()) == KEYS, r
        assert r["bank"] == bank and r["source_type"] == source_type and r["fetched_at"] == TS
        assert r["currency"] in ["HKD", "USD"] and r["product"] == "time deposit"
        assert r["rate_type"] in ["standard", "promotional"]
        assert type(r["rate_pct"]) == float and r["rate_pct"] >= 0
        assert r["tenor_months"] != None and r["tenor_months"] > 0


def pick(rows, **want):                        # rows that match every key=value given
    found = []
    for r in rows:
        ok = True
        for key in want:
            if r[key] != want[key]:
                ok = False
        if ok:
            found.append(r)
    return found


# ---------- helpers ----------
assert rate_to_float("4.20%") == 4.2
assert rate_to_float("4%") == 4.0
assert rate_to_float("0.001%") == 0.001
assert rate_to_float("3.380%(BM -0.5%)") == 3.38        # IB style text
assert rate_to_float("N/A") == None
assert rate_to_float("-") == None
assert rate_to_float("") == None

assert plain_float("2.90") == 2.9
assert plain_float("") == None and plain_float("-") == None

assert tenor_to_months("12 months") == 12.0
assert tenor_to_months("12-month") == 12.0               # Mox
assert tenor_to_months("3 Months") == 3.0                # BEA
assert tenor_to_months("1 week") == 0.25
assert tenor_to_months("2 Weeks") == 0.5                 # Hang Seng writes capitals
assert tenor_to_months("1.5 Years") == 18.0
assert tenor_to_months("1 day") == 0.0333
assert tenor_to_months("7 days") == 0.2333
assert tenor_to_months("Any") == None
assert tenor_to_months("") == None
assert tenor_to_months("Tenor") == None
assert tenor_to_months("Note: x") == None

assert amount_from("HKD10,000") == 10000.0
assert amount_from("HK$ 30,000") == 30000.0
assert amount_from("USD1,000,000 or above") == 1000000.0
assert amount_from("(In original currency of 1,000 or above)") == 1000.0
assert amount_from("Deposit period") == None

assert parse_band("HKD10,000 to HKD99,999") == [10000.0, 99999.0]
assert parse_band("HK$10,000 to HK$29,999") == [10000.0, 29999.0]
assert parse_band("USD2,000 to USD49,999") == [2000.0, 49999.0]
assert parse_band("HKD1,000,000 or above") == [1000000.0, None]
assert parse_band("HK$5,000,000 or above") == [5000000.0, None]
assert parse_band("Below 100,000") == [0.0, 99999.0]
assert parse_band("100,000 to 499,999") == [100000.0, 499999.0]
assert parse_band("500,000 or above") == [500000.0, None]
assert parse_band("something odd") == [None, None]

assert currency_from_heading("Hong Kong dollar (HKD)") == "HKD"
assert currency_from_heading("Time Deposit Rates (interest rate per annum)") == None
assert currency_from_heading("Useful links") == None

row = make_row("X", "HKD", "time deposit", "standard", "12 months", 1.5, None, None, "", "u", "json", TS, None)
assert list(row.keys()) == KEYS and row["tenor_months"] == 12.0

# ---------- eleBank: the saved copy from week 5 (29 Sep 2026), shortened ----------
ELEBANK = [
 {'rate_type': 'Saving Interest Rate', 'currency': {'code': 'HKD', 'en': 'HKD'},
  'tiers': [{'en': 'First HKD50,000', 'rate': '0.50%'}]},
 {'rate_type': 'Time Deposit Rate', 'currency': {'code': 'HKD', 'en': 'HKD'},
  'tenors': [{'en': '1 week', 'rate': '0.01%'}, {'en': '1 month', 'rate': '0.50%'},
             {'en': '2 months', 'rate': '2.15%'}, {'en': '3 months', 'rate': '2.60%'},
             {'en': '4 months', 'rate': '2.80%'}, {'en': '6 months', 'rate': '3.10%'},
             {'en': '9 months', 'rate': '3.30%'}, {'en': '12 months', 'rate': '3.40%'}]},
 {'rate_type': 'Time Deposit Rate', 'currency': {'code': 'CNY', 'en': 'CNY'},
  'tenors': [{'en': '1 week', 'rate': '0.01%'}]},
 {'rate_type': 'Time Deposit Rate', 'currency': {'code': 'USD', 'en': 'USD'},
  'tenors': [{'en': '1 week', 'rate': '0.05%'}, {'en': '1 month', 'rate': '3.00%'},
             {'en': '2 months', 'rate': '3.30%'}, {'en': '3 months', 'rate': '3.60%'},
             {'en': '4 months', 'rate': '3.70%'}, {'en': '6 months', 'rate': '3.80%'},
             {'en': '9 months', 'rate': '4.00%'}, {'en': '12 months', 'rate': '4.20%'}]},
 {'rate_type': 'Update Time', 'update_time': '2026-09-29T12:16:00.476Z'}]     # no "currency" key

rows = parse_elebank(ELEBANK, TS)
assert len(rows) == 16                                   # 8 HKD + 8 USD; no savings, no CNY
check_rows(rows, "eleBank", "json")
usd12 = pick(rows, currency="USD", tenor_label="12 months")
assert len(usd12) == 1 and usd12[0]["rate_pct"] == 4.2 and usd12[0]["tenor_months"] == 12.0
assert usd12[0]["min_amount"] == None and usd12[0]["max_amount"] == None
assert usd12[0]["as_of"] == "2026-09-29T12:16:00.476Z" and usd12[0]["rate_type"] == "standard"
assert pick(rows, currency="HKD", tenor_label="1 week")[0]["tenor_months"] == 0.25
assert parse_elebank([], TS) == []
assert parse_elebank([{'rate_type': 'Update Time', 'update_time': 'x'}], TS) == []
assert parse_elebank([{'rate_type': 'Time Deposit Rate', 'currency': {'code': 'USD'},
                       'tenors': [{'en': '1 week', 'rate': 'N/A'}]}], TS) == []   # blank rate skipped

# ---------- HSBC: an INVENTED page laid out like the real one's text ----------
HSBC_HTML = """
<h1>Deposit Rate</h1>
<h2>Hong Kong dollar (HKD)</h2>
<h3>Savings</h3>
<table><tr><th>Account Balance</th><th>Interest Rate (p.a.)</th></tr>
<tr><td>Less than HKD5,000</td><td>0.000%</td></tr></table>
<h3>Time Deposit</h3>
<table>
<tr><th>Deposit Period</th><th>HKD10,000<br>to<br>HKD99,999</th><th>HKD100,000<br>to<br>HKD499,999</th>
<th>HKD500,000<br>to<br>HKD999,999</th><th>HKD1,000,000<br>or above</th></tr>
<tr><td>1 day</td><td>-</td><td>-</td><td>-</td><td>0.001%</td></tr>
<tr><td>1 week</td><td>0.100%</td><td>0.100%</td><td>0.100%</td><td>0.100%</td></tr>
<tr><td>3 months</td><td>0.125%</td><td>0.125%</td><td>0.125%</td><td>0.125%</td></tr>
<tr><td>12 months</td><td>0.150%</td><td>0.150%</td><td>0.150%</td><td>0.150%</td></tr>
</table>
<h3>Time Deposit</h3>
<table>
<tr><th>Deposit Period</th><th>1 week</th></tr>
<tr><td>HKD10,000<br>to<br>HKD99,999</td><td>0.100%</td></tr>
<tr><td>HKD100,000<br>to<br>HKD499,999</td><td>0.100%</td></tr>
<tr><td></td><td></td></tr>
<tr><th>Deposit Period</th><th>12 months</th></tr>
<tr><td>HKD10,000<br>to<br>HKD99,999</td><td>0.150%</td></tr>
</table>
<h2>Renminbi (CNY)</h2>
<h3>Time Deposit</h3>
<table><tr><th>Deposit Period</th><th>CNY10,000<br>to<br>CNY499,999</th><th>CNY500,000<br>or above</th></tr>
<tr><td>1 week</td><td>0.250%</td><td>0.250%</td></tr></table>
<h2>United States dollar (USD)</h2>
<h3>Time Deposit</h3>
<table>
<tr><th>Deposit Period</th><th>USD2,000<br>to<br>USD49,999</th><th>USD50,000<br>to<br>USD499,999</th>
<th>USD500,000<br>to<br>USD999,999</th><th>USD1,000,000<br>or above</th></tr>
<tr><td>1 month</td><td>0.125%</td><td>0.125%</td><td>0.125%</td><td>0.125%</td></tr>
<tr><td>12 months</td><td>0.225%</td><td>0.225%</td><td>0.225%</td><td>0.225%</td></tr>
</table>
<h2>Useful links</h2>
<table><tr><th>a</th><th>b</th><th>c</th></tr><tr><td>1</td><td>2</td><td>3</td></tr></table>
"""
rows = parse_hsbc(HSBC_HTML, TS)
hkd = pick(rows, currency="HKD")
usd = pick(rows, currency="USD")
assert len(hkd) == 1 + 4 + 4 + 4                         # 1 day (one band only) + 3 tenors x 4 bands
assert len(usd) == 8                                     # 2 tenors x 4 bands
assert len(rows) == len(hkd) + len(usd)                  # no CNY, no savings, no stacked copy
check_rows(rows, "HSBC", "html")
band = pick(hkd, tenor_label="12 months", min_amount=100000.0)
assert len(band) == 1 and band[0]["max_amount"] == 499999.0 and band[0]["rate_pct"] == 0.15
assert band[0]["tenor_months"] == 12.0 and band[0]["rate_type"] == "standard" and band[0]["as_of"] == None
day = pick(hkd, tenor_label="1 day")
assert len(day) == 1 and day[0]["rate_pct"] == 0.001 and day[0]["min_amount"] == 1000000.0
assert day[0]["max_amount"] == None and day[0]["tenor_months"] == 0.0333
u = pick(usd, tenor_label="12 months", min_amount=2000.0)
assert u[0]["rate_pct"] == 0.225 and u[0]["max_amount"] == 49999.0
assert parse_hsbc("", TS) == []
assert parse_hsbc("<html><body>no tables here</body></html>", TS) == []

# ---------- Standard Chartered: an INVENTED page laid out like the real one's text ----------
SC_HTML = """
<h1>Board Rates</h1><p>As at 5 June 2026</p>
<h3>Hong Kong Dollar Deposit Rates (HKD)</h3>
<p><b>Savings Deposit Rates (interest rate per annum)</b></p>
<table><tr><th>Deposit Amount</th><th>Annual Interest Rate</th></tr><tr><td>HK$1 - HK$149,999</td><td>0.001%</td></tr></table>
<h4>Time Deposit Rates (interest rate per annum)</h4>
<table>
<tr><th>Deposit period</th><th>HK$10,000 to<br>HK$29,999</th><th>HK$100,000 to<br>HK$499,999</th><th>HK$5,000,000<br>or above</th></tr>
<tr><td>7 days</td><td>0.100%</td><td>0.100%</td><td>0.100%</td></tr>
<tr><td>1 month</td><td>0.100%</td><td>0.100%</td><td>0.100%</td></tr>
<tr><td>3 months</td><td>0.125%</td><td>0.125%</td><td>0.125%</td></tr>
<tr><td>12 months</td><td>0.150%</td><td>0.150%</td><td>0.150%</td></tr>
<tr><td colspan="4">Note:<br>The time deposit interest rate is valid on the date as specified only.</td></tr>
</table>
<h3>US Dollar Deposit Rates (USD)</h3>
<table><tr><th>Deposit Amount</th><th>Annual Interest Rate</th></tr><tr><td>USD1 - USD99,999</td><td>0.125%</td></tr></table>
<table>
<tr><th>Deposit period</th><th>USD2,000 to<br>USD49,999</th><th>USD50,000 to<br>USD499,999</th><th>USD500,000 to<br>USD999,999</th><th>USD1,000,000 or<br>above</th></tr>
<tr><td>7 days</td><td>0.50%</td><td>0.50%</td><td>0.50%</td><td>0.50%</td></tr>
<tr><td>12 months</td><td>0.60%</td><td>0.60%</td><td>0.60%</td><td>0.60%</td></tr>
<tr><td>Note:<br>valid only on the date</td><td></td><td></td><td></td><td></td></tr>
</table>
<h3>Renminbi Deposit Rates (CNY)</h3>
<table><tr><th>Deposit period</th><th>CNY10,000 to<br>CNY49,999</th><th>CNY50,000<br>or above</th></tr>
<tr><td>7 days</td><td>0.25%</td><td>0.25%</td></tr></table>
"""
rows = parse_sc(SC_HTML, TS)
hkd = pick(rows, currency="HKD")
usd = pick(rows, currency="USD")
assert len(hkd) == 4 * 3 and len(usd) == 2 * 4           # no savings, no CNY, no note rows
assert len(rows) == 20
check_rows(rows, "Standard Chartered", "html")
for r in rows:
    assert r["rate_type"] == "standard" and r["as_of"] == "5 June 2026"
d7 = pick(hkd, tenor_label="7 days", min_amount=10000.0)
assert len(d7) == 1 and d7[0]["max_amount"] == 29999.0 and d7[0]["rate_pct"] == 0.1
assert d7[0]["tenor_months"] == 0.2333
top = pick(hkd, tenor_label="12 months", min_amount=5000000.0)
assert top[0]["max_amount"] == None and top[0]["rate_pct"] == 0.15
u12 = pick(usd, tenor_label="12 months", min_amount=1000000.0)
assert u12[0]["rate_pct"] == 0.6 and u12[0]["max_amount"] == None
assert parse_sc("", TS) == []
assert pick(parse_sc(SC_HTML.replace("As at 5 June 2026", "no date"), TS), currency="USD")[0]["as_of"] == None

# ---------- BEA: an INVENTED page laid out like the real one's text ----------
BEA_HTML = """
<h1>Preferential Time Deposits Interest Rate</h1>
<p><strong>Online HKD Time Deposit Preferential Rates (% p.a.)</strong></p>
<table>
<tr><th>Tenor</th><th>SupremeGold Private</th><th>SupremeGold</th><th>Supreme</th><th>BEA GOAL<br>or<br>Other Personal All-in-one Account</th></tr>
<tr><td></td><td colspan="3">(In original currency of 10,000 or above)</td><td>(In original currency of 1,000 or above)</td></tr>
<tr><td></td><td colspan="4">New Fund / Existing Fund*</td></tr>
<tr><td>3 Months</td><td>3.00 / 2.90</td><td>2.95 / 2.90</td><td>2.90 / 2.85</td><td>2.90 / 2.85</td></tr>
<tr><td>6 Months</td><td>3.05 / 2.95</td><td>3.00 / 2.95</td><td>2.95 / 2.90</td><td>2.95 / 2.90</td></tr>
<tr><td>12 Months</td><td>3.10 / 3.00</td><td>3.05 / 3.00</td><td>3.00 / 2.95</td><td>3.00</td></tr>
</table>
<p><strong>Online USD Time Deposit Preferential Rates (% p.a.)</strong></p>
<table>
<tr><th>Tenor</th><th>SupremeGold Private</th><th>SupremeGold</th><th>Supreme</th><th>BEA GOAL<br>or<br>Other Personal All-in-one Account</th></tr>
<tr><td></td><td colspan="4">(In original currency of 1,000 or above)</td></tr>
<tr><td></td><td colspan="4">New Fund / Existing Fund*</td></tr>
<tr><td>3 Months</td><td>3.85 / 3.75</td><td>3.80 / 3.75</td><td>3.75 / 3.70</td><td>3.75 / 3.70</td></tr>
<tr><td>12 Months</td><td>3.95 / 3.85</td><td>3.90 / 3.85</td><td>3.85 / 3.80</td><td>3.85 / 3.80</td></tr>
</table>
<p><strong>Online Other Foreign Currencies Time Deposit Preferential Rates (% p.a.)</strong></p>
<table><tr><th>Tenor</th><th>x</th></tr><tr><td>AUD</td><td>1</td></tr></table>
<p>These interest rates are quoted as of 13 Nov 2026. They are for reference only and not guaranteed.</p>
"""
rows = parse_bea(BEA_HTML, TS)
hkd = pick(rows, currency="HKD")
usd = pick(rows, currency="USD")
assert len(hkd) == 3 * 2 - 1                             # 12 months has only one number: no "existing fund" row
assert len(usd) == 2 * 2
check_rows(rows, "BEA", "html")
for r in rows:
    assert r["rate_type"] == "promotional" and r["as_of"] == "13 Nov 2026" and r["max_amount"] == None
    assert "BEA GOAL" in r["conditions"]
h3 = pick(hkd, tenor_label="3 Months")
assert len(h3) == 2 and h3[0]["rate_pct"] == 2.9 and "new fund" in h3[0]["conditions"]
assert h3[1]["rate_pct"] == 2.85 and "existing fund" in h3[1]["conditions"]
assert h3[0]["min_amount"] == 1000.0                     # the last cell of the amount row, not the 10,000 one
u12 = pick(usd, tenor_label="12 Months")
assert u12[0]["rate_pct"] == 3.85 and u12[1]["rate_pct"] == 3.8 and u12[0]["min_amount"] == 1000.0
assert pick(hkd, tenor_label="12 Months")[0]["rate_pct"] == 3.0
assert parse_bea("", TS) == []
# the same page with the title INSIDE the table (as the first row) must give the same answer
inside = BEA_HTML.replace('<p><strong>Online HKD Time Deposit Preferential Rates (% p.a.)</strong></p>\n<table>',
                          '<table><tr><td colspan="5">Online HKD Time Deposit Preferential Rates (% p.a.)</td></tr>')
inside_rows = parse_bea(inside, TS)
assert len(pick(inside_rows, currency="HKD")) == 5 and len(pick(inside_rows, currency="USD")) == 4

# ---------- Mox: an INVENTED page laid out like the real one's text ----------
MOX_HTML = """
<h4>The flexibility to match your needs</h4>
<p>Place a Time Deposit with New Funds to enjoy the following competitive interest rates.</p>
<ul>
<li>USD New Funds Time Deposit</li>
</ul>
<table><tr><th>Tenor</th><th>Preferential interest rate (p.a.)</th></tr>
<tr><td>1-month</td><td>3.35%</td></tr><tr><td>3-month</td><td>3.9%</td></tr>
<tr><td>6-month</td><td>4%</td></tr><tr><td>12-month</td><td>4%</td></tr></table>
<p>HKD New Funds Time Deposit</p>
<table><tr><th>Tenor</th><th>Preferential interest rate (p.a.)</th></tr>
<tr><td>3-month</td><td>2.8%</td></tr><tr><td>24-month</td><td>2.8%</td></tr></table>
<h4>Limited time offer: up to 15% p.a. on USD Time Deposit</h4>
<table><tr><th>Tenor (USD)</th><th>Time Deposit Rate (p.a.)</th></tr>
<tr><td>7-days</td><td>15%</td></tr><tr><td>30-days</td><td>6.8%</td></tr></table>
"""
rows = parse_mox(MOX_HTML, TS)
assert len(pick(rows, currency="USD")) == 4 and len(pick(rows, currency="HKD")) == 2
assert len(rows) == 6                                    # the limited-time 15% table is left out
check_rows(rows, "Mox", "html")
for r in rows:
    assert r["rate_type"] == "promotional" and "new funds" in r["conditions"] and r["as_of"] == None
assert pick(rows, currency="USD", tenor_label="3-month")[0]["rate_pct"] == 3.9
assert pick(rows, currency="USD", tenor_label="6-month")[0]["rate_pct"] == 4.0
assert pick(rows, currency="HKD", tenor_label="24-month")[0]["tenor_months"] == 24.0
assert parse_mox("", TS) == []
assert parse_mox("<p>USD New Funds Time Deposit</p>", TS) == []        # label but no table

print("All bank_data tests passed.")
