"""Dividend income and withholding tax (WHT) credits.

- Gross dividend = net received + WHT (etoro's EUR figures).
- Irish company dividends: Irish dividend withholding tax (DWT) is credited in full.
- Foreign company dividends: income tax at your marginal rate, plus USC and PRSI.
  WHT is credited against income tax only, up to the treaty rate for the source
  country. Anything withheld above the treaty rate is not creditable in Ireland,
  but can usually be reclaimed from the source country.
- Distributions from EEA funds (e.g. Irish-domiciled ETFs) are taxed at the exit tax
  rate instead, with no USC/PRSI and no credit.
- Dividend equivalents on CFDs are not real dividends: taxed as income, with no credit.
"""

from dataclasses import dataclass
from datetime import date

from deemed_disposal import exit_tax_rate
from etoro import Statement, etoro_datetime, num, regime_for

# Portfolio dividend WHT rates under Ireland's double taxation agreements, by the
# country in the ISIN. 0 = treaty exists but the country doesn't withhold (or 0%).
# Countries not listed: no treaty assumed, so no credit (check).
TREATY_RATES = {
    "US": 0.15, "CA": 0.15, "ES": 0.15, "NO": 0.15, "FR": 0.15, "DK": 0.15, "CH": 0.15,
    "DE": 0.15, "NL": 0.15, "FI": 0.15, "SE": 0.15, "LU": 0.15, "IT": 0.15, "BE": 0.15,
    "PT": 0.15, "AT": 0.10, "JP": 0.15, "AU": 0.15, "NZ": 0.15, "GB": 0.15, "AE": 0.0,
    "IL": 0.15, "KR": 0.15, "SG": 0.0, "HK": 0.0, "CN": 0.10, "IN": 0.10,
}


@dataclass
class TaxRates:
    income_tax: float = 0.40
    usc: float = 0.08
    prsi: float | None = None  # None = the PRSI rate in force on the payment date

    def prsi_on(self, on: date) -> float:
        if self.prsi is not None:
            return self.prsi
        if on >= date(2025, 10, 1):
            return 0.042
        if on >= date(2024, 10, 1):
            return 0.041
        return 0.04


@dataclass
class Dividend:
    paid: date
    name: str
    isin: str
    asset_type: str
    position_id: str
    net: float       # EUR
    wht: float       # EUR
    wht_rate: float  # as reported, e.g. 0.15
    source: str = "etoro"
    fund_distribution: bool = False  # manual entries say so explicitly

    @property
    def gross(self) -> float:
        return self.net + self.wht

    @property
    def country(self) -> str:
        return self.isin[:2].upper() if self.isin else "??"

    @property
    def category(self) -> str:
        if self.asset_type.upper() == "CFD":
            return "CFD dividend equivalent"
        if self.fund_distribution or (self.asset_type.upper() == "ETF" and regime_for(self.isin) == "Exit tax"):
            return "Fund distribution (exit tax)"
        return "Foreign dividend"

    @property
    def treaty_rate(self) -> float | None:
        if self.category != "Foreign dividend":
            return None
        return TREATY_RATES.get(self.country)


@dataclass
class DividendTax:
    d: Dividend
    income_tax: float
    credit: float
    usc: float
    prsi: float
    exit_tax: float

    @property
    def excess_wht(self) -> float:
        """Withheld above the treaty rate: not creditable here, possibly reclaimable abroad."""
        return max(self.d.wht - self.credit, 0.0) if self.d.category == "Foreign dividend" else self.d.wht

    @property
    def irish_tax(self) -> float:
        return self.income_tax - self.credit + self.usc + self.prsi + self.exit_tax

    @property
    def flag(self) -> str:
        d = self.d
        if d.category == "Fund distribution (exit tax)":
            return ""
        if d.category == "CFD dividend equivalent":
            return "CFD: etoro deduction is not a foreign tax, no credit"
        if d.country == "IE":
            return "Irish DWT: credited in full" if d.wht else ""
        if d.treaty_rate is None:
            return f"No treaty rate for {d.country}, so no credit. Check" if d.wht else ""
        if d.wht_rate > d.treaty_rate + 0.005:
            return f"WHT {d.wht_rate:.0%} above treaty {d.treaty_rate:.0%}, excess reclaimable at source"
        return ""


def assess(d: Dividend, rates: TaxRates) -> DividendTax:
    if d.category == "Fund distribution (exit tax)":
        return DividendTax(d, 0.0, 0.0, 0.0, 0.0, d.gross * exit_tax_rate(d.paid))
    income_tax = d.gross * rates.income_tax
    credit = 0.0
    if d.country == "IE":  # Irish dividend withholding tax is credited in full
        credit = d.wht
    elif d.treaty_rate is not None:
        credit = min(d.wht, d.treaty_rate * d.gross, income_tax)
    return DividendTax(d, income_tax, credit, d.gross * rates.usc, d.gross * rates.prsi_on(d.paid), 0.0)


def load_dividends(statement: Statement) -> list[Dividend]:
    out = []
    for r in statement.sheet("Dividends"):
        if not r.get("Date of Payment"):
            continue
        out.append(Dividend(
            paid=etoro_datetime(r["Date of Payment"]),
            name=r.get("Instrument Name", ""),
            isin=r.get("ISIN", ""),
            asset_type=r.get("Type", ""),
            position_id=r.get("Position ID", ""),
            net=num(r.get("Net Dividend Received (EUR)")),
            wht=num(r.get("Withholding Tax Amount (EUR)")),
            wht_rate=num(r.get("Withholding Tax Rate (%)")) / 100,
        ))
    out.sort(key=lambda d: (d.paid, d.name))
    return out


def totals(items: list[DividendTax]) -> dict[str, float]:
    return {
        "count": len(items),
        "net": sum(t.d.net for t in items),
        "wht": sum(t.d.wht for t in items),
        "gross": sum(t.d.gross for t in items),
        "income_tax": sum(t.income_tax for t in items),
        "credit": sum(t.credit for t in items),
        "excess": sum(t.excess_wht for t in items),
        "usc": sum(t.usc for t in items),
        "prsi": sum(t.prsi for t in items),
        "exit_tax": sum(t.exit_tax for t in items),
        "irish_tax": sum(t.irish_tax for t in items),
    }


def group(items: list[DividendTax], key) -> dict[str, dict[str, float]]:
    groups: dict[str, list[DividendTax]] = {}
    for t in items:
        groups.setdefault(key(t), []).append(t)
    return {k: totals(v) for k, v in sorted(groups.items(), key=lambda kv: -totals(kv[1])["gross"])}
