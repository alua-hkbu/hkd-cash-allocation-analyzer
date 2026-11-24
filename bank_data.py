# bank_data.py - Part 1: bank deposit data collection
#
# Every row is a dict with the same 14 keys, in this order (see make_row):
#   bank, currency, product, rate_type, tenor_label, tenor_months, rate_pct,
#   min_amount, max_amount, conditions, source_url, source_type, fetched_at, as_of
#
# rate_type: "standard"    = the board rate anyone can get, no conditions
#            "promotional" = any rate with a condition (new funds, online only, customer segment)

import time
import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0"}
WANTED_CURRENCIES = ["HKD", "USD"]

ELEBANK_URL = "https://www.elebank.com/vendor/json/deposit_rate.json"
HSBC_URL = "https://www.hsbc.com.hk/investments/market-information/hk/deposit-rate/"
SC_URL = "https://www.sc.com/hk/deposits/board-rates/"
BEA_URL = "https://www.hkbea.com/html/en/bea-personal-banking-supremegold-time-deposit.html"
MOX_URL = "https://mox.com/promotions/time-deposit/"


# ---------- shared helpers ----------

def now_text():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def rate_to_float(text):               # "4.20%" -> 4.2 ; "N/A", "-", "" -> None
    parts = text.split("%")
    if len(parts) < 2 or parts[0] == "":
        return None
    return float(parts[0])


def plain_float(text):                 # "2.90" -> 2.9 ; "" or "-" -> None
    try:
        return float(text)
    except ValueError:
        return None


def tenor_to_months(label):            # "12 months" -> 12.0 ; "3-month" -> 3.0 ; "1 week" -> 0.25
    parts = label.replace("-", " ").split(" ")
    if len(parts) != 2:
        return None
    try:
        number = float(parts[0])
    except ValueError:
        return None
    unit = parts[1].lower()
    if unit.startswith("week"):
        return round(number * 0.25, 4)
    if unit.startswith("month"):
        return round(number, 4)
    if unit.startswith("year"):
        return round(number * 12, 4)
    if unit.startswith("day"):
        return round(number / 30, 4)
    return None


def amount_from(text):                 # "HKD10,000" or "HK$ 10,000" -> 10000.0 ; "5,000,000 or above" -> 5000000.0
    text = text.split(" or ")[0]
    digits = ""
    for ch in text:
        if ch in "0123456789.":
            digits = digits + ch
    if digits == "":
        return None
    return float(digits)


def parse_band(text):                  # "HKD10,000 to HKD99,999" -> [10000.0, 99999.0]
    lowered = text.lower()             # "HKD1,000,000 or above"  -> [1000000.0, None]
    if lowered.startswith("below") or lowered.startswith("less than"):
        top = amount_from(text)        # "Below 100,000"          -> [0.0, 99999.0]
        if top == None:
            return [None, None]
        return [0.0, top - 1]
    pieces = text.split(" to ")
    low = amount_from(pieces[0])
    high = None
    if len(pieces) == 2:
        high = amount_from(pieces[1])
    return [low, high]


def currency_from_heading(text):       # "Hong Kong dollar (HKD)" -> "HKD" ; "Rates (per annum)" -> None
    if "(" not in text or ")" not in text:
        return None
    code = text.split("(")[1].split(")")[0]
    if len(code) == 3 and code == code.upper():
        return code
    return None


def find_text_with(soup, part):        # the first piece of page text that contains `part`
    for piece in soup.find_all(string=True):
        if part in piece:
            return piece
    return None


def table_after(label_piece):          # the table the label sits in, or the next table after it
    table = label_piece.find_parent("table")
    if table == None:
        table = label_piece.find_next("table")
    return table


def make_row(bank, currency, product, rate_type, tenor_label, rate_pct,
             min_amount, max_amount, conditions, source_url, source_type, fetched_at, as_of):
    return {"bank": bank, "currency": currency, "product": product, "rate_type": rate_type,
            "tenor_label": tenor_label, "tenor_months": tenor_to_months(tenor_label),
            "rate_pct": rate_pct, "min_amount": min_amount, "max_amount": max_amount,
            "conditions": conditions, "source_url": source_url, "source_type": source_type,
            "fetched_at": fetched_at, "as_of": as_of}


