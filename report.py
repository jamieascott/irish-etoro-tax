"""HTML dashboard and CSV output."""

import csv
import html
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import dividends as dv
from cgt import ANNUAL_EXEMPTION, CGT_RATE, CgtYear, Disposal, by_asset_type
from deemed_disposal import (DEEMED_DISPOSAL_YEARS, EXIT_TAX_NEW, AccountSummary, OpenLot,
                             SummaryLine, exit_tax_rate)

e = html.escape
FOUR_WEEK_BADGE = '<span class="badge b-check">4-week</span>'


@dataclass
class Report:
    today: date
    year: int
    snapshot: date | None             # None when there are no etoro statements
    statements: list[str]             # etoro statements and manual CSVs
    account: AccountSummary | None
    open_lots: list[OpenLot]
    cgt: list[CgtYear]                 # losses fully allowed
    cgt_conservative: list[CgtYear]    # 4-week-rule losses disallowed
    dividends: dict[int, list[dv.DividendTax]]
    rates: dv.TaxRates

    @property
    def cgt_latest(self) -> CgtYear:
        return self.cgt[-1]

    @property
    def cgt_latest_conservative(self) -> CgtYear:
        return self.cgt_conservative[-1]

    @property
    def etf_disposals(self) -> list[Disposal]:
        return [d for d in self.cgt_latest.disposals if d.regime == "Exit tax"]


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def eur(x: float) -> str:
    return f"€{x:,.2f}" if x >= -0.005 else f"−€{-x:,.2f}"


def usd(x: float) -> str:
    return f"${x:,.2f}" if x >= -0.005 else f"−${-x:,.2f}"


def opt_money(x: float | None, fmt) -> str:
    return "<span class='sub'>n/a</span>" if x is None else fmt(x)


def opt_signed_cell(x: float | None) -> str:
    return '<td class="num sub">—</td>' if x is None else signed_cell(x)


def opt_fmt(x: float | None) -> str:
    return "" if x is None else f"{x:.2f}"


def signed_cell(x: float) -> str:
    cls = "pos" if x > 0.005 else "neg" if x < -0.005 else ""
    return f'<td class="num {cls}">{eur(x)}</td>'


def money_cell(x: float) -> str:
    return f'<td class="num">{eur(x)}</td>'


def tile(label: str, value: str, sub: str = "", cls: str = "") -> str:
    sub = f'<div class="sub">{sub}</div>' if sub else ""
    return f'<div class="tile"><div class="k">{label}</div><div class="v {cls}">{value}</div>{sub}</div>'


def badge(regime: str) -> str:
    cls = {"Exit tax": "b-exit", "CGT (US-domiciled)": "b-cgt", "CGT": "b-cgt"}.get(regime, "b-check")
    return f'<span class="badge {cls}">{e(regime)}</span>'


