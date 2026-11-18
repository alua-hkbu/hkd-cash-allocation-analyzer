from bank_data import *

fetched_at = now_text()

sources = [
    ("eleBank", fetch_elebank, parse_elebank),
    ("HSBC", fetch_hsbc, parse_hsbc),
    ("Standard Chartered", fetch_sc, parse_sc),
    ("BEA", fetch_bea, parse_bea),
    ("Mox", fetch_mox, parse_mox),
]

for name, fetch, parse in sources:
    raw = fetch()
    if raw == None:
        print(name, "-> FETCH FAILED (all 3 attempts)")
        continue
    rows = parse(raw, fetched_at)
    print(name, "->", len(rows), "rows")
    if len(rows) > 0:
        print("   first row:", rows[0])



html = fetch_html(HSBC_URL)
print("downloaded:", html != None, "| length:", len(html) if html else 0)

soup = BeautifulSoup(html, "html.parser")
print("tables:", len(soup.find_all("table")))
print("--- headings seen by the parser ---")
for el in soup.find_all(["h2", "h3", "table"]):
    if el.name == "table":
        print("   [table]", len(el.find_all("tr")), "rows")
    else:
        text = el.get_text(" ", strip=True)
        print(el.name, repr(text), "-> currency:", currency_from_heading(text))

open("hsbc_dump.html", "w", encoding="utf-8").write(html)