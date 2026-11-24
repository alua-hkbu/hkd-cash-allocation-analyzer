# integration_check.py - final integration check (Member 5). No internet needed.
#   python integration_check.py
# Runs every member's self-tests, then checks that the modules fit together
# (INTERFACES.md). A part whose files are not in this branch yet is SKIPPED,
# not failed, so each feature branch can be checked on its own. Once all the
# branches are merged into main, nothing is skipped.
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)                      # the tests use paths like data/_selftest.db

# (name, files it needs, command)
SUITES = [
    ("Member 1 - bank data", ["bank_data.py", "test_bank_data.py"], ["test_bank_data.py"]),
    ("Members 2 + 3 - FX, Treasury, ranking", ["market_and_returns.py"],
     ["market_and_returns.py", "test"]),
    ("Member 5 - email report", ["bank_data.py", "market_and_returns.py", "live_ranking.py",
                                 "email_report.py", "test_email_report.py"],
     ["test_email_report.py"]),
]


def missing(files):
    return [name for name in files if not (ROOT / name).exists()]


failed = []
skipped = []
for name, needs, command in SUITES:
    print("=" * 60, "\n" + name)
    gone = missing(needs)
    if gone:
        print("SKIPPED - not in this branch yet:", ", ".join(gone))
        skipped.append(name)
        continue
    result = subprocess.run([sys.executable] + command, capture_output=True, text=True)
    print(result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "(no output)")
    if result.returncode != 0:
        print(result.stderr[-1500:])
        failed.append(name)

print("=" * 60, "\nIntegration checks")
gone = missing(["bank_data.py", "market_and_returns.py", "live_ranking.py", "email_report.py"])
if gone:
    print("SKIPPED - not in this branch yet:", ", ".join(gone))
    skipped.append("integration checks")
else:
    try:
        from email_report import BROKER_RATES  # also pulls in live_ranking and market_and_returns
        from live_ranking import SOURCES
        from market_and_returns import BANK_ROW_KEYS, check_inputs
        from bank_data import make_row
        row = make_row("X", "HKD", "time deposit", "standard", "12 months", 1.0, None, None,
                       "", "u", "html", "t", None)
        assert all(key in row for key in BANK_ROW_KEYS), "Member 1's rows lack a key Member 3 needs"
        assert check_inputs(100000, 12, [row], BROKER_RATES, {"mid": 7.8}, {"yields_pct": {}}) == [], \
            "broker_rates.json or Member 1's rows fail Member 3's input checks"
        assert len(SOURCES) >= 5, "the brief asks for at least 5 banks"
        print("bank rows, broker rates and the 5 bank sources fit Member 3's ranking")
    except Exception as error:
        print("FAILED:", error)
        failed.append("integration checks")

print("=" * 60)
if failed:
    print("FAILED: " + ", ".join(failed))
elif skipped:
    print("PASSED (skipped until merged: " + ", ".join(skipped) + ")")
else:
    print("ALL PASSED")
sys.exit(1 if failed else 0)
