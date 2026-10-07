#!/usr/bin/env python3
"""Irish Deemed Disposal Tracker.

Reads the "Holdings" and "Closed Positions" tabs of an eToro account statement
(.xlsx) and produces an HTML dashboard + CSV showing, for every ETF lot:
purchase date, name, ISIN, units, purchase value, current value, gain/loss,
the 8-year deemed disposal date and exit tax on gains only.

Standard library only - no installs needed.

Usage:
    python3 deemed_disposal_tracker.py [statement.xlsx] [--out output_dir]
"""

import argparse
import csv
import html
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_STATEMENT = HERE.parent / "etoro-account-statement-1-1-2025-12-31-2025.xlsx"

DEEMED_DISPOSAL_YEARS = 8
# Exit tax: 41% for disposals up to 31 Dec 2025, 38% from 1 Jan 2026 (Budget 2026).
EXIT_TAX_RATE_CHANGE = date(2026, 1, 1)
EXIT_TAX_OLD, EXIT_TAX_NEW = 0.41, 0.38

# Funds domiciled in the EU/EEA fall under the exit tax / deemed disposal regime.
# US-domiciled ETFs are generally taxed under CGT instead; others need checking.
EEA_ISIN_PREFIXES = {
    "IE", "LU", "DE", "FR", "NL", "AT", "BE", "DK", "FI", "SE", "ES", "PT",
    "IT", "GR", "CY", "MT", "EE", "LV", "LT", "PL", "CZ", "SK", "SI", "HU",
    "HR", "RO", "BG", "NO", "IS", "LI",
}

XML_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
XML_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


