"""Irish Capital Gains Tax on etoro closed positions.

- Gains in EUR at ECB rates: cost at the rate on the open date, proceeds at the rate
  on the close date (long, unleveraged positions). For CFDs, leveraged and short
  positions the stake is only margin, so the USD profit is converted at the close rate.
- 33% on net gains after current-year losses, then losses brought forward, then the
  €1,270 annual personal exemption. Unused losses carry forward.
- EEA-domiciled ETFs are excluded (exit tax regime, see deemed_disposal.py).
- Four-week rule (s.581 TCA 1997): a loss on shares is restricted if shares of the same
  class are acquired within 4 weeks before or after the disposal. Flagged per disposal and
  shown as a conservative scenario that ignores those losses.
"""

from dataclasses import dataclass, field
from datetime import date

from etoro import History, Statement, etoro_datetime, num, regime_for
from fx import EcbRates

CGT_RATE = 0.33
ANNUAL_EXEMPTION = 1270.0
FOUR_WEEKS = 28
# Disposals 1 Jan–30 Nov: pay by 15 Dec. Disposals in December: pay by 31 Jan.
INITIAL_PERIOD_END = (11, 30)


@dataclass
class Disposal:
    position_id: str
    name: str
    isin: str
    asset_type: str
    opened: date
    closed: date
    units: float
    leverage: int
    short: bool
    cost_usd: float | None          # None for manual EUR entries
    profit_usd: float | None        # etoro only
    profit_eur_etoro: float | None  # etoro only
    cost_eur: float
    proceeds_eur: float
    gain: float          # EUR, ECB rates
    regime: str          # "CGT" or "Exit tax"
    regime_note: str = ""
    restricted: bool = False  # loss caught by the 4-week rule
    source: str = "etoro"     # or the broker of a manual entry

    @property
    def method(self) -> str:
        return "Profit at close rate" if self.margin_only else "Cost/proceeds"

    @property
    def margin_only(self) -> bool:
        return self.source == "etoro" and (self.asset_type == "CFD" or self.leverage != 1 or self.short)

    @property
    def initial_period(self) -> bool:
        return (self.closed.month, self.closed.day) <= INITIAL_PERIOD_END


def disposal_regime(asset_type: str, isin: str) -> tuple[str, str]:
    if asset_type != "ETF":
        return "CGT", ""
    r = regime_for(isin)
    if r == "Exit tax":
        return "Exit tax", ""
    if r.startswith("CGT"):
        return "CGT", "US-domiciled ETF"
    return "CGT", "Non-EEA product, check treatment"


def load_disposals(statement: Statement, history: History, fx: EcbRates) -> list[Disposal]:
    out = []
    for r in statement.sheet("Closed Positions"):
        opened, closed = etoro_datetime(r["Open Date"]), etoro_datetime(r["Close Date"])
        cost_usd, profit_usd = num(r["Amount"]), num(r["Profit(USD)"])
        asset_type, isin = r.get("Type", ""), r.get("ISIN", "")
        leverage = int(num(r.get("Leverage", "1")) or 1)
        short = r.get("Long / Short", "Long").strip().lower() == "short"
        regime, note = disposal_regime(asset_type, isin)
        d = Disposal(
            position_id=r["Position ID"], name=r["Action"], isin=isin, asset_type=asset_type,
            opened=opened, closed=closed, units=num(r["Units / Contracts"]),
            leverage=leverage, short=short, cost_usd=cost_usd, profit_usd=profit_usd,
            profit_eur_etoro=num(r["Profit(EUR)"]), cost_eur=0.0, proceeds_eur=0.0, gain=0.0,
            regime=regime, regime_note=note,
        )
        if d.margin_only:
            d.gain = fx.to_eur(profit_usd, closed)
        else:
            d.cost_eur = fx.to_eur(cost_usd, opened)
            d.proceeds_eur = fx.to_eur(cost_usd + profit_usd, closed)
            d.gain = d.proceeds_eur - d.cost_eur
        flag_four_week(d, history)
        out.append(d)
    out.sort(key=lambda d: (d.closed, d.name))
    return out