def fetch_json(url):                   # Week 6 retry pattern; None if all 3 tries fail
    for attempt in [1, 2, 3]:
        try:
            response = requests.get(url, headers=HEADERS, timeout=30)
            if response.status_code == 200:
                return response.json()
            print("attempt", attempt, "got", response.status_code)
        except (requests.exceptions.RequestException, ValueError) as error:
            print("attempt", attempt, "failed:", error)
        time.sleep(2 * attempt)
    return None


def fetch_html(url):
    for attempt in [1, 2, 3]:
        try:
            response = requests.get(url, headers=HEADERS, timeout=30)
            if response.status_code == 200:
                return response.text
            print("attempt", attempt, "got", response.status_code)
        except requests.exceptions.RequestException as error:
            print("attempt", attempt, "failed:", error)
        time.sleep(2 * attempt)
    return None


def rows_from_wide_table(table, bank, currency, rate_type, conditions, source_url,
                         fetched_at, as_of, first_header):
    # A "wide" table: first row = [period, band 1, band 2, ...]; later rows = [tenor, rate, rate, ...]
    # first_header: if given, the table is used only when its first header cell says this (lower case)
    rows = []
    table_rows = table.find_all("tr")
    if len(table_rows) < 2:
        return rows
    header = table_rows[0].find_all(["th", "td"])
    if len(header) < 3:                # a stacked copy of the table has only 2 columns: skip it
        return rows
    if first_header != None and header[0].get_text(" ", strip=True).lower() != first_header:
        return rows
    bands = []
    for cell in header[1:]:
        bands.append(parse_band(cell.get_text(" ", strip=True)))
    for tr in table_rows[1:]:
        cells = tr.find_all(["th", "td"])
        if len(cells) == len(header):
            tenor = cells[0].get_text(" ", strip=True)
            if tenor.startswith("Note"):
                continue
            for i in range(1, len(cells)):
                rate = rate_to_float(cells[i].get_text(strip=True))
                if rate != None:
                    rows.append(make_row(bank, currency, "time deposit", rate_type, tenor, rate,
                                         bands[i - 1][0], bands[i - 1][1], conditions,
                                         source_url, "html", fetched_at, as_of))
    return rows


# ---------- eleBank (JSON) ----------

def fetch_elebank():
    return fetch_json(ELEBANK_URL)


def parse_elebank(data, fetched_at):
    as_of = None
    for item in data:
        if item["rate_type"] == "Update Time":
            as_of = item["update_time"]
    rows = []
    for item in data:
        if item["rate_type"] == "Time Deposit Rate" and item["currency"]["code"] in WANTED_CURRENCIES:
            for t in item["tenors"]:
                rate = rate_to_float(t["rate"])
                if rate != None:
                    rows.append(make_row("eleBank", item["currency"]["code"], "time deposit",
                                         "standard", t["en"], rate, None, None,
                                         "for reference only", ELEBANK_URL, "json", fetched_at, as_of))
    return rows


# ---------- HSBC (HTML tables) ----------

def fetch_hsbc():
    return fetch_html(HSBC_URL)


def parse_hsbc(html, fetched_at):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    current_currency = None            # remember the last heading, as in week 5's ib_rate
    current_section = None
    for element in soup.find_all(["h2", "h3", "table"]):
        if element.name == "h2":
            current_section = None
            current_currency = currency_from_heading(element.get_text(" ", strip=True))
        elif element.name == "h3":
            current_section = element.get_text(" ", strip=True)
        elif current_currency in WANTED_CURRENCIES:
            rows = rows + rows_from_wide_table(element, "HSBC", current_currency, "standard",
                                               "for indication only", HSBC_URL, fetched_at, None,
                                               "deposit period")
    return rows


# ---------- Standard Chartered (HTML tables, board rates) ----------

def fetch_sc():
    return fetch_html(SC_URL)