# --------------------------------------------------------------------------- #
# Minimal .xlsx reader
# --------------------------------------------------------------------------- #
def read_sheet(path: Path, sheet_name: str) -> list[dict]:
    """Return the rows of a worksheet as dicts keyed by the header row."""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(XML_MAIN + "si"):
                shared.append("".join(t.text or "" for t in si.iter(XML_MAIN + "t")))

        rels = {r.get("Id"): r.get("Target")
                for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        target = None
        for s in ET.fromstring(z.read("xl/workbook.xml")).find(XML_MAIN + "sheets"):
            if s.get("name") == sheet_name:
                target = rels[s.get(XML_REL + "id")].lstrip("/")
                if not target.startswith("xl/"):
                    target = "xl/" + target
        if target is None:
            raise SystemExit(f"Sheet {sheet_name!r} not found in {path.name}")

        rows = []
        for row in ET.fromstring(z.read(target)).find(XML_MAIN + "sheetData").findall(XML_MAIN + "row"):
            cells = {}
            for c in row.findall(XML_MAIN + "c"):
                col = "".join(ch for ch in c.get("r", "") if ch.isalpha())
                kind, v = c.get("t"), c.find(XML_MAIN + "v")
                if kind == "inlineStr":
                    val = "".join(t.text or "" for t in c.iter(XML_MAIN + "t"))
                elif v is None:
                    val = ""
                elif kind == "s":
                    val = shared[int(v.text)]
                else:
                    val = v.text
                cells[col] = val
            rows.append(cells)

    # Blank header cells fall back to the column letter so no column is dropped.
    header = rows[0]
    return [{header.get(k) or k: v for k, v in r.items()} for r in rows[1:]]


def excel_date(serial: str) -> date:
    return date(1899, 12, 30) + timedelta(days=int(float(serial)))


def etoro_datetime(text: str) -> date:
    return datetime.strptime(text.strip(), "%d/%m/%Y %H:%M:%S").date()


def num(text: str) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 Feb -> 28 Feb
        return d.replace(year=d.year + years, day=28)


def exit_tax_rate(on: date) -> float:
    return EXIT_TAX_NEW if on >= EXIT_TAX_RATE_CHANGE else EXIT_TAX_OLD


def regime_for(isin: str) -> str:
    prefix = isin[:2].upper()
    if prefix in EEA_ISIN_PREFIXES:
        return "Exit tax"
    if prefix == "US":
        return "CGT (US-domiciled)"
    return "Check (non-EEA)"


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
@dataclass
class OpenLot:
    position_id: str
    purchased: date
    name: str
    isin: str
    units: float
    purchase_value: float  # EUR
    current_value: float   # EUR
    regime: str

    @property
    def gain(self) -> float:
        return self.current_value - self.purchase_value

    @property
    def deemed_disposal_date(self) -> date:
        return add_years(self.purchased, DEEMED_DISPOSAL_YEARS)

    @property
    def applies(self) -> bool:
        return self.regime == "Exit tax"

    @property
    def exit_tax(self) -> float:
        if not self.applies:
            return 0.0
        return max(self.gain, 0.0) * exit_tax_rate(self.deemed_disposal_date)


@dataclass
class ClosedLot:
    position_id: str
    purchased: date
    sold: date
    name: str
    isin: str
    units: float
    gain: float  # EUR, as reported by eToro
    regime: str

    @property
    def rate(self) -> float:
        return exit_tax_rate(self.sold)

    @property
    def exit_tax(self) -> float:
        return max(self.gain, 0.0) * self.rate if self.regime == "Exit tax" else 0.0


@dataclass
class SummaryLine:
    label: str
    usd: float | None
    eur: float | None  # None where eToro reports "N/A"
    info_only: bool = False  # already included in another line


@dataclass
class AccountSummary:
    start: str
    end: str
    beginning: SummaryLine
    lines: list[SummaryLine]
    ending: SummaryLine
    unrealised_beginning: SummaryLine
    unrealised_ending: SummaryLine

    @property
    def reconciles(self) -> bool:
        moved = sum(l.usd or 0.0 for l in self.lines if not l.info_only)
        return abs(self.beginning.usd + moved - self.ending.usd) < 0.02


# Reported by eToro but already netted into Deposits, so excluded from the reconciliation.
SUMMARY_INFO_ONLY = {"Refunds", "Deposit/Withdrawal FX Conversion Fee"}


def load_account_summary(statement: Path) -> AccountSummary:
    def opt(text: str) -> float | None:
        return None if not text or text.strip().upper() == "N/A" else num(text)

    rows = {r.get("Details", "").strip(): r for r in read_sheet(statement, "Account Summary")}

    def line(label: str) -> SummaryLine:
        r = rows[label]
        return SummaryLine(label, opt(r.get("B")), opt(r.get("C")), label in SUMMARY_INFO_ONLY)

    labels = list(rows)
    first = labels.index("Beginning Realized Equity") + 1
    last = labels.index("Ending Realized Equity")
    lines = [line(l) for l in labels[first:last]]
    return AccountSummary(
        start=rows["Start Date"].get("B", "").split()[0],
        end=rows["End Date"].get("B", "").split()[0],
        beginning=line("Beginning Realized Equity"),
        lines=[l for l in lines if l.usd or l.eur],  # hide all-zero lines
        ending=line("Ending Realized Equity"),
        unrealised_beginning=line("Beginning Unrealized Equity"),
        unrealised_ending=line("Ending Unrealized Equity"),
    )


def load_open_lots(statement: Path) -> tuple[date, list[OpenLot]]:
    rows = [r for r in read_sheet(statement, "Holdings") if r.get("Type") == "ETF"]
    snapshot = max(r["Snapshot Date"] for r in rows)
    lots = []
    for r in rows:
        if r["Snapshot Date"] != snapshot:
            continue
        current_eur = num(r["Value in EUR"])
        open_rate, current_rate = num(r["Open Rate"]), num(r["Current Rate"])
        # Holdings has no historic FX, so cost is converted at the snapshot FX rate.
        purchase_eur = current_eur * open_rate / current_rate if current_rate else 0.0
        lots.append(OpenLot(
            position_id=r["Position ID"],
            purchased=etoro_datetime(r["Open Date"]),
            name=r["Asset"],
            isin=r.get("ISIN", ""),
            units=num(r["Units"]),
            purchase_value=purchase_eur,
            current_value=current_eur,
            regime=regime_for(r.get("ISIN", "")),
        ))
    lots.sort(key=lambda l: (l.deemed_disposal_date, l.name))
    return excel_date(snapshot), lots


def load_closed_lots(statement: Path) -> list[ClosedLot]:
    lots = []
    for r in read_sheet(statement, "Closed Positions"):
        if r.get("Type") != "ETF":
            continue
        lots.append(ClosedLot(
            position_id=r["Position ID"],
            purchased=etoro_datetime(r["Open Date"]),
            sold=etoro_datetime(r["Close Date"]),
            name=r["Action"],
            isin=r.get("ISIN", ""),
            units=num(r["Units / Contracts"]),
            gain=num(r["Profit(EUR)"]),
            regime=regime_for(r.get("ISIN", "")),
        ))
    lots.sort(key=lambda l: (l.sold, l.name))
    return lots


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_csv(path: Path, lots: list[OpenLot]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date purchased", "ETF name", "ISIN", "Units purchased",
                    "Purchase value (EUR)", "Current value (EUR)", "Gain/loss (EUR)",
                    "Deemed disposal date", "Tax regime", "Exit tax rate", "Exit tax (EUR)"])
        for l in lots:
            w.writerow([l.purchased.isoformat(), l.name, l.isin, f"{l.units:.6f}",
                        f"{l.purchase_value:.2f}", f"{l.current_value:.2f}", f"{l.gain:.2f}",
                        l.deemed_disposal_date.isoformat() if l.applies else "", l.regime,
                        f"{exit_tax_rate(l.deemed_disposal_date):.0%}" if l.applies else "",
                        f"{l.exit_tax:.2f}"])


