# test_email_report.py - Module 5 self-tests. No internet, no real email.
#   python test_email_report.py
# The bank rows below are INVENTED but in Member 1's exact row format, and the
# FX/Treasury database is filled from Member 2's saved answers.

import shutil
from datetime import datetime
from email import message_from_bytes
from pathlib import Path

import email_report as er
from bank_data import make_row
from market_and_returns import run_with_saved_answers, update

TS = "2026-11-24T08:00:00"
TODAY = "2026-11-24"
WORK = Path("data/_test_part5")


def row(bank, currency, tenor, rate, min_amount=None, rate_type="standard", conditions=""):
    return make_row(bank, currency, "time deposit", rate_type, tenor, rate, min_amount, None,
                    conditions, "saved copy", "html", TS, None)


BANK_ROWS = [
    row("eleBank", "HKD", "12 months", 3.40), row("eleBank", "USD", "12 months", 4.20),
    row("eleBank", "HKD", "3 months", 2.60), row("eleBank", "USD", "3 months", 3.60),
    row("HSBC", "HKD", "12 months", 0.15, 10000.0), row("HSBC", "USD", "12 months", 0.225, 2000.0),
    row("Mox", "USD", "12-month", 4.00, None, "promotional", "new funds only; rates may change"),
    row("Mox", "HKD", "3-month", 2.80, None, "promotional", "new funds only; rates may change"),
]
FAILED = ["Standard Chartered", "BEA (page loaded, but no HKD/USD rows)"]


def fresh_folder():
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    db = WORK / "market.db"
    run_with_saved_answers(True, True, update, db)       # FX 7.7791 and yields from 20 Nov 2026
    return db


def test_report_uses_member3_numbers():
    db = fresh_folder()
    report = er.build_report(100000.0, BANK_ROWS, FAILED, er.BROKER_RATES, db, TODAY, WORK / "h.csv")
    assert sorted(report["tables"]) == [1, 3, 6, 12]     # IB cash gives every horizon a route
    t12 = report["tables"][12]
    # the same numbers Member 3's own get_ranking gives - nothing recalculated here
    from market_and_returns import get_ranking
    direct, _ = get_ranking(100000.0, 12, BANK_ROWS, er.BROKER_RATES, today=TODAY, db_path=db)
    assert list(t12["option"]) == list(direct["option"])
    assert list(t12["hkd_end"]) == list(direct["hkd_end"])
    # the IB T-bill route is ranked now that broker_rates.json exists
    assert "IB (carry, T-bill)" in list(t12["option"])
    # conditions are carried through for the promotional Mox rate
    mox = t12[t12["option"] == "Mox (carry)"].iloc[0]
    assert mox["rate_type"] == "promotional" and "new funds" in mox["conditions"]
    assert report["failed_banks"] == FAILED
    assert any("typed in by hand" in w for w in report["warnings"])


def test_findings_say_the_key_things():
    db = fresh_folder()
    report = er.build_report(100000.0, BANK_ROWS, [], er.BROKER_RATES, db, TODAY, WORK / "h.csv")
    best = report["tables"][12].iloc[0]
    text = " ".join(report["findings"])
    assert best["option"] in report["findings"][0]
    assert f"{best['earned']:,.2f}" in report["findings"][0]
    if best["fx_risk"]:
        assert f"{best['breakeven_vs_best_hkd']:.4f}" in text and "band" in text
    assert "1m:" in text and "12m:" in text


def test_band_sentences():
    assert "7.75" in er.band_sentence({"band_verdict": "cannot lose inside the band"})
    s = er.band_sentence({"band_verdict": "can lose inside the band", "breakeven_vs_best_hkd": 7.7968})
    assert "7.7968" in s and "inside" in s
    assert "7.85" in er.band_sentence({"band_verdict": "loses even at 7.85"})


def test_history_one_block_per_day_and_change_line():
    db = fresh_folder()
    history = WORK / "h.csv"
    yesterday = er.build_report(100000.0, BANK_ROWS, [], er.BROKER_RATES, db, "2026-11-23", history)
    n = er.append_history(yesterday, history)
    assert er.append_history(yesterday, history) == n            # same day twice: replaced, not doubled
    better = [r.copy() for r in BANK_ROWS]
    better[1]["rate_pct"] = 4.50                                  # eleBank USD 12m goes up
    today = er.build_report(100000.0, better, [], er.BROKER_RATES, db, TODAY, history)
    assert today["previous"]["run_date"] == "2026-11-23"
    assert any("Change since 23 Nov 2026" in f for f in today["findings"])
    er.append_history(today, history)
    import pandas as pd
    saved = pd.read_csv(history)
    assert sorted(set(saved["run_date"])) == ["2026-11-23", TODAY]
    assert list(saved.columns) == er.HISTORY_COLUMNS


