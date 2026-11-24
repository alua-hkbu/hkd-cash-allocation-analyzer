# =============================================================================
# email_report.py - Module 5: daily email report and final integration
# ECON3086 group project "Where should HK$100k sit?"      written by Member 5
#
# Every morning this file:
#   1. refreshes FX and Treasury data          (Member 2: update)
#   2. loads today's bank rates once           (Member 1, through live_ranking)
#   3. ranks every route for 1, 3, 6, 12 months (Member 3: get_ranking)
#   4. writes the key findings in plain words
#   5. builds an email (text + HTML) and a PDF report
#   6. appends today's ranking to data/rankings_history.csv
#   7. sends the email (or, with --dry-run, saves it to the output folder)
#
# It does NOT recalculate any return. Every number comes from get_ranking().
#
#   python email_report.py --dry-run           # build everything, send nothing
#   python email_report.py --send              # build and send
#   python email_report.py --send --daily-at 08:00   # keep running, send every morning
#
# Email settings: email_config.json (copy email_config.example.json), or the
# environment variables EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_TO, SMTP_HOST, SMTP_PORT.
# =============================================================================

import argparse
import json
import os
import smtplib
import sys
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from html import escape
from pathlib import Path

import pandas as pd

from live_ranking import DB_PATH, HORIZONS, load_bank_rows
from market_and_returns import get_daily_dataset, get_ranking, update

ROOT = Path(__file__).resolve().parent
BROKER_RATES_PATH = ROOT / "data" / "broker_rates.json"


def load_broker_rates(path=BROKER_RATES_PATH):
    """IB cash rates, typed in by hand. Member 1's scraper has no IB part, so
    without this file the IB cash and T-bill routes never reach the ranking.
    Format: broker_rates in INTERFACES.md. No file -> [] (no IB routes)."""
    path = Path(path)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as file:
        return json.load(file)


BROKER_RATES = load_broker_rates()
OUTPUT_DIR = ROOT / "output"
HISTORY_PATH = ROOT / "data" / "rankings_history.csv"
CONFIG_PATH = ROOT / "email_config.json"

DEFAULT_PRINCIPAL = 100000.0
MAIN_HORIZON = 12            # the horizon the headline and findings talk about
TOP_N = 5                    # rows per horizon in the email (the PDF shows all)

HISTORY_COLUMNS = ["run_date", "run_time", "principal", "months", "rank", "option",
                   "provider", "kind", "currency", "rate_pct", "hkd_end", "earned",
                   "return_pct", "vs_best_hkd", "breakeven_vs_best_hkd", "band_verdict",
                   "fx_mid", "fx_date", "tbill_date"]


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------
def money(x):
    return f"HK${x:,.2f}"


def signed_money(x):
    sign = "+" if x >= 0 else "-"
    return f"{sign}HK${abs(x):,.2f}"


def is_missing(x):
    return x is None or (isinstance(x, float) and pd.isna(x))


def nice_date(iso_text):
    return date.fromisoformat(iso_text).strftime("%d %b %Y")


# ---------------------------------------------------------------------------
# 1. Collect: one bank download, four rankings
# ---------------------------------------------------------------------------
def conditions_for(row, months, bank_rows, broker_rates):
    """The conditions text and rate type behind one ranked route.

    get_ranking keeps the rate but not the conditions, so look the row up
    again in Member 1's rows (same bank, currency, tenor and rate)."""
    for broker in broker_rates:
        if broker["provider"] == row["provider"]:
            if row["kind"] == "tbill":
                return "standard", "US T-bill bought through IB; IB commissions included"
            return "standard", "IB cash; first part of the balance earns 0%"
    for bank_row in bank_rows:
        if (bank_row["bank"] == row["provider"] and bank_row["currency"] == row["currency"]
                and bank_row["tenor_months"] == months and bank_row["rate_pct"] == row["rate_pct"]):
            return bank_row.get("rate_type", "standard"), bank_row.get("conditions", "")
    return "standard", ""


