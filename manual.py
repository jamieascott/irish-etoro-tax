"""Manual entries for brokers that aren't supported yet.

Reads the CSV the web app exports (Manual entry → Export CSV), so both tools give the
same totals. Validation matches the web app's.

Columns: record,broker,asset,isin,type,currency,acquired,disposed,units,cost,proceeds,fees,paid,net,wht
  record = disposal | dividend
  type   = Stocks | ETF | Crypto | CFD | Other  (disposals); Dividend | Fund distribution (dividends)
  currency = EUR | USD. USD is converted at the ECB rate: cost on the acquisition date,
  proceeds, fees and dividends on the disposal or payment date.
"""

import csv
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from cgt import Disposal, disposal_regime
from dividends import Dividend
from etoro import num
from fx import EcbRates

COLUMNS = ["record", "broker", "asset", "isin", "type", "currency", "acquired", "disposed", "units",
           "cost", "proceeds", "fees", "paid", "net", "wht"]
ASSET_TYPES = {"Stocks", "ETF", "Crypto", "CFD", "Other"}
ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
ISO_DATE = re.compile(r"^\d{4}-\d\d-\d\d$")


@dataclass
class ManualDisposal:
    id: str
    broker: str
    asset: str
    isin: str
    type: str
    currency: str
    acquired: date
    disposed: date
    units: float
    cost: float
    proceeds: float
    fees: float


@dataclass
class ManualDividend:
    id: str
    broker: str
    asset: str
    isin: str
    fund_distribution: bool
    currency: str
    paid: date
    net: float
    wht: float


class ManualError(ValueError):
    pass


def _date(text: str, what: str) -> date:
    text = (text or "").strip()
    try:
        if ISO_DATE.match(text):
            return date.fromisoformat(text)
    except ValueError:
        pass
    raise ManualError(f"enter a valid {what} date (YYYY-MM-DD)")


def _isin(text: str) -> str:
    isin = (text or "").strip().upper()
    if isin and not ISIN.match(isin):
        raise ManualError(f'"{isin}" isn\'t a valid ISIN (2 letters, 9 characters, 1 check digit)')
    return isin


def _amounts(*values: float) -> None:
    if any(v < 0 for v in values):
        raise ManualError("amounts must be zero or more")


def _parse_disposal(f: dict, row_id: str) -> ManualDisposal:
    asset = f["asset"].strip()
    if not asset:
        raise ManualError("enter an asset name")
    acquired, disposed = _date(f["acquired"], "acquisition"), _date(f["disposed"], "disposal")
    if disposed < acquired:
        raise ManualError("the disposal date is before the acquisition date")
    m = ManualDisposal(
        id=row_id, broker=f["broker"].strip(), asset=asset, isin=_isin(f["isin"]),
        type=f["type"].strip() if f["type"].strip() in ASSET_TYPES else "Other",
        currency="USD" if f["currency"].strip().upper() == "USD" else "EUR",
        acquired=acquired, disposed=disposed, units=num(f["units"]),
        cost=num(f["cost"]), proceeds=num(f["proceeds"]), fees=num(f["fees"]),
    )
    _amounts(m.cost, m.proceeds, m.fees)
    return m


def _parse_dividend(f: dict, row_id: str) -> ManualDividend:
    asset = f["asset"].strip()
    if not asset:
        raise ManualError("enter an instrument name")
    m = ManualDividend(
        id=row_id, broker=f["broker"].strip(), asset=asset, isin=_isin(f["isin"]),
        fund_distribution=f["type"].strip() == "Fund distribution",
        currency="USD" if f["currency"].strip().upper() == "USD" else "EUR",
        paid=_date(f["paid"], "payment"), net=num(f["net"]), wht=num(f["wht"]),
    )
    _amounts(m.net, m.wht)
    return m


def read_manual(path: Path) -> tuple[list[ManualDisposal], list[ManualDividend]]:
    """All-or-nothing, like the web app: any bad row stops the run with every problem listed."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    rows = [r for r in rows if any(c.strip() for c in r)]
    head = [h.strip().lower() for h in rows[0]] if rows else []
    missing = [c for c in COLUMNS if c not in head]
    if missing:
        raise SystemExit(f"{path.name}: missing column(s): {', '.join(missing)}")

    disposals, dividends, errors = [], [], []
    for n, r in enumerate(rows[1:], start=2):
        f = {h: (r[i] if i < len(r) else "") for i, h in enumerate(head)}
        kind, row_id = f["record"].strip().lower(), f"{path.stem}:{n}"
        try:
            if kind == "disposal":
                disposals.append(_parse_disposal(f, row_id))
            elif kind == "dividend":
                dividends.append(_parse_dividend(f, row_id))
            else:
                raise ManualError(f'unknown record "{f["record"]}"')
        except ManualError as e:
            errors.append(f"row {n}: {e}")
    if errors:
        raise SystemExit(f"{path.name}: nothing imported, fix these rows first:\n  " + "\n  ".join(errors))
    return disposals, dividends


def to_disposal(m: ManualDisposal, fx: EcbRates) -> Disposal:
    def eur(x: float, on: date) -> float:
        return fx.to_eur(x, on) if m.currency == "USD" else x

    cost_eur = eur(m.cost, m.acquired)
    proceeds_eur = eur(m.proceeds, m.disposed) - eur(m.fees, m.disposed)
    regime, note = disposal_regime(m.type, m.isin)
    return Disposal(
        position_id=m.id, name=m.asset, isin=m.isin, asset_type=m.type, opened=m.acquired, closed=m.disposed,
        units=m.units, leverage=1, short=False,
        cost_usd=m.cost if m.currency == "USD" else None, profit_usd=None, profit_eur_etoro=None,
        cost_eur=cost_eur, proceeds_eur=proceeds_eur, gain=proceeds_eur - cost_eur,
        regime=regime, regime_note=note or ("Check treatment" if m.type == "Other" else ""),
        source=m.broker or "Manual",
    )


def to_dividend(m: ManualDividend, fx: EcbRates) -> Dividend:
    def eur(x: float) -> float:
        return fx.to_eur(x, m.paid) if m.currency == "USD" else x

    net, wht = eur(m.net), eur(m.wht)
    return Dividend(
        paid=m.paid, name=m.asset, isin=m.isin, asset_type="Stocks", position_id=m.id, net=net, wht=wht,
        wht_rate=wht / (net + wht) if net + wht else 0.0, source=m.broker or "Manual",
        fund_distribution=m.fund_distribution,
    )
