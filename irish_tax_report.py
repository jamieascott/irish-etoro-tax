#!/usr/bin/env python3
"""Irish etoro Tax Report.

Reads one or more etoro annual account statements (.xlsx) and produces an HTML
dashboard plus CSVs covering, for the latest year:
  - Capital Gains Tax: 33%, €1,270 exemption, loss offset and carry-forward across
    the statements given, 4-week-rule flags, 15 Dec / 31 Jan payment split
  - Dividends: gross, withholding tax, treaty-rate credit, income tax/USC/PRSI
  - ETFs: 8-year deemed disposal and exit tax per lot
All EUR figures use ECB reference rates (downloaded once, cached in fx/).

Standard library only - no installs needed.

Usage:
    python3 irish_tax_report.py                      # every statement in ../
    python3 irish_tax_report.py a.xlsx b.xlsx --out some/dir
    python3 irish_tax_report.py --income-tax 0.2 --usc 0.03
    python3 irish_tax_report.py --manual other-brokers.csv   # + entries exported from the web app
"""

import argparse
from datetime import date, timedelta
from pathlib import Path

import dividends as dv
from cgt import cgt_years, flag_four_week, load_disposals
from deemed_disposal import load_account_summary, load_open_lots
from etoro import History, Statement, find_statements
from fx import EcbRates
from manual import read_manual, to_disposal, to_dividend
from report import (Report, eur, write_deemed_disposal_csv, write_dividends_csv, write_disposals_csv,
                    write_html)

HERE = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("statements", nargs="*", type=Path,
                    help="etoro statements (default: every etoro-account-statement-*.xlsx in ../)")
    ap.add_argument("--out", type=Path, default=HERE / "output")
    ap.add_argument("--income-tax", type=float, default=0.40, help="marginal income tax rate (default 0.40)")
    ap.add_argument("--usc", type=float, default=0.08, help="marginal USC rate (default 0.08)")
    ap.add_argument("--prsi", type=float, default=None, help="PRSI rate (default: rate in force on payment date)")
    ap.add_argument("--losses-bf", type=float, default=0.0,
                    help="CGT losses brought forward into the earliest statement year (EUR)")
    ap.add_argument("--manual", type=Path, action="append", default=[], metavar="CSV",
                    help="manual entries for other brokers, as exported by the web app (repeatable)")
    ap.add_argument("--offline", action="store_true", help="use cached ECB rates only")
    args = ap.parse_args()

    paths = args.statements or find_statements(HERE.parent)
    manual_disposals, manual_dividends = [], []
    for p in args.manual:
        d, v = read_manual(p)
        manual_disposals += d
        manual_dividends += v
    if not paths and not (manual_disposals or manual_dividends):
        raise SystemExit(f"No etoro statements found in {HERE.parent}, and no --manual entries given")
    statements = [Statement.open(p) for p in paths]
    history = History(statements)
    for m in manual_disposals:  # so the 4-week rule sees purchases at every broker
        history.add_acquisition(m.isin, m.acquired, m.id)
    latest = history.latest

    dates = ([d for acq in history.acquisitions.values() for d, _ in acq]
             + [m.disposed for m in manual_disposals] + [m.paid for m in manual_dividends]
             + [s.end for s in statements])
    fx = EcbRates(HERE / "fx" / "ecb_usd_per_eur.csv", min(dates) - timedelta(days=10), max(dates),
                  offline=args.offline)

    rates = dv.TaxRates(args.income_tax, args.usc, args.prsi)
    disposals = {s.year: load_disposals(s, history, fx) for s in history.statements}
    divs = {s.year: [dv.assess(d, rates) for d in dv.load_dividends(s)] for s in history.statements}
    for m in manual_disposals:
        d = to_disposal(m, fx)
        flag_four_week(d, history)
        disposals.setdefault(d.closed.year, []).append(d)
    for m in manual_dividends:
        divs.setdefault(m.paid.year, []).append(dv.assess(to_dividend(m, fx), rates))
    years = set(disposals) | set(divs)
    for y in years:  # every year gets a CGT row, so losses chain through years with only dividends
        disposals.setdefault(y, []).sort(key=lambda d: (d.closed, d.name))
        divs.setdefault(y, []).sort(key=lambda t: (t.d.paid, t.d.name))
    snapshot, open_lots = load_open_lots(latest, history, fx) if latest else (None, [])

    r = Report(
        today=date.today(), year=max(years), snapshot=snapshot,
        statements=[p.name for p in paths] + [p.name for p in args.manual],
        account=load_account_summary(latest) if latest else None, open_lots=open_lots,
        cgt=cgt_years(disposals, conservative=False, opening_losses=args.losses_bf),
        cgt_conservative=cgt_years(disposals, conservative=True, opening_losses=args.losses_bf),
        dividends=divs, rates=rates,
    )

    args.out.mkdir(parents=True, exist_ok=True)
    outputs = {
        "irish_tax_report.html": lambda p: write_html(p, r),
        "cgt_disposals.csv": lambda p: write_disposals_csv(p, r.cgt),
        "dividends.csv": lambda p: write_dividends_csv(p, divs),
        "deemed_disposal_lots.csv": lambda p: write_deemed_disposal_csv(p, open_lots),
    }
    for name, write in outputs.items():
        write(args.out / name)

    for y, yc in zip(r.cgt, r.cgt_conservative):
        print(f"CGT {y.year}: {len(y.chargeable)} disposals, net {eur(y.full.gains - y.full.losses)}, "
              f"tax {eur(y.full.tax)} (conservative {eur(yc.full.tax)}), "
              f"losses c/f {eur(y.full.carried_forward)}")
    c = r.cgt_latest
    print(f"  {c.year} payments: {eur(c.tax_initial)} by {c.due_initial}, {eur(c.tax_later)} by {c.due_later}")
    for year, items in sorted(divs.items()):
        t = dv.totals(items)
        print(f"Dividends {year}: gross {eur(t['gross'])}, WHT {eur(t['wht'])}, credit {eur(t['credit'])}, "
              f"Irish tax {eur(t['irish_tax'])}")
    in_scope = [l for l in open_lots if l.applies]
    print(f"ETFs {snapshot or 'n/a'}: {len(in_scope)} exit-tax lots, cost {eur(sum(l.purchase_value for l in in_scope))}, "
          f"value {eur(sum(l.current_value for l in in_scope))}, "
          f"exit tax now {eur(sum(l.exit_tax for l in in_scope))}")
    for name in outputs:
        print(f"Wrote {args.out / name}")


if __name__ == "__main__":
    main()