def build_report(principal, bank_rows, failed_banks, broker_rates=BROKER_RATES,
                 db_path=DB_PATH, today=None, history_path=HISTORY_PATH):
    """Everything the email and the PDF need, in one dictionary."""
    if today is None:
        today = date.today().isoformat()
    report = {"date": today, "principal": principal, "tables": {}, "warnings": [],
              "failed_banks": list(failed_banks), "bank_count": len(bank_names(bank_rows)),
              "bank_rows": len(bank_rows), "broker_rates": broker_rates}

    for months in HORIZONS:
        table = None
        warnings = []
        if bank_rows or broker_rates:
            table, warnings = get_ranking(principal, months, bank_rows, broker_rates,
                                          today=today, db_path=db_path)
        for warning in warnings:
            if warning not in report["warnings"]:
                report["warnings"].append(warning)
        if table is not None and len(table) > 0:
            table = table.copy()
            types = []
            conditions = []
            for i in range(len(table)):
                rate_type, text = conditions_for(table.iloc[i], months, bank_rows, broker_rates)
                types.append(rate_type)
                conditions.append(text)
            table["rate_type"] = types
            table["conditions"] = conditions
            report["tables"][months] = table

    for broker in broker_rates:
        checked = str(broker.get("fetched_at", ""))[:10]
        if checked:
            report["warnings"].append(
                f"{broker['provider']} rates were typed in by hand on {checked} - check them on IB's page")

    report["previous"] = previous_best(history_path, today, principal)
    report["findings"] = key_findings(report)
    return report


def bank_names(bank_rows):
    names = []
    for row in bank_rows:
        if row["bank"] not in names:
            names.append(row["bank"])
    return names


# ---------------------------------------------------------------------------
# 2. Key findings, in plain words
# ---------------------------------------------------------------------------
def best_hkd_row(table):
    hkd = table[table["fx_risk"] == False]
    if len(hkd) == 0:
        return None
    return hkd.iloc[0]               # the table is already sorted, best first


def band_sentence(row):
    verdict = row["band_verdict"]
    if verdict == "cannot lose inside the band":
        return ("It still beats the best HKD route even if USD/HKD ends at 7.75, the "
                "strongest HKD the HKMA band allows, so the remaining risk is a change to the peg itself.")
    if verdict == "can lose inside the band":
        return (f"It falls behind if USD/HKD at the exit is below {row['breakeven_vs_best_hkd']:.4f}. "
                "That rate is inside the 7.75-7.85 band, so normal moves within the peg can erase the gain.")
    if verdict == "loses even at 7.85":
        return "It loses to the best HKD route even at 7.85, the best exit rate the band allows."
    return ""


def key_findings(report):
    findings = []
    tables = report["tables"]
    if not tables:
        return ["No ranking could be produced today. See the data warnings below."]

    months = MAIN_HORIZON if MAIN_HORIZON in tables else max(tables)
    table = tables[months]
    best = table.iloc[0]
    findings.append(
        f"Best route for {months} months: {best['option']} at {best['rate_pct']:.3f}% p.a. - "
        f"{money(best['hkd_end'])} at the end, a gain of {money(best['earned'])} "
        f"({best['return_pct']:.3f}%).")

    safe = best_hkd_row(table)
    if best["fx_risk"]:
        if safe is not None:
            findings.append(
                f"It is a USD route. Versus the best HKD route ({safe['option']}, "
                f"{money(safe['earned'])}) it earns {money(abs(best['vs_best_hkd']))} "
                f"{'more' if best['vs_best_hkd'] >= 0 else 'less'}. "
                f"Break-even exit rate: {best['breakeven_vs_best_hkd']:.4f} HKD per USD "
                f"(today's mid: {best['fx_mid']:.4f}).")
        sentence = band_sentence(best)
        if sentence:
            findings.append(sentence)
    elif safe is not None:
        usd = table[table["fx_risk"] == True]
        if len(usd) > 0:
            top_usd = usd.iloc[0]
            findings.append(
                f"No FX risk is needed: the best USD route ({top_usd['option']}) ends "
                f"{money(abs(top_usd['vs_best_hkd']))} behind the best HKD route.")

    if best["rate_type"] == "promotional":
        findings.append(f"The top rate is promotional: {best['conditions']}.")

    by_horizon = []
    for m in HORIZONS:
        if m in tables:
            top = tables[m].iloc[0]
            by_horizon.append(f"{m}m: {top['option']} ({top['return_pct']:.3f}%)")
    findings.append("Best by horizon - " + "; ".join(by_horizon) + ".")

    previous = report.get("previous")
    if previous is not None and previous["months"] == months:
        change = best["earned"] - previous["earned"]
        if previous["option"] != best["option"]:
            findings.append(f"Change since {nice_date(previous['run_date'])}: the top route was "
                            f"{previous['option']} ({money(previous['earned'])}); now {best['option']}.")
        else:
            findings.append(f"Change since {nice_date(previous['run_date'])}: same top route, "
                            f"expected gain {signed_money(change)}.")
    return findings