def eur(x: float) -> str:
    return f"€{x:,.2f}" if x >= 0 else f"−€{-x:,.2f}"


def usd(x: float) -> str:
    return f"${x:,.2f}" if x >= 0 else f"−${-x:,.2f}"


def opt_money(x: float | None, fmt) -> str:
    return "<span class='sub'>n/a</span>" if x is None else fmt(x)


def account_panel(a: AccountSummary) -> str:
    e = html.escape

    def row(l: SummaryLine, cls: str = "") -> str:
        note = " <span class='sub'>(included in deposits)</span>" if l.info_only else ""
        signed = cls == "" and not l.info_only
        def cell(x, fmt):
            if x is None or not signed:
                return f"<td class='num'>{opt_money(x, fmt)}</td>"
            c = "pos" if x > 0.005 else "neg" if x < -0.005 else ""
            return f"<td class='num {c}'>{fmt(x)}</td>"
        return (f"<tr class='{cls}{' muted' if l.info_only else ''}'><td>{e(l.label)}{note}</td>"
                f"{cell(l.usd, usd)}{cell(l.eur, eur)}</tr>")

    body = (row(a.beginning, "total") + "".join(row(l) for l in a.lines) + row(a.ending, "total"))
    gain_usd = a.unrealised_ending.usd - a.ending.usd
    gain_eur = a.unrealised_ending.eur - a.ending.eur
    check = ("<span class='badge b-exit'>Reconciles ✓</span>" if a.reconciles
             else "<span class='badge b-check'>Does not reconcile</span>")
    return f"""
<h2>Account summary <span class="sub">{e(a.start)} – {e(a.end)}</span></h2>
<div class="split">
  <div class="wrap"><table>
    <thead><tr><th>Realised equity</th><th class="num">USD</th><th class="num">EUR</th></tr></thead>
    <tbody>{body}</tbody></table>
    <div class="foot">{check} <span class="sub">Beginning + movements = ending (USD). eToro gives no EUR value for
    deposits, transfers and withdrawals, so the EUR column doesn't add up on its own.</span></div>
  </div>
  <div class="stack">
    <div class="tile"><div class="k">Ending realised equity</div><div class="v">{eur(a.ending.eur)}</div>
      <div class="sub">{usd(a.ending.usd)}</div></div>
    <div class="tile"><div class="k">Ending unrealised equity</div><div class="v">{eur(a.unrealised_ending.eur)}</div>
      <div class="sub">{usd(a.unrealised_ending.usd)} · start {eur(a.unrealised_beginning.eur)}</div></div>
    <div class="tile"><div class="k">Unrealised gain on open positions</div>
      <div class="v {'pos' if gain_eur >= 0 else 'neg'}">{eur(gain_eur)}</div>
      <div class="sub">{usd(gain_usd)} · all asset types, not just ETFs</div></div>
  </div>
</div>"""


def gain_cell(x: float) -> str:
    cls = "pos" if x > 0.005 else "neg" if x < -0.005 else ""
    return f'<td class="num {cls}">{eur(x)}</td>'