def test_no_data_still_gives_a_report():
    db = WORK / "empty.db"
    if db.exists():
        db.unlink()
    report = er.build_report(100000.0, [], ["eleBank", "HSBC"], [], db, TODAY, WORK / "none.csv")
    assert report["tables"] == {}
    assert "no ranking" in er.subject_line(report)
    assert "Could not load: eleBank" in er.text_body(report)


def test_email_and_pdf_are_built():
    db = fresh_folder()
    report = er.build_report(100000.0, BANK_ROWS, FAILED, er.BROKER_RATES, db, TODAY, WORK / "h.csv")
    pdf = er.make_pdf(report, WORK / "r.pdf", db)
    assert pdf.read_bytes()[:4] == b"%PDF" and pdf.stat().st_size > 20000
    msg = er.build_message(report, "a@x.com", ["b@y.com", "c@z.com"], pdf)
    parsed = message_from_bytes(bytes(msg))
    assert parsed["To"] == "b@y.com, c@z.com" and "HK$100k ranking 24 Nov 2026" in parsed["Subject"]
    kinds = [part.get_content_type() for part in parsed.walk()]
    assert "text/plain" in kinds and "text/html" in kinds and "application/pdf" in kinds
    assert "Key findings" in er.html_body(report) and "<script" not in er.html_body(report)


class FakeSMTP:
    sent = []
    fail_times = 0

    def __init__(self, host, port, timeout=None):
        if FakeSMTP.fail_times > 0:
            FakeSMTP.fail_times -= 1
            raise OSError("network down")
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        self.user = user

    def send_message(self, message):
        FakeSMTP.sent.append(message)


def test_send_and_retry():
    import time
    real_sleep = time.sleep
    time.sleep = lambda s: None
    try:
        config = {"smtp_host": "smtp.test", "smtp_port": 465, "sender": "a@x.com",
                  "password": "app-password", "to": ["b@y.com"]}
        FakeSMTP.sent, FakeSMTP.fail_times = [], 2
        assert er.send_message("hello", config, FakeSMTP) is True        # 2 failures, then sent
        assert FakeSMTP.sent == ["hello"]
        FakeSMTP.fail_times = 5
        assert er.send_message("hello", config, FakeSMTP) is False       # gives up after 3
        try:
            er.send_message("hello", dict(config, password=""), FakeSMTP)
            assert False, "missing password must raise"
        except ValueError:
            pass
    finally:
        time.sleep = real_sleep


def test_config_from_environment():
    import os
    os.environ["EMAIL_TO"] = "one@x.com, two@y.com"
    os.environ["SMTP_PORT"] = "587"
    try:
        config = er.load_config(WORK / "does_not_exist.json")
        assert config["to"] == ["one@x.com", "two@y.com"] and config["smtp_port"] == 587
    finally:
        del os.environ["EMAIL_TO"], os.environ["SMTP_PORT"]


def test_whole_run_end_to_end():
    db = fresh_folder()
    FakeSMTP.sent, FakeSMTP.fail_times = [], 0
    config = WORK / "cfg.json"
    config.write_text('{"sender": "a@x.com", "password": "p", "to": ["b@y.com"]}')
    report, sent = er.run_once(100000.0, send=True, refresh_market=False, db_path=db,
                               config_path=config, history_path=WORK / "h.csv",
                               output_dir=WORK / "out", bank_loader=lambda: (BANK_ROWS, FAILED),
                               smtp_class=FakeSMTP)
    assert sent and len(FakeSMTP.sent) == 1
    for ending in [".pdf", ".html", ".eml"]:
        assert (WORK / "out" / f"report_{report['date']}{ending}").exists()
    assert (WORK / "h.csv").exists()


def test_seconds_until():
    now = datetime(2026, 11, 24, 7, 30)
    assert er.seconds_until("08:00", now) == 1800
    assert er.seconds_until("07:00", now) == 23.5 * 3600              # already past: tomorrow


TESTS = [test_report_uses_member3_numbers, test_findings_say_the_key_things, test_band_sentences,
         test_history_one_block_per_day_and_change_line, test_no_data_still_gives_a_report,
         test_email_and_pdf_are_built, test_send_and_retry, test_config_from_environment,
         test_whole_run_end_to_end, test_seconds_until]

if __name__ == "__main__":
    import contextlib
    import io
    for test in TESTS:
        with contextlib.redirect_stdout(io.StringIO()):   # get_ranking prints skipped routes
            test()
        print("passed:", test.__name__)
    shutil.rmtree(WORK, ignore_errors=True)
    print("All", len(TESTS), "email report tests passed.")