# ---------------------------------------------------------------------------
# 3. History: one block of rows per day, for the dashboard's "over time" chart
# ---------------------------------------------------------------------------
def append_history(report, path=HISTORY_PATH):
    """Add today's rows. Running twice on one day replaces that day's rows."""
    path = Path(path)
    frames = []
    for months in report["tables"]:
        table = report["tables"][months].copy()
        table["run_date"] = report["date"]
        table["run_time"] = datetime.now().strftime("%H:%M:%S")
        frames.append(table[HISTORY_COLUMNS])
    if not frames:
        return 0
    today_rows = pd.concat(frames, ignore_index=True)
    if path.exists():
        old = pd.read_csv(path)
        old = old[~((old["run_date"] == report["date"]) & (old["principal"] == report["principal"]))]
        today_rows = pd.concat([old, today_rows], ignore_index=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    today_rows.to_csv(path, index=False)
    return len(today_rows)


def previous_best(path, today, principal, months=MAIN_HORIZON):
    """The rank-1 row of the last report before today, or None."""
    path = Path(path)
    if not path.exists():
        return None
    old = pd.read_csv(path)
    old = old[(old["run_date"] < today) & (old["principal"] == principal)
              & (old["months"] == months) & (old["rank"] == 1)]
    if len(old) == 0:
        return None
    return old.sort_values("run_date").iloc[-1].to_dict()


# ---------------------------------------------------------------------------
# 4. The email: a subject, a plain-text body and an HTML body
# ---------------------------------------------------------------------------
def subject_line(report):
    head = f"HK${report['principal'] / 1000:,.0f}k ranking {nice_date(report['date'])}"
    tables = report["tables"]
    if MAIN_HORIZON in tables:
        best = tables[MAIN_HORIZON].iloc[0]
        return f"{head}: best {MAIN_HORIZON}m = {best['option']} ({signed_money(best['earned'])})"
    return head + ": no ranking today - data problem"


def text_body(report):
    lines = [f"Where should {money(report['principal'])} sit? Report for {nice_date(report['date'])}", ""]
    lines.append("KEY FINDINGS")
    for finding in report["findings"]:
        lines.append("  - " + finding)
    for months in HORIZONS:
        if months not in report["tables"]:
            continue
        lines.append("")
        lines.append(f"TOP {TOP_N} - {months} MONTH{'S' if months > 1 else ''}")
        for _, row in report["tables"][months].head(TOP_N).iterrows():
            extra = ""
            if row["fx_risk"] and not is_missing(row["breakeven_vs_best_hkd"]):
                extra = f" | break-even {row['breakeven_vs_best_hkd']:.4f} | {row['band_verdict']}"
            lines.append(f"  {int(row['rank'])}. {row['option']:<24} {row['rate_pct']:>6.3f}%  "
                         f"end {money(row['hkd_end'])}  gain {money(row['earned'])}{extra}")
    lines += ["", "DATA"] + ["  - " + text for text in data_notes(report)]
    lines += ["", disclaimer()]
    return "\n".join(lines)


def data_notes(report):
    notes = []
    any_table = None
    for months in report["tables"]:
        any_table = report["tables"][months]
    if any_table is not None:
        row = any_table.iloc[0]
        notes.append(f"USD/HKD mid {row['fx_mid']:.4f} (Frankfurter, {row['fx_date']}); "
                     f"T-bill yields from the US Treasury, {row['tbill_date']}")
    notes.append(f"{report['bank_rows']} bank rate rows from {report['bank_count']} banks")
    for name in report["failed_banks"]:
        notes.append("Could not load: " + name)
    notes += report["warnings"]
    return notes


def disclaimer():
    return ("Rates are for reference only and can change without notice. Promotional rates have "
            "conditions. FX spreads for banks without a published quote are estimates. "
            "This is a student project, not financial advice.")


def html_table(table):
    head = ("<tr>" + "".join(f"<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ccc'>{h}</th>"
            for h in ["#", "Option", "Rate", "Final value", "Gain", "Break-even", "HKMA band"]) + "</tr>")
    body = ""
    for _, row in table.head(TOP_N).iterrows():
        breakeven = "-"
        band = "no FX risk"
        if row["fx_risk"]:
            band = row["band_verdict"]
            if not is_missing(row["breakeven_vs_best_hkd"]):
                breakeven = f"{row['breakeven_vs_best_hkd']:.4f}"
        colour = {"cannot lose inside the band": "#1b7f3b", "can lose inside the band": "#b26b00",
                  "loses even at 7.85": "#b00020"}.get(band, "#555")
        promo = " *" if row["rate_type"] == "promotional" else ""
        cells = [str(int(row["rank"])), escape(row["option"]) + promo, f"{row['rate_pct']:.3f}%",
                 money(row["hkd_end"]), money(row["earned"]), breakeven,
                 f"<span style='color:{colour}'>{escape(band)}</span>"]
        body += "<tr>" + "".join(f"<td style='padding:4px 8px;border-bottom:1px solid #eee'>{c}</td>"
                                 for c in cells) + "</tr>"
    return f"<table style='border-collapse:collapse;font-size:13px'>{head}{body}</table>"


def html_body(report):
    parts = ["<div style='font-family:Arial,sans-serif;color:#222;max-width:760px'>",
             f"<h2 style='margin-bottom:4px'>Where should {money(report['principal'])} sit?</h2>",
             f"<div style='color:#666'>Daily report for {nice_date(report['date'])}</div>",
             "<h3>Key findings</h3><ul>"]
    for finding in report["findings"]:
        parts.append(f"<li style='margin-bottom:4px'>{escape(finding)}</li>")
    parts.append("</ul>")
    for months in HORIZONS:
        if months in report["tables"]:
            parts.append(f"<h3>Top {TOP_N}: {months} month{'s' if months > 1 else ''}</h3>")
            parts.append(html_table(report["tables"][months]))
    parts.append("<p style='font-size:12px;color:#666'>* promotional rate - conditions apply "
                 "(see the PDF). Break-even = the USD/HKD exit rate at which the route only "
                 "matches the best HKD route.</p>")
    parts.append("<h3>Data</h3><ul style='font-size:12px;color:#444'>")
    for note in data_notes(report):
        parts.append(f"<li>{escape(note)}</li>")
    parts.append(f"</ul><p style='font-size:11px;color:#888'>{escape(disclaimer())}</p></div>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 5. The PDF attachment (reportlab tables + one matplotlib chart)
# ---------------------------------------------------------------------------
def make_chart(report, path, db_path=DB_PATH):
    """Left: 12-month gain per route. Right: USD/HKD history with the HKMA band."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (left, right) = plt.subplots(1, 2, figsize=(10, 3.6))
    months = MAIN_HORIZON if MAIN_HORIZON in report["tables"] else max(report["tables"])
    table = report["tables"][months].iloc[::-1]
    colours = ["#d9822b" if risk else "#2b6cb0" for risk in table["fx_risk"]]
    left.barh(table["option"], table["earned"], color=colours)
    left.set_title(f"Net gain on {money(report['principal'])}, {months} months", fontsize=10)
    left.set_xlabel("HKD (blue = HKD route, orange = USD route)", fontsize=8)
    left.tick_params(labelsize=7)

    daily = get_daily_dataset(db_path=db_path)
    if len(daily) > 0 and "usd_hkd" in daily.columns:
        fx = daily.dropna(subset=["usd_hkd"]).tail(180)
        right.plot(pd.to_datetime(fx["date"]), fx["usd_hkd"], color="#2b6cb0")
        right.axhline(7.75, linestyle="--", color="gray", linewidth=1)
        right.axhline(7.85, linestyle="--", color="gray", linewidth=1)
        right.set_ylim(7.74, 7.86)
        right.set_title("USD/HKD and the HKMA band 7.75-7.85", fontsize=10)
        right.tick_params(labelsize=7)
        fig.autofmt_xdate()
    else:
        right.axis("off")
        right.text(0.5, 0.5, "No FX history yet", ha="center")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def make_pdf(report, path, db_path=DB_PATH):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (Image, PageBreak, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)

    path = Path(path)
    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=8, leading=10)
    doc = SimpleDocTemplate(str(path), pagesize=landscape(A4), leftMargin=1.5 * cm,
                            rightMargin=1.5 * cm, topMargin=1.2 * cm, bottomMargin=1.2 * cm)
    story = [Paragraph(f"Where should {money(report['principal'])} sit?", styles["Title"]),
             Paragraph(f"Daily report for {nice_date(report['date'])}", styles["Normal"]),
             Spacer(1, 8), Paragraph("Key findings", styles["Heading2"])]
    for finding in report["findings"]:
        story.append(Paragraph("&bull; " + escape(finding), styles["BodyText"]))

    if report["tables"]:
        chart_path = path.with_suffix(".png")
        make_chart(report, chart_path, db_path)
        story += [Spacer(1, 8), Image(str(chart_path), width=24 * cm, height=8.6 * cm)]

    for months in HORIZONS:
        if months not in report["tables"]:
            continue
        story += [PageBreak(), Paragraph(f"Full ranking: {months} months", styles["Heading2"])]
        rows = [["#", "Option", "Rate %", "Final HKD", "Gain HKD", "Return %",
                 "vs best HKD", "Break-even", "At 7.75", "At 7.85", "HKMA band", "Conditions"]]
        for _, row in report["tables"][months].iterrows():
            def number(value, pattern):
                return "-" if is_missing(value) else format(value, pattern)
            rows.append([int(row["rank"]), Paragraph(escape(row["option"]), small),
                         f"{row['rate_pct']:.3f}", f"{row['hkd_end']:,.2f}", f"{row['earned']:,.2f}",
                         f"{row['return_pct']:.3f}", number(row["vs_best_hkd"], ",.2f"),
                         number(row["breakeven_vs_best_hkd"], ".4f"),
                         number(row["hkd_if_exit_at_7.75"], ",.2f"),
                         number(row["hkd_if_exit_at_7.85"], ",.2f"),
                         Paragraph(escape(row["band_verdict"]), small),
                         Paragraph(escape(row["conditions"] or ""), small)])
        widths = [0.8, 4.2, 1.5, 2.3, 1.9, 1.6, 2.0, 1.8, 2.1, 2.1, 2.6, 4.0]
        table = Table(rows, colWidths=[w * cm for w in widths], repeatRows=1)
        table.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2b6cb0")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f5f9")]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (2, 1), (9, -1), "RIGHT")]))
        story.append(table)

    story += [Spacer(1, 12), Paragraph("How to read the FX columns", styles["Heading3"]),
              Paragraph("A USD route changes HKD into USD at the provider's selling price, earns USD "
                        "interest, then changes back at its buying price. <b>Break-even</b> is the "
                        "USD/HKD mid rate at the exit at which the route only matches the best HKD "
                        "route. The HKMA keeps USD/HKD between 7.75 and 7.85, so the columns "
                        "<b>At 7.75</b> and <b>At 7.85</b> show the worst and best final HKD the band "
                        "allows (the provider's spread is applied at each edge).", styles["BodyText"]),
              Spacer(1, 8), Paragraph("Data", styles["Heading3"])]
    for note in data_notes(report):
        story.append(Paragraph(escape(note), small))
    story += [Spacer(1, 6), Paragraph(escape(disclaimer()), small)]
    doc.build(story)
    return path


# ---------------------------------------------------------------------------
# 6. Sending
# ---------------------------------------------------------------------------
def load_config(path=CONFIG_PATH):
    """email_config.json first, then environment variables win over it."""
    config = {"smtp_host": "smtp.gmail.com", "smtp_port": 465, "sender": "", "password": "", "to": []}
    path = Path(path)
    if path.exists():
        with open(path, encoding="utf-8") as file:
            config.update(json.load(file))
    names = {"EMAIL_SENDER": "sender", "EMAIL_PASSWORD": "password", "EMAIL_TO": "to",
             "SMTP_HOST": "smtp_host", "SMTP_PORT": "smtp_port"}
    for variable in names:
        if os.environ.get(variable):
            config[names[variable]] = os.environ[variable]
    if isinstance(config["to"], str):
        config["to"] = [address.strip() for address in config["to"].split(",") if address.strip()]
    config["smtp_port"] = int(config["smtp_port"])
    return config


def build_message(report, sender, recipients, pdf_path=None):
    message = EmailMessage()
    message["Subject"] = subject_line(report)
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(text_body(report))
    message.add_alternative(html_body(report), subtype="html")
    if pdf_path is not None and Path(pdf_path).exists():
        message.add_attachment(Path(pdf_path).read_bytes(), maintype="application",
                               subtype="pdf", filename=Path(pdf_path).name)
    return message


def send_message(message, config, smtp_class=None):
    """Port 465 = SSL from the start (Gmail); any other port = STARTTLS (e.g. 587).
    smtp_class is only for the tests, which pass a fake server."""
    if not config["sender"] or not config["password"] or not config["to"]:
        raise ValueError("Email settings missing: set sender, password and to in email_config.json")
    if smtp_class is None:
        smtp_class = smtplib.SMTP_SSL if config["smtp_port"] == 465 else smtplib.SMTP
    for attempt in [1, 2, 3]:                      # the week 6 retry pattern again
        try:
            with smtp_class(config["smtp_host"], config["smtp_port"], timeout=30) as server:
                if config["smtp_port"] != 465:
                    server.starttls()
                server.login(config["sender"], config["password"])
                server.send_message(message)
            return True
        except smtplib.SMTPAuthenticationError:
            raise                                   # a wrong password will not fix itself
        except (smtplib.SMTPException, OSError) as error:
            print("send attempt", attempt, "failed:", error)
            time.sleep(5 * attempt)
    return False


# ---------------------------------------------------------------------------
# 7. One whole run, and the morning loop
# ---------------------------------------------------------------------------
def run_once(principal=DEFAULT_PRINCIPAL, send=False, refresh_market=True, db_path=DB_PATH,
             config_path=CONFIG_PATH, history_path=HISTORY_PATH, output_dir=OUTPUT_DIR,
             bank_loader=load_bank_rows, smtp_class=None):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "- daily report starts")
    if refresh_market:
        update(db_path)                             # keeps old data if the APIs are down
    bank_rows, failed = bank_loader()
    print(f"bank rows: {len(bank_rows)}; failed: {failed or 'none'}")

    report = build_report(principal, bank_rows, failed, db_path=db_path, history_path=history_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"report_{report['date']}"
    pdf_path = None
    if report["tables"]:
        pdf_path = make_pdf(report, output_dir / (stem + ".pdf"), db_path)
        append_history(report, history_path)
    (output_dir / (stem + ".html")).write_text(html_body(report), encoding="utf-8")

    config = load_config(config_path)
    sender = config["sender"] or "report@example.com"
    message = build_message(report, sender, config["to"] or ["team@example.com"], pdf_path)
    (output_dir / (stem + ".eml")).write_bytes(bytes(message))
    print("subject:", message["Subject"])
    print("saved:", output_dir / (stem + ".html"), "and .eml", "and .pdf" if pdf_path else "")

    if send:
        sent = send_message(message, config, smtp_class)
        print("email sent to", ", ".join(config["to"]) if sent else "NOBODY - all attempts failed")
        return report, sent
    print("dry run: nothing sent")
    return report, False


def seconds_until(clock_text, now=None):
    """Seconds from now until the next HH:MM (today if still ahead, else tomorrow)."""
    if now is None:
        now = datetime.now()
    hour, minute = [int(part) for part in clock_text.split(":")]
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return (target - now).total_seconds()


def run_every_morning(clock_text, **options):
    """Simple scheduler for a computer left on. Task Scheduler / cron is better
    (see README_part5.md), because it also works after a restart."""
    while True:
        wait = seconds_until(clock_text)
        print(f"next report in {wait / 3600:.1f} hours (at {clock_text})")
        time.sleep(wait)
        try:
            run_once(**options)
        except Exception as error:                  # one bad morning must not stop the loop
            print("report failed:", error)


def main():
    parser = argparse.ArgumentParser(description="Daily email report (Module 5)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--send", action="store_true", help="send the email")
    group.add_argument("--dry-run", action="store_true", help="build the files, send nothing (default)")
    parser.add_argument("--principal", type=float, default=DEFAULT_PRINCIPAL)
    parser.add_argument("--no-update", action="store_true", help="do not refresh FX and Treasury data")
    parser.add_argument("--daily-at", metavar="HH:MM", help="keep running and report every day at this time")
    args = parser.parse_args()

    options = {"principal": args.principal, "send": args.send, "refresh_market": not args.no_update}
    if args.daily_at:
        run_every_morning(args.daily_at, **options)
    else:
        report, sent = run_once(**options)
        if args.send and not sent:
            sys.exit(1)        # a failed send fails the GitHub job, so GitHub warns the owner


if __name__ == "__main__":
    main()