def flag_four_week(d: Disposal, history: History) -> None:
    # The 4-week rule applies to shares (incl. ETF units), not crypto or CFDs. Exit-tax
    # funds get no loss relief at all, so there's nothing to restrict.
    if d.gain < 0 and d.regime == "CGT" and d.asset_type in ("Stocks", "ETF") and d.isin:
        d.restricted = history.bought_near(d.isin, d.closed, FOUR_WEEKS, exclude=d.position_id)


@dataclass
class CgtComputation:
    gains: float
    losses: float           # allowable this year
    brought_forward: float
    losses_used_bf: float
    exemption: float
    taxable: float
    tax: float
    carried_forward: float


def compute(gains: float, losses: float, brought_forward: float) -> CgtComputation:
    net = gains - losses
    if net <= 0:
        return CgtComputation(gains, losses, brought_forward, 0.0, 0.0, 0.0, 0.0, brought_forward - net)
    used = min(brought_forward, net)
    net -= used
    exemption = min(ANNUAL_EXEMPTION, net)
    taxable = net - exemption
    return CgtComputation(gains, losses, brought_forward, used, exemption, taxable,
                          taxable * CGT_RATE, brought_forward - used)


@dataclass
class CgtYear:
    year: int
    disposals: list[Disposal]
    conservative: bool                       # 4-week-rule losses disallowed
    brought_forward: float
    full: CgtComputation = field(init=False)
    initial: CgtComputation = field(init=False)  # Jan–Nov only, for the 15 Dec payment

    def __post_init__(self):
        self.full = self._compute(self.chargeable)
        self.initial = self._compute([d for d in self.chargeable if d.initial_period])

    @property
    def chargeable(self) -> list[Disposal]:
        return [d for d in self.disposals if d.regime == "CGT"]

    @property
    def restricted_losses(self) -> float:
        return -sum(d.gain for d in self.chargeable if d.restricted)

    def _compute(self, ds: list[Disposal]) -> CgtComputation:
        gains = sum(d.gain for d in ds if d.gain > 0)
        losses = -sum(d.gain for d in ds if d.gain < 0 and not (self.conservative and d.restricted))
        return compute(gains, losses, self.brought_forward)

    @property
    def tax_initial(self) -> float:
        return self.initial.tax

    @property
    def tax_later(self) -> float:
        """December balance due 31 Jan. Negative means the 15 Dec payment was too high."""
        return self.full.tax - self.initial.tax

    @property
    def due_initial(self) -> date:
        return date(self.year, 12, 15)

    @property
    def due_later(self) -> date:
        return date(self.year + 1, 1, 31)


def cgt_years(disposals_by_year: dict[int, list[Disposal]], conservative: bool,
              opening_losses: float = 0.0) -> list[CgtYear]:
    """Chain years in order so unused losses carry forward."""
    out, carry = [], opening_losses
    for year in sorted(disposals_by_year):
        y = CgtYear(year, disposals_by_year[year], conservative, carry)
        out.append(y)
        carry = y.full.carried_forward
    return out


def by_asset_type(disposals: list[Disposal]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for d in disposals:
        key = d.asset_type if d.regime == "CGT" else "ETF (exit tax)"
        s = out.setdefault(key, {"count": 0, "gains": 0.0, "losses": 0.0, "restricted": 0.0,
                                 "etoro": None, "ecb": 0.0})
        s["count"] += 1
        s["gains" if d.gain >= 0 else "losses"] += d.gain
        if d.restricted:
            s["restricted"] += d.gain
        if d.profit_eur_etoro is not None:
            s["etoro"] = (s["etoro"] or 0.0) + d.profit_eur_etoro
        s["ecb"] += d.gain
    return out
