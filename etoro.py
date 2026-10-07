"""etoro account statement (.xlsx) reading - standard library only."""

import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import cached_property
from pathlib import Path

XML_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
XML_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# Funds domiciled in the EU/EEA fall under the exit tax / deemed disposal regime.
# US-domiciled ETFs are generally taxed under CGT instead; others need checking.
EEA_ISIN_PREFIXES = {
    "IE", "LU", "DE", "FR", "NL", "AT", "BE", "DK", "FI", "SE", "ES", "PT",
    "IT", "GR", "CY", "MT", "EE", "LV", "LT", "PL", "CZ", "SK", "SI", "HU",
    "HR", "RO", "BG", "NO", "IS", "LI",
}


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

    if not rows:
        return []
    # Blank header cells fall back to the column letter so no column is dropped.
    header = rows[0]
    return [{header.get(k) or k: v for k, v in r.items()} for r in rows[1:]]


def excel_date(serial: str) -> date:
    return date(1899, 12, 30) + timedelta(days=int(float(serial)))


def etoro_datetime(text: str) -> date:
    return datetime.strptime(text.strip().split()[0], "%d/%m/%Y").date()


def num(text: str) -> float:
    try:
        return float(str(text).replace("%", "").strip())
    except (TypeError, ValueError):
        return 0.0


def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 Feb -> 28 Feb
        return d.replace(year=d.year + years, day=28)


def regime_for(isin: str) -> str:
    prefix = isin[:2].upper()
    if prefix in EEA_ISIN_PREFIXES:
        return "Exit tax"
    if prefix == "US":
        return "CGT (US-domiciled)"
    return "Check (non-EEA)"


@dataclass
class Statement:
    path: Path
    start: date
    end: date
    _cache: dict = field(default_factory=dict, repr=False)

    @property
    def year(self) -> int:
        return self.start.year

    def sheet(self, name: str) -> list[dict]:
        if name not in self._cache:
            self._cache[name] = read_sheet(self.path, name)
        return self._cache[name]

    @classmethod
    def open(cls, path: Path) -> "Statement":
        rows = {r.get("Details", "").strip(): r for r in read_sheet(path, "Account Summary")}
        return cls(path, etoro_datetime(rows["Start Date"]["B"]), etoro_datetime(rows["End Date"]["B"]))


@dataclass
class Opening:
    """An 'Open Position' row from Account Activity: what was actually paid, in USD."""
    amount_usd: float
    units: float


class History:
    """Everything that spans statements: what was paid for each position, and when
    each ISIN was bought (for the 4-week rule)."""

    def __init__(self, statements: list[Statement]):
        self.statements = sorted(statements, key=lambda s: s.start)
        self.openings: dict[str, Opening] = {}
        self.acquisitions: dict[str, list[tuple[date, str]]] = {}  # ISIN -> [(open date, position id)]
        seen: set[str] = set()
        for s in self.statements:
            for r in s.sheet("Account Activity"):
                if r.get("Type") == "Open Position":
                    self.openings[r["Position ID"]] = Opening(num(r["Amount"]), num(r["Units / Contracts"]))
            for sheet in ("Closed Positions", "Holdings"):
                for r in s.sheet(sheet):
                    pid, isin = r.get("Position ID", ""), r.get("ISIN", "")
                    if isin and pid not in seen:
                        seen.add(pid)
                        self.acquisitions.setdefault(isin, []).append((etoro_datetime(r["Open Date"]), pid))

    @cached_property
    def latest(self) -> Statement | None:
        return self.statements[-1] if self.statements else None

    def add_acquisition(self, isin: str, on: date, position_id: str) -> None:
        if isin:
            self.acquisitions.setdefault(isin, []).append((on, position_id))

    def bought_near(self, isin: str, on: date, days: int, exclude: str) -> bool:
        """Were units of this ISIN bought within `days` either side of `on` (other than `exclude`)?"""
        return any(abs((d - on).days) <= days and pid != exclude
                   for d, pid in self.acquisitions.get(isin, []))


def find_statements(folder: Path) -> list[Path]:
    return sorted(folder.glob("etoro-account-statement-*.xlsx"))