def table(head: list[str], rows: list[str], numeric: set[int] = frozenset(), sortable: bool = True) -> str:
    ths = "".join(f"<th class='num'>{h}</th>" if i in numeric else f"<th>{h}</th>" for i, h in enumerate(head))
    return (f"<div class='wrap'><table class='{'sortable' if sortable else ''}'><thead><tr>{ths}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
def summary_tiles(r: Report) -> str:
    c, cc = r.cgt_latest, r.cgt_latest_conservative
    div = dv.totals(r.dividends.get(r.year, []))
    etf_tax = sum(max(d.gain, 0) * exit_tax_rate(d.closed) for d in r.etf_disposals)
    total = c.full.tax + etf_tax + div["irish_tax"]
    return f"""
<h2>{r.year} at a glance</h2>
<div class="tiles">
  {tile(f"CGT due 15 Dec {r.year}", eur(c.tax_initial), f"Jan–Nov disposals · conservative {eur(cc.tax_initial)}")}
  {tile(f"CGT due 31 Jan {r.year + 1}", eur(c.tax_later), f"December balance · conservative {eur(cc.tax_later)}")}
  {tile("Exit tax on ETF disposals", eur(etf_tax), f"{len(r.etf_disposals)} EEA ETF lots sold")}
  {tile("Tax on dividends", eur(div["irish_tax"]), f"after {eur(div['credit'])} foreign tax credit")}
  {tile(f"Total estimated for {r.year}", eur(total), f"conservative CGT: {eur(cc.full.tax + etf_tax + div['irish_tax'])}")}
  {tile(f"Losses to carry into {r.year + 1}", eur(c.full.carried_forward), f"conservative {eur(cc.full.carried_forward)}")}
</div>"""


def account_panel(a: AccountSummary) -> str:
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

    body = row(a.beginning, "total") + "".join(row(l) for l in a.lines) + row(a.ending, "total")
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
    <div class="foot">{check} <span class="sub">Beginning + movements = ending (USD). etoro gives no EUR value for
    deposits, transfers and withdrawals, so the EUR column doesn't add up on its own.</span></div>
  </div>
  <div class="stack">
    {tile("Ending realised equity", eur(a.ending.eur), usd(a.ending.usd))}
    {tile("Ending unrealised equity", eur(a.unrealised_ending.eur),
          f"{usd(a.unrealised_ending.usd)} · start {eur(a.unrealised_beginning.eur)}")}
    {tile("Unrealised gain on open positions", eur(gain_eur), f"{usd(gain_usd)} · all asset types, not just ETFs",
          "pos" if gain_eur >= 0 else "neg")}
  </div>
</div>"""


def cgt_section(r: Report) -> str:
    def year_rows(years: list[CgtYear]) -> list[str]:
        return [
            f"<tr><td>{y.year}</td><td class='num'>{len(y.chargeable)}</td>{money_cell(y.full.gains)}"
            f"{money_cell(-y.full.losses)}{money_cell(y.full.brought_forward)}{money_cell(y.full.losses_used_bf)}"
            f"{money_cell(y.full.exemption)}{money_cell(y.full.taxable)}<td class='num'><b>{eur(y.full.tax)}</b></td>"
            f"{money_cell(y.full.carried_forward)}</tr>"
            for y in years]

    head = ["Year", "Disposals", "Gains", "Losses", "Losses b/f", "B/f used", "Exemption", "Taxable",
            f"CGT {CGT_RATE:.0%}", "Losses c/f"]
    nums = set(range(1, len(head)))
    c, cc = r.cgt_latest, r.cgt_latest_conservative
    restricted = [d for d in c.chargeable if d.restricted]

    types = by_asset_type(c.disposals)
    type_rows = [
        f"<tr><td>{e(k)}</td><td class='num'>{s['count']}</td>{signed_cell(s['gains'])}{signed_cell(s['losses'])}"
        f"{signed_cell(s['ecb'])}{opt_signed_cell(s['etoro'])}"
        f"{opt_signed_cell(None if s['etoro'] is None else s['ecb'] - s['etoro'])}"
        f"{money_cell(-s['restricted'])}</tr>" for k, s in sorted(types.items())]

    disp_rows = [
        f"<tr class='{'muted' if d.regime != 'CGT' else ''}'><td>{d.closed.isoformat()}</td>"
        f"<td>{d.opened.isoformat()}</td><td>{e(d.name)}</td><td class='mono'>{e(d.isin)}</td><td>{e(d.asset_type)}</td>"
        f"<td>{e(d.source)}</td><td class='num'>{d.units:.6f}</td>"
        f"<td class='num'>{'—' if d.cost_usd is None else usd(d.cost_usd)}</td>"
        f"<td class='num'>{eur(d.cost_eur) if not d.margin_only else '—'}</td>"
        f"<td class='num'>{eur(d.proceeds_eur) if not d.margin_only else '—'}</td>{signed_cell(d.gain)}"
        f"{opt_signed_cell(d.profit_eur_etoro)}<td>{badge(d.regime)}"
        f"{' <span class=sub>' + e(d.regime_note) + '</span>' if d.regime_note else ''}</td>"
        f"<td>{FOUR_WEEK_BADGE if d.restricted else ''}</td></tr>"
        for d in c.disposals]

    return f"""
<h2>Capital Gains Tax</h2>
<div class="meta" style="margin-bottom:10px">Stocks, crypto, CFDs and non-EEA ETFs. {CGT_RATE:.0%} on gains after
current-year losses, losses brought forward and the {eur(ANNUAL_EXEMPTION)} annual exemption. EUR at ECB reference rates.</div>

<h3>By year: losses allowed in full</h3>
{table(head, year_rows(r.cgt), nums, sortable=False)}
<h3>By year: conservative (losses caught by the 4-week rule disallowed)</h3>
{table(head, year_rows(r.cgt_conservative), nums, sortable=False)}

<div class="tiles">
  {tile(f"{r.year} net gain", eur(c.full.gains - c.full.losses), cls="pos" if c.full.gains >= c.full.losses else "neg")}
  {tile("Initial period (Jan–Nov)", eur(c.tax_initial), f"pay by {c.due_initial.strftime('%-d %b %Y')}")}
  {tile("Later period (Dec)", eur(c.tax_later), f"pay by {c.due_later.strftime('%-d %b %Y')}"
        + (" · negative = overpaid in Dec" if c.tax_later < 0 else ""))}
  {tile("Losses flagged by 4-week rule", eur(c.restricted_losses), f"{len(restricted)} disposals")}
  {tile("Difference if restricted", eur(cc.full.tax - c.full.tax), "extra CGT in conservative case")}
</div>

<h3>{r.year} by asset type: ECB vs etoro's EUR figures</h3>
{table(["Asset type", "Disposals", "Gains", "Losses", "Net (ECB)", "Net (etoro EUR)", "FX difference", "4-week losses"],
       type_rows, set(range(1, 8)))}

<details><summary>All {len(c.disposals)} disposals in {r.year}</summary>
{table(["Closed", "Opened", "Instrument", "ISIN", "Type", "Source", "Units", "Cost (USD)", "Cost (EUR)",
        "Proceeds (EUR)", "Gain (ECB)", "etoro EUR", "Regime", "Flag"], disp_rows, {6, 7, 8, 9, 10, 11})}
</details>"""


def dividend_section(r: Report) -> str:
    years = sorted(r.dividends)
    yrows = []
    for y in years:
        t = dv.totals(r.dividends[y])
        yrows.append(f"<tr><td>{y}</td><td class='num'>{t['count']}</td>{money_cell(t['gross'])}{money_cell(t['wht'])}"
                     f"{money_cell(t['net'])}{money_cell(t['credit'])}{money_cell(t['excess'])}"
                     f"<td class='num'><b>{eur(t['irish_tax'])}</b></td></tr>")
    items = r.dividends.get(r.year, [])
    t = dv.totals(items)

    def grp_rows(groups: dict) -> list[str]:
        return [f"<tr><td>{e(k)}</td><td class='num'>{g['count']}</td>{money_cell(g['gross'])}{money_cell(g['wht'])}"
                f"{money_cell(g['credit'])}{money_cell(g['excess'])}{money_cell(g['income_tax'] - g['credit'])}"
                f"{money_cell(g['usc'])}{money_cell(g['prsi'])}{money_cell(g['exit_tax'])}"
                f"<td class='num'><b>{eur(g['irish_tax'])}</b></td></tr>" for k, g in groups.items()]

    def country(x: dv.DividendTax) -> str:
        rate = x.d.treaty_rate
        treaty = "exit tax" if x.d.category == "Fund distribution (exit tax)" else (
            "no treaty" if rate is None else f"treaty {rate:.0%}")
        return f"{x.d.country} ({treaty})"

    ghead = ["", "Payments", "Gross", "WHT", "Credit", "Excess WHT", "Income tax", "USC", "PRSI", "Exit tax", "Irish tax"]
    flagged = [x for x in items if x.flag]
    detail = [f"<tr><td>{x.d.paid.isoformat()}</td><td>{e(x.d.name)}</td><td class='mono'>{e(x.d.isin)}</td>"
              f"<td>{e(x.d.source)}</td><td>{e(x.d.category)}</td>{money_cell(x.d.gross)}<td class='num'>{x.d.wht_rate:.0%}</td>"
              f"{money_cell(x.d.wht)}{money_cell(x.credit)}{money_cell(x.irish_tax)}<td class='sub'>{e(x.flag)}</td></tr>"
              for x in items]
    prsi = (f"{r.rates.prsi:.1%}" if r.rates.prsi is not None else "4.1% to 30 Sep 2025, 4.2% from 1 Oct 2025")
    return f"""
<h2>Dividends</h2>
<div class="meta" style="margin-bottom:10px">Assumed marginal rates: income tax {r.rates.income_tax:.0%}, USC {r.rates.usc:.0%},
PRSI {prsi}. Change them with <code>--income-tax</code>, <code>--usc</code> and <code>--prsi</code>.
Foreign tax is credited against income tax only, up to the treaty rate.</div>
{table(["Year", "Payments", "Gross", "WHT", "Net received", "Credit", "Excess WHT", "Irish tax"], yrows,
       set(range(1, 8)), sortable=False)}

<div class="tiles">
  {tile(f"{r.year} gross dividends", eur(t["gross"]), f"net received {eur(t['net'])}")}
  {tile("Foreign tax withheld", eur(t["wht"]), f"credit allowed {eur(t['credit'])}")}
  {tile("Excess WHT", eur(t["excess"]), "above treaty rates; reclaim from source country")}
  {tile("Irish tax on dividends", eur(t["irish_tax"]),
        f"IT {eur(t['income_tax'] - t['credit'])} · USC {eur(t['usc'])} · PRSI {eur(t['prsi'])} · exit {eur(t['exit_tax'])}")}
</div>

<h3>{r.year} by category</h3>
{table(ghead, grp_rows(dv.group(items, lambda x: x.d.category)), set(range(1, 11)))}
<h3>{r.year} by country of the ISIN</h3>
{table(ghead, grp_rows(dv.group(items, country)), set(range(1, 11)))}

<details><summary>All {len(items)} payments in {r.year} ({len(flagged)} flagged)</summary>
{table(["Paid", "Instrument", "ISIN", "Source", "Category", "Gross", "WHT rate", "WHT", "Credit", "Irish tax",
        "Flag"], detail, {5, 6, 7, 8, 9})}
</details>"""


def deemed_disposal_section(r: Report) -> str:
    open_lots, today = r.open_lots, r.today
    in_scope = [l for l in open_lots if l.applies]
    total_cost = sum(l.purchase_value for l in in_scope)
    total_value = sum(l.current_value for l in in_scope)
    total_tax = sum(l.exit_tax for l in in_scope)
    next_dd = min((l.deemed_disposal_date for l in in_scope), default=None)
    estimated = sum(1 for l in open_lots if l.cost_estimated)

    summary: dict[str, dict] = {}
    for l in open_lots:
        s = summary.setdefault(l.isin, {"name": l.name, "regime": l.regime, "lots": 0, "units": 0.0, "cost": 0.0,
                                        "value": 0.0, "tax": 0.0, "first_dd": l.deemed_disposal_date})
        s["lots"] += 1
        s["units"] += l.units
        s["cost"] += l.purchase_value
        s["value"] += l.current_value
        s["tax"] += l.exit_tax
        s["first_dd"] = min(s["first_dd"], l.deemed_disposal_date)

    summary_rows = [
        f"<tr><td>{e(s['name'])}</td><td class='mono'>{e(isin)}</td><td>{badge(s['regime'])}</td>"
        f"<td class='num'>{s['lots']}</td><td class='num'>{s['units']:.6f}</td>"
        f"{money_cell(s['cost'])}{money_cell(s['value'])}{signed_cell(s['value'] - s['cost'])}"
        f"<td>{s['first_dd'].isoformat() if s['regime'] == 'Exit tax' else '—'}</td>{money_cell(s['tax'])}</tr>"
        for isin, s in summary.items()]

    lot_rows = []
    for l in open_lots:
        days = (l.deemed_disposal_date - today).days
        dd = (f"{l.deemed_disposal_date.isoformat()}<div class='sub'>{days / 365.25:.1f} yrs away</div>"
              if l.applies else "<span class='sub'>n/a</span>")
        rate = f"{exit_tax_rate(l.deemed_disposal_date):.0%}" if l.applies else "—"
        est = " <span class='sub'>est.</span>" if l.cost_estimated else ""
        lot_rows.append(
            f"<tr class='{'' if l.applies else 'muted'}'>"
            f"<td>{l.purchased.isoformat()}</td><td>{e(l.name)}</td><td class='mono'>{e(l.isin)}</td>"
            f"<td class='num'>{l.units:.6f}</td><td class='num'>{eur(l.purchase_value)}{est}</td>"
            f"{money_cell(l.current_value)}{signed_cell(l.gain)}"
            f"<td>{dd}</td><td>{badge(l.regime)}</td><td class='num'>{rate}</td>{money_cell(l.exit_tax)}</tr>")

    etf = r.etf_disposals
    closed_rows = [
        f"<tr><td>{d.opened.isoformat()}</td><td>{d.closed.isoformat()}</td><td>{e(d.name)}</td>"
        f"<td class='mono'>{e(d.isin)}</td><td>{e(d.source)}</td><td class='num'>{d.units:.6f}</td>{signed_cell(d.gain)}"
        f"{opt_signed_cell(d.profit_eur_etoro)}<td class='num'>{exit_tax_rate(d.closed):.0%}</td>"
        f"{money_cell(max(d.gain, 0) * exit_tax_rate(d.closed))}</tr>" for d in etf]
    closed_gain = sum(d.gain for d in etf)
    closed_tax = sum(max(d.gain, 0) * exit_tax_rate(d.closed) for d in etf)

    return f"""
<h2>ETFs: deemed disposal &amp; exit tax <span class="sub">{f"etoro holdings as of {r.snapshot.isoformat()}" if r.snapshot else "no etoro holdings loaded"}</span></h2>
<div class="tiles">
  {tile("Exit-tax lots held", str(len(in_scope)))}
  {tile("Purchase value", eur(total_cost), "ECB rate on each purchase date")}
  {tile("Current value", eur(total_value))}
  {tile("Unrealised gain/loss", eur(total_value - total_cost), cls="pos" if total_value >= total_cost else "neg")}
  {tile("Exit tax at current value", eur(total_tax))}
  {tile("Next deemed disposal", next_dd.isoformat() if next_dd else "—")}
</div>

<h3>By ETF</h3>
{table(["ETF name", "ISIN", "Tax regime", "Lots", "Units", "Purchase value", "Current value", "Gain / loss",
        "First deemed disposal", "Exit tax"], summary_rows, {3, 4, 5, 6, 7, 9})}

<h3>Open lots (each purchase has its own {DEEMED_DISPOSAL_YEARS}-year clock)</h3>
{table(["Date purchased", "ETF name", "ISIN", "Units", "Purchase value", "Current value", "Gain / loss",
        "Deemed disposal date", "Tax regime", "Rate", "Exit tax"], lot_rows, {3, 4, 5, 6, 9, 10})}
{f'<div class="sub" style="margin-top:6px">{estimated} lot(s) marked est. were bought before the earliest statement, so their cost comes from the open price.</div>' if estimated else ''}

<h3>EEA ETFs sold in {r.year} (actual disposals)</h3>
<div class="meta" style="margin-bottom:10px">Gain {eur(closed_gain)} · exit tax on gains only {eur(closed_tax)}.
Losses on exit-tax funds can't be offset against anything.</div>
{table(["Date purchased", "Date sold", "ETF name", "ISIN", "Source", "Units", "Gain (ECB)", "etoro EUR", "Rate",
        "Exit tax"], closed_rows, {5, 6, 7, 8, 9})}"""


NOTES = f"""
<ul class="notes">
  <li><b>FX:</b> etoro accounts are in USD. Cost is the USD amount invested converted at the ECB reference rate on
      the open date. Proceeds use the ECB rate on the close date. For CFDs, leveraged and short positions the stake is
      only margin, so the USD profit is converted at the close-date rate. Weekends and holidays use the previous ECB rate.</li>
  <li><b>Losses:</b> current-year losses are set against gains first, then losses brought forward, then the
      {eur(ANNUAL_EXEMPTION)} exemption. Carry-forward starts from the earliest statement supplied, so losses from earlier years aren't included.</li>
  <li><b>4-week rule:</b> a loss on shares is flagged if the same ISIN was bought within 4 weeks before or after
      the sale, in any statement supplied. The conservative figures disallow the whole loss. Strictly, only the
      matched quantity is restricted, and the loss can be used against a later gain on those shares.</li>
  <li><b>Not modelled:</b> FIFO share identification (etoro closes specific positions), CGT on holding USD cash
      (foreign currency is a chargeable asset), corporate actions, and fees and stamp duty outside etoro's profit figure.</li>
  <li><b>Dividends:</b> treaty rates are standard portfolio rates by ISIN country. ADRs show a US ISIN but the tax is
      withheld by the issuer's home country (e.g. Taiwan for TSMC), so check those flags.</li>
  <li><b>Deemed disposal:</b> every {DEEMED_DISPOSAL_YEARS} years per lot, at the value then. The figure here uses
      today's value. Exit tax {EXIT_TAX_NEW:.0%} from 2026 (41% before). US-domiciled ETFs are CGT.</li>
  <li>This is an aid for your own records, not tax advice. Always consult a qualified accountant for complex situations.</li>
</ul>"""

STYLE = """
  :root { --bg:#f7f8fa; --card:#fff; --ink:#1d2330; --sub:#6b7280; --line:#e5e7eb;
          --pos:#0f7b3f; --neg:#b42318; --accent:#16794c; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#111418; --card:#1a1f26; --ink:#e6e8eb; --sub:#9aa3ae; --line:#2b323c;
            --pos:#4cc38a; --neg:#f97066; --accent:#4cc38a; } }
  * { box-sizing:border-box }
  body { margin:0; font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:var(--bg); color:var(--ink); }
  main { max-width:1280px; margin:0 auto; padding:28px 20px 60px }
  h1 { margin:0 0 4px; font-size:24px } h2 { font-size:19px; margin:40px 0 10px }
  h3 { font-size:15px; margin:22px 0 8px }
  .meta { color:var(--sub) }
  .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:12px; margin:16px 0 }
  .tile { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px }
  .tile .k { color:var(--sub); font-size:12px; text-transform:uppercase; letter-spacing:.04em }
  .tile .v { font-size:22px; font-weight:600; margin-top:4px; font-variant-numeric:tabular-nums }
  .wrap { overflow:auto; max-height:640px; background:var(--card); border:1px solid var(--line); border-radius:10px }
  table { border-collapse:collapse; width:100%; }
  th, td { padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; vertical-align:top }
  th { font-size:12px; color:var(--sub); font-weight:600; position:sticky; top:0; background:var(--card) }
  table.sortable th { cursor:pointer; user-select:none }
  tr:last-child td { border-bottom:0 }
  .num { text-align:right; font-variant-numeric:tabular-nums }
  .mono { font-family:ui-monospace,Menlo,monospace; font-size:12.5px }
  .pos { color:var(--pos) } .neg { color:var(--neg) }
  .sub { color:var(--sub); font-size:12px }
  tr.muted td { opacity:.6 }
  .badge { display:inline-block; padding:1px 8px; border-radius:999px; font-size:12px; border:1px solid currentColor }
  .b-exit { color:var(--accent) } .b-cgt { color:#2563eb } .b-check { color:#b54708 }
  .split { display:grid; grid-template-columns:minmax(0,2fr) minmax(220px,1fr); gap:12px; align-items:start }
  @media (max-width:800px) { .split { grid-template-columns:1fr } }
  .stack { display:grid; gap:12px }
  tr.total td { font-weight:600; background:color-mix(in srgb, var(--line) 35%, transparent) }
  .foot { padding:10px; border-top:1px solid var(--line) }
  details { margin-top:14px } summary { cursor:pointer; font-weight:600; margin-bottom:8px }
  code { font-size:12.5px }
  .notes { color:var(--sub); font-size:13px; margin-top:34px } .notes li { margin:5px 0 }
"""

SCRIPT = """
document.querySelectorAll("table.sortable").forEach(t => {
  t.querySelectorAll("th").forEach((th, i) => th.addEventListener("click", () => {
    const body = t.tBodies[0], rows = [...body.rows], asc = th.dataset.asc !== "1";
    t.querySelectorAll("th").forEach(h => delete h.dataset.asc); th.dataset.asc = asc ? "1" : "0";
    const key = r => { const s = r.cells[i].innerText.trim().split("\\n")[0];
      const n = parseFloat(s.replace(/[€$,%\\s]/g, "").replace("−", "-"));
      return isNaN(n) || /^\\d{4}-\\d\\d-\\d\\d/.test(s) ? s : n; };
    rows.sort((a, b) => { const x = key(a), y = key(b);
      return (x > y ? 1 : x < y ? -1 : 0) * (asc ? 1 : -1); });
    rows.forEach(r => body.appendChild(r));
  }));
});
"""


def write_html(path: Path, r: Report) -> None:
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Irish etoro Tax Report {r.year}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{STYLE}</style></head>
<body><main>
<h1>Irish etoro Tax Report: {r.year}</h1>
<div class="meta">Sources: {e(", ".join(r.statements))} · generated {r.today.isoformat()} ·
EUR at ECB reference rates · not tax advice</div>
{summary_tiles(r)}
{cgt_section(r)}
{dividend_section(r)}
{deemed_disposal_section(r)}
{account_panel(r.account) if r.account else ''}
{NOTES}
</main>
<script>{SCRIPT}</script>
</body></html>"""
    path.write_text(page, encoding="utf-8")


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def write_deemed_disposal_csv(path: Path, lots: list[OpenLot]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date purchased", "ETF name", "ISIN", "Units", "Purchase value (EUR)", "Cost estimated",
                    "Current value (EUR)", "Gain/loss (EUR)", "Deemed disposal date", "Tax regime",
                    "Exit tax rate", "Exit tax (EUR)"])
        for l in lots:
            w.writerow([l.purchased.isoformat(), l.name, l.isin, f"{l.units:.6f}", f"{l.purchase_value:.2f}",
                        "yes" if l.cost_estimated else "", f"{l.current_value:.2f}", f"{l.gain:.2f}",
                        l.deemed_disposal_date.isoformat() if l.applies else "", l.regime,
                        f"{exit_tax_rate(l.deemed_disposal_date):.0%}" if l.applies else "", f"{l.exit_tax:.2f}"])


def write_disposals_csv(path: Path, years: list[CgtYear]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Tax year", "Closed", "Opened", "Instrument", "ISIN", "Type", "Source", "Leverage", "Short", "Units",
                    "Cost (USD)", "Profit (USD)", "Method", "Cost (EUR)", "Proceeds (EUR)", "Gain (EUR, ECB)",
                    "Profit (EUR, etoro)", "Regime", "Regime note", "4-week rule flag", "Payment period"])
        for y in years:
            for d in y.disposals:
                w.writerow([y.year, d.closed.isoformat(), d.opened.isoformat(), d.name, d.isin, d.asset_type,
                            d.source, d.leverage, "yes" if d.short else "", f"{d.units:.6f}", opt_fmt(d.cost_usd),
                            opt_fmt(d.profit_usd), d.method,
                            "" if d.margin_only else f"{d.cost_eur:.2f}",
                            "" if d.margin_only else f"{d.proceeds_eur:.2f}",
                            f"{d.gain:.2f}", opt_fmt(d.profit_eur_etoro), d.regime, d.regime_note,
                            "yes" if d.restricted else "", "Jan–Nov" if d.initial_period else "Dec"])


def write_dividends_csv(path: Path, by_year: dict[int, list[dv.DividendTax]]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Tax year", "Paid", "Instrument", "ISIN", "Source", "Country", "Type", "Category", "Net (EUR)",
                    "WHT rate", "WHT (EUR)", "Gross (EUR)", "Treaty rate", "Credit (EUR)", "Excess WHT (EUR)",
                    "Income tax (EUR)", "USC (EUR)", "PRSI (EUR)", "Exit tax (EUR)", "Irish tax (EUR)", "Flag"])
        for year in sorted(by_year):
            for x in by_year[year]:
                d = x.d
                w.writerow([year, d.paid.isoformat(), d.name, d.isin, d.source, d.country, d.asset_type, d.category,
                            f"{d.net:.4f}", f"{d.wht_rate:.4f}", f"{d.wht:.4f}", f"{d.gross:.4f}",
                            "" if d.treaty_rate is None else f"{d.treaty_rate:.2f}", f"{x.credit:.4f}",
                            f"{x.excess_wht:.4f}", f"{x.income_tax:.4f}", f"{x.usc:.4f}", f"{x.prsi:.4f}",
                            f"{x.exit_tax:.4f}", f"{x.irish_tax:.4f}", x.flag])