def write_html(path: Path, snapshot: date, today: date, account: AccountSummary,
               open_lots: list[OpenLot], closed_lots: list[ClosedLot]) -> None:
    e = html.escape
    in_scope = [l for l in open_lots if l.applies]
    total_cost = sum(l.purchase_value for l in in_scope)
    total_value = sum(l.current_value for l in in_scope)
    total_tax = sum(l.exit_tax for l in in_scope)
    next_dd = min((l.deemed_disposal_date for l in in_scope), default=None)

    # Per-ETF summary
    summary: dict[str, dict] = {}
    for l in open_lots:
        s = summary.setdefault(l.isin, {"name": l.name, "regime": l.regime, "lots": 0,
                                        "units": 0.0, "cost": 0.0, "value": 0.0, "tax": 0.0,
                                        "first_dd": l.deemed_disposal_date})
        s["lots"] += 1
        s["units"] += l.units
        s["cost"] += l.purchase_value
        s["value"] += l.current_value
        s["tax"] += l.exit_tax
        s["first_dd"] = min(s["first_dd"], l.deemed_disposal_date)

    def badge(regime: str) -> str:
        cls = {"Exit tax": "b-exit", "CGT (US-domiciled)": "b-cgt"}.get(regime, "b-check")
        return f'<span class="badge {cls}">{e(regime)}</span>'

    summary_rows = "".join(
        f"<tr><td>{e(s['name'])}</td><td class='mono'>{e(isin)}</td><td>{badge(s['regime'])}</td>"
        f"<td class='num'>{s['lots']}</td><td class='num'>{s['units']:.6f}</td>"
        f"<td class='num'>{eur(s['cost'])}</td><td class='num'>{eur(s['value'])}</td>"
        f"{gain_cell(s['value'] - s['cost'])}"
        f"<td>{s['first_dd'].isoformat() if s['regime'] == 'Exit tax' else '—'}</td>"
        f"<td class='num'>{eur(s['tax'])}</td></tr>"
        for isin, s in summary.items()
    )

    lot_rows = []
    for l in open_lots:
        days = (l.deemed_disposal_date - today).days
        dd = (f"{l.deemed_disposal_date.isoformat()}<div class='sub'>{days / 365.25:.1f} yrs away</div>"
              if l.applies else "<span class='sub'>n/a</span>")
        rate = f"{exit_tax_rate(l.deemed_disposal_date):.0%}" if l.applies else "—"
        lot_rows.append(
            f"<tr class='{'' if l.applies else 'muted'}'>"
            f"<td>{l.purchased.isoformat()}</td><td>{e(l.name)}</td><td class='mono'>{e(l.isin)}</td>"
            f"<td class='num'>{l.units:.6f}</td><td class='num'>{eur(l.purchase_value)}</td>"
            f"<td class='num'>{eur(l.current_value)}</td>{gain_cell(l.gain)}"
            f"<td>{dd}</td><td>{badge(l.regime)}</td><td class='num'>{rate}</td>"
            f"<td class='num'>{eur(l.exit_tax)}</td></tr>")

    closed_rows = "".join(
        f"<tr class='{'' if l.regime == 'Exit tax' else 'muted'}'>"
        f"<td>{l.purchased.isoformat()}</td><td>{l.sold.isoformat()}</td><td>{e(l.name)}</td>"
        f"<td class='mono'>{e(l.isin)}</td><td class='num'>{l.units:.6f}</td>{gain_cell(l.gain)}"
        f"<td>{badge(l.regime)}</td><td class='num'>{f'{l.rate:.0%}' if l.regime == 'Exit tax' else '—'}</td>"
        f"<td class='num'>{eur(l.exit_tax)}</td></tr>"
        for l in closed_lots
    )
    closed_exit = [l for l in closed_lots if l.regime == "Exit tax"]
    closed_gain = sum(l.gain for l in closed_exit)
    closed_tax = sum(l.exit_tax for l in closed_exit)

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Irish Deemed Disposal Tracker</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{ --bg:#f7f8fa; --card:#fff; --ink:#1d2330; --sub:#6b7280; --line:#e5e7eb;
          --pos:#0f7b3f; --neg:#b42318; --accent:#16794c; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#111418; --card:#1a1f26; --ink:#e6e8eb; --sub:#9aa3ae; --line:#2b323c;
            --pos:#4cc38a; --neg:#f97066; --accent:#4cc38a; }} }}
  * {{ box-sizing:border-box }}
  body {{ margin:0; font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:var(--bg); color:var(--ink); }}
  main {{ max-width:1280px; margin:0 auto; padding:28px 20px 60px }}
  h1 {{ margin:0 0 4px; font-size:24px }} h2 {{ font-size:17px; margin:34px 0 10px }}
  .meta {{ color:var(--sub) }}
  .tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:12px; margin-top:20px }}
  .tile {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px }}
  .tile .k {{ color:var(--sub); font-size:12px; text-transform:uppercase; letter-spacing:.04em }}
  .tile .v {{ font-size:22px; font-weight:600; margin-top:4px; font-variant-numeric:tabular-nums }}
  .wrap {{ overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:10px }}
  table {{ border-collapse:collapse; width:100%; }}
  th, td {{ padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; vertical-align:top }}
  th {{ font-size:12px; color:var(--sub); font-weight:600; cursor:pointer; user-select:none; position:sticky; top:0; background:var(--card) }}
  tr:last-child td {{ border-bottom:0 }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums }}
  .mono {{ font-family:ui-monospace,Menlo,monospace; font-size:12.5px }}
  .pos {{ color:var(--pos) }} .neg {{ color:var(--neg) }}
  .sub {{ color:var(--sub); font-size:12px }}
  tr.muted td {{ opacity:.6 }}
  .badge {{ display:inline-block; padding:1px 8px; border-radius:999px; font-size:12px; border:1px solid currentColor }}
  .b-exit {{ color:var(--accent) }} .b-cgt {{ color:#2563eb }} .b-check {{ color:#b54708 }}
  .split {{ display:grid; grid-template-columns:minmax(0,2fr) minmax(220px,1fr); gap:12px; align-items:start }}
  @media (max-width:800px) {{ .split {{ grid-template-columns:1fr }} }}
  .stack {{ display:grid; gap:12px }}
  tr.total td {{ font-weight:600; background:color-mix(in srgb, var(--line) 35%, transparent) }}
  .foot {{ padding:10px; border-top:1px solid var(--line) }}
  .notes {{ color:var(--sub); font-size:13px; margin-top:30px }} .notes li {{ margin:4px 0 }}
</style></head>
<body><main>
<h1>Irish Deemed Disposal Tracker</h1>
<div class="meta">eToro holdings as of <b>{snapshot.isoformat()}</b> · report generated {today.isoformat()} ·
deemed disposal every {DEEMED_DISPOSAL_YEARS} years · exit tax {EXIT_TAX_NEW:.0%} on gains only</div>

<div class="tiles">
  <div class="tile"><div class="k">Exit-tax lots held</div><div class="v">{len(in_scope)}</div></div>
  <div class="tile"><div class="k">Purchase value</div><div class="v">{eur(total_cost)}</div></div>
  <div class="tile"><div class="k">Current value</div><div class="v">{eur(total_value)}</div></div>
  <div class="tile"><div class="k">Unrealised gain/loss</div><div class="v {'pos' if total_value >= total_cost else 'neg'}">{eur(total_value - total_cost)}</div></div>
  <div class="tile"><div class="k">Exit tax at current value</div><div class="v">{eur(total_tax)}</div></div>
  <div class="tile"><div class="k">Next deemed disposal</div><div class="v">{next_dd.isoformat() if next_dd else '—'}</div></div>
</div>

{account_panel(account)}

<h2>By ETF</h2>
<div class="wrap"><table class="sortable">
<thead><tr><th>ETF name</th><th>ISIN</th><th>Tax regime</th><th class="num">Lots</th><th class="num">Units</th>
<th class="num">Purchase value</th><th class="num">Current value</th><th class="num">Gain / loss</th>
<th>First deemed disposal</th><th class="num">Exit tax</th></tr></thead>
<tbody>{summary_rows}</tbody></table></div>

<h2>Open lots (each purchase has its own 8-year clock)</h2>
<div class="wrap"><table class="sortable">
<thead><tr><th>Date purchased</th><th>ETF name</th><th>ISIN</th><th class="num">Units purchased</th>
<th class="num">Purchase value</th><th class="num">Current value</th><th class="num">Gain / loss</th>
<th>Deemed disposal date</th><th>Tax regime</th><th class="num">Rate</th><th class="num">Exit tax</th></tr></thead>
<tbody>{''.join(lot_rows)}</tbody></table></div>

<h2>Closed in the statement period (actual disposals)</h2>
<div class="meta" style="margin-bottom:10px">Exit-tax funds sold: gain {eur(closed_gain)} ·
exit tax on gains only {eur(closed_tax)} (41% applies to disposals before 1 Jan 2026)</div>
<div class="wrap"><table class="sortable">
<thead><tr><th>Date purchased</th><th>Date sold</th><th>ETF name</th><th>ISIN</th><th class="num">Units</th>
<th class="num">Gain / loss</th><th>Tax regime</th><th class="num">Rate</th><th class="num">Exit tax</th></tr></thead>
<tbody>{closed_rows}</tbody></table></div>

<ul class="notes">
  <li><b>Deemed disposal date</b> = purchase date + {DEEMED_DISPOSAL_YEARS} years, tracked per lot. The tax at that date
      will depend on the value then; the figure shown is based on the current value.</li>
  <li><b>Exit tax</b> is charged on gains only. Losses give zero tax and cannot be offset against CGT gains.</li>
  <li><b>Purchase value</b> for open lots is units × open price, converted to EUR at the snapshot FX rate
      (the Holdings tab has no FX rate at purchase). Revenue expects the EUR value at the purchase date, so
      treat this as an estimate.</li>
  <li><b>US-domiciled ETFs</b> (ISIN starting US) are generally taxed under CGT at 33%, not exit tax, and have
      no deemed disposal. Non-EEA products such as Jersey ETCs (JE…) are usually CGT too — confirm with Revenue or an adviser.</li>
  <li>Closed-position gains are eToro's reported Profit (EUR). This is an aid, not tax advice.</li>
</ul>
</main>
<script>
document.querySelectorAll("table.sortable").forEach(t => {{
  t.querySelectorAll("th").forEach((th, i) => th.addEventListener("click", () => {{
    const body = t.tBodies[0], rows = [...body.rows], asc = th.dataset.asc !== "1";
    t.querySelectorAll("th").forEach(h => delete h.dataset.asc); th.dataset.asc = asc ? "1" : "0";
    const key = r => {{ const s = r.cells[i].innerText.trim().split("\\n")[0];
      const n = parseFloat(s.replace(/[€,%\\s]/g, "").replace("−", "-"));
      return isNaN(n) || /^\\d{{4}}-\\d\\d-\\d\\d/.test(s) ? s : n; }};
    rows.sort((a, b) => {{ const x = key(a), y = key(b);
      return (x > y ? 1 : x < y ? -1 : 0) * (asc ? 1 : -1); }});
    rows.forEach(r => body.appendChild(r));
  }}));
}});
</script>
</body></html>"""
    path.write_text(page, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("statement", nargs="?", type=Path, default=DEFAULT_STATEMENT)
    ap.add_argument("--out", type=Path, default=HERE / "output")
    args = ap.parse_args()

    snapshot, open_lots = load_open_lots(args.statement)
    closed_lots = load_closed_lots(args.statement)
    account = load_account_summary(args.statement)

    args.out.mkdir(parents=True, exist_ok=True)
    html_path = args.out / "deemed_disposal_tracker.html"
    csv_path = args.out / "deemed_disposal_tracker.csv"
    write_html(html_path, snapshot, date.today(), account, open_lots, closed_lots)
    write_csv(csv_path, open_lots)

    in_scope = [l for l in open_lots if l.applies]
    print(f"Snapshot {snapshot}: {len(open_lots)} open ETF lots ({len(in_scope)} under exit tax), "
          f"{len(closed_lots)} closed ETF lots")
    print(f"Exit-tax lots: cost {eur(sum(l.purchase_value for l in in_scope))}, "
          f"value {eur(sum(l.current_value for l in in_scope))}, "
          f"exit tax {eur(sum(l.exit_tax for l in in_scope))}")
    print(f"Account: ending realised {eur(account.ending.eur)}, unrealised "
          f"{eur(account.unrealised_ending.eur)}, reconciles={account.reconciles}")
    print(f"Wrote {html_path}\nWrote {csv_path}")


if __name__ == "__main__":
    main()