def parse_sc(html, fetched_at):
    soup = BeautifulSoup(html, "html.parser")
    as_of = None
    piece = find_text_with(soup, "As at ")
    if piece != None:
        as_of = str(piece).split("As at ")[1].strip()
    rows = []
    current_currency = None
    for element in soup.find_all(["h2", "h3", "h4", "table"]):
        if element.name != "table":
            code = currency_from_heading(element.get_text(" ", strip=True))
            if code != None:
                current_currency = code
        elif current_currency in WANTED_CURRENCIES:
            rows = rows + rows_from_wide_table(element, "Standard Chartered", current_currency,
                                               "standard", "board rate; for reference only", SC_URL,
                                               fetched_at, as_of, "deposit period")
    return rows


# ---------- BEA (HTML tables, online preferential rates) ----------

BEA_SEGMENT = "Other Personal"         # the broadest customer group: "BEA GOAL or Other Personal All-in-one Account"


def two_rates_from(text):              # "2.90 / 2.85" -> [2.9, 2.85]   (new fund, existing fund)
    parts = text.split("/")
    if len(parts) == 2:
        return [plain_float(parts[0].strip()), plain_float(parts[1].strip())]
    return [plain_float(text.strip()), None]


def rows_from_bea_table(table, currency, fetched_at, as_of):
    rows = []
    table_rows = table.find_all("tr")
    header = None                      # the first row that names our customer group
    first_data = 0
    for n in range(len(table_rows)):
        cells = table_rows[n].find_all(["th", "td"])
        if header == None:
            for cell in cells:
                if BEA_SEGMENT in cell.get_text(" ", strip=True):
                    header = cells
                    first_data = n + 1
    if header == None:
        return rows
    column = 0
    for i in range(len(header)):
        if BEA_SEGMENT in header[i].get_text(" ", strip=True):
            column = i
    min_amount = None                  # the row that says "(In original currency of 1,000 or above)"
    for tr in table_rows[first_data:]:
        cells = tr.find_all(["th", "td"])
        if len(cells) > 0 and "In original currency of" in cells[-1].get_text(" ", strip=True):
            min_amount = amount_from(cells[-1].get_text(" ", strip=True))
    for tr in table_rows[first_data:]:
        cells = tr.find_all(["th", "td"])
        if len(cells) == len(header):
            tenor = cells[0].get_text(" ", strip=True)
            if tenor_to_months(tenor) != None:
                two = two_rates_from(cells[column].get_text(" ", strip=True))
                kinds = ["new fund", "existing fund"]
                for k in range(2):
                    if two[k] != None:
                        conditions = ("BEA GOAL / other all-in-one account; " + kinds[k] +
                                      "; placed online (BEA Online or Mobile)")
                        rows.append(make_row("BEA", currency, "time deposit", "promotional", tenor,
                                             two[k], min_amount, None, conditions, BEA_URL,
                                             "html", fetched_at, as_of))
    return rows


def fetch_bea():
    return fetch_html(BEA_URL)


def parse_bea(html, fetched_at):
    soup = BeautifulSoup(html, "html.parser")
    as_of = None
    piece = find_text_with(soup, "quoted as of ")
    if piece != None:
        as_of = str(piece).split("quoted as of ")[1].split(".")[0].strip()
    rows = []
    for currency in WANTED_CURRENCIES:
        label = find_text_with(soup, currency + " Time Deposit Preferential Rates")
        if label != None:
            table = table_after(label)
            if table != None:
                rows = rows + rows_from_bea_table(table, currency, fetched_at, as_of)
    return rows


# ---------- Mox (HTML tables, new-funds offer) ----------

def fetch_mox():
    return fetch_html(MOX_URL)


def parse_mox(html, fetched_at):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for currency in WANTED_CURRENCIES:
        label = find_text_with(soup, currency + " New Funds Time Deposit")
        if label != None:
            table = table_after(label)
            if table != None:
                for tr in table.find_all("tr"):
                    cells = tr.find_all(["th", "td"])
                    if len(cells) == 2:
                        tenor = cells[0].get_text(" ", strip=True)
                        rate = rate_to_float(cells[1].get_text(strip=True))
                        if rate != None and tenor_to_months(tenor) != None:
                            rows.append(make_row("Mox", currency, "time deposit", "promotional", tenor,
                                                 rate, None, None, "new funds only; rates may change",
                                                 MOX_URL, "html", fetched_at, None))
    return rows
