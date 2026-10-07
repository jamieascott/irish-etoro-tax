"""ETF lots under the exit tax regime: 8-year deemed disposal and exit tax.
Also the account summary panel."""

from dataclasses import dataclass
from datetime import date

from etoro import History, Statement, add_years, excel_date, etoro_datetime, num, regime_for
from fx import EcbRates

DEEMED_DISPOSAL_YEARS = 8
# Exit tax: 41% for disposals up to 31 Dec 2025, 38% from 1 Jan 2026 (Budget 2026).
EXIT_TAX_RATE_CHANGE = date(2026, 1, 1)
EXIT_TAX_OLD, EXIT_TAX_NEW = 0.41, 0.38


def exit_tax_rate(on: date) -> float:
    return EXIT_TAX_NEW if on >= EXIT_TAX_RATE_CHANGE else EXIT_TAX_OLD


@dataclass
class OpenLot:
    position_id: str
    purchased: date
    name: str
    isin: str
    units: float
    purchase_value: float  # EUR, cost at the ECB rate on the purchase date
    current_value: float   # EUR, at the ECB rate on the snapshot date
    regime: str
    cost_estimated: bool   # no Open Position row found, so cost derived from the open price

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


def load_open_lots(statement: Statement, history: History, fx: EcbRates) -> tuple[date, list[OpenLot]]:
    rows = [r for r in statement.sheet("Holdings") if r.get("Type") == "ETF"]
    if not rows:
        return statement.end, []
    snapshot = max(rows, key=lambda r: num(r["Snapshot Date"]))["Snapshot Date"]
    snap_date = excel_date(snapshot)
    lots = []
    for r in rows:
        if r["Snapshot Date"] != snapshot:
            continue
        purchased = etoro_datetime(r["Open Date"])
        units, value_usd = num(r["Units"]), num(r["Value in USD"])
        opening = history.openings.get(r["Position ID"])
        if opening and opening.units:
            # Scale for partial closes: the remaining units carry their share of the cost.
            cost_usd = opening.amount_usd * units / opening.units
        else:
            open_rate, current_rate = num(r["Open Rate"]), num(r["Current Rate"])
            cost_usd = value_usd * open_rate / current_rate if current_rate else 0.0
        lots.append(OpenLot(
            position_id=r["Position ID"],
            purchased=purchased,
            name=r["Asset"],
            isin=r.get("ISIN", ""),
            units=units,
            purchase_value=fx.to_eur(cost_usd, purchased),
            current_value=fx.to_eur(value_usd, snap_date),
            regime=regime_for(r.get("ISIN", "")),
            cost_estimated=opening is None,
        ))
    lots.sort(key=lambda l: (l.deemed_disposal_date, l.name))
    return snap_date, lots


# --------------------------------------------------------------------------- #
# Account summary
# --------------------------------------------------------------------------- #
@dataclass
class SummaryLine:
    label: str
    usd: float | None
    eur: float | None  # None where etoro reports "N/A"
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


# Reported by etoro but already netted into Deposits, so excluded from the reconciliation.
SUMMARY_INFO_ONLY = {"Refunds", "Deposit/Withdrawal FX Conversion Fee"}


def load_account_summary(statement: Statement) -> AccountSummary:
    def opt(text: str) -> float | None:
        return None if not text or text.strip().upper() == "N/A" else num(text)

    rows = {r.get("Details", "").strip(): r for r in statement.sheet("Account Summary")}

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
