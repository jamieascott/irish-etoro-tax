"""ECB euro reference rates (USD per EUR), downloaded once and cached as CSV.

Revenue accepts ECB reference rates for converting foreign-currency amounts. Rates
are published for TARGET business days only, so weekends and holidays use the
most recent earlier rate.
"""

import bisect
import csv
import io
import subprocess
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ECB_URL = ("https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
           "?startPeriod={start}&format=csvdata")
MAX_GAP_DAYS = 7  # Christmas/Easter closures are the longest gaps


class EcbRates:
    def __init__(self, cache: Path, needed_from: date, needed_to: date, offline: bool = False):
        self.cache = cache
        rates = self._read_cache()
        if not offline and (not rates or min(rates) > needed_from or max(rates) < min(needed_to, _last_business_day())):
            rates = self._download(min(needed_from, min(rates, default=needed_from)) - timedelta(days=14))
        if not rates:
            raise SystemExit(f"No ECB rates available (cache {cache}); run once with network access.")
        self.days = sorted(rates)
        self.rates = rates

    def usd_per_eur(self, on: date) -> float:
        i = bisect.bisect_right(self.days, on) - 1
        if i < 0 or (on - self.days[i]).days > MAX_GAP_DAYS:
            raise SystemExit(f"No ECB USD rate on or shortly before {on}; delete {self.cache} and re-run.")
        return self.rates[self.days[i]]

    def to_eur(self, usd: float, on: date) -> float:
        return usd / self.usd_per_eur(on)

    def _read_cache(self) -> dict[date, float]:
        if not self.cache.exists():
            return {}
        with self.cache.open() as f:
            return {date.fromisoformat(r["date"]): float(r["usd_per_eur"]) for r in csv.DictReader(f)}

    def _download(self, start: date) -> dict[date, float]:
        url = ECB_URL.format(start=start.isoformat())
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                text = resp.read().decode("utf-8")
        except urllib.error.URLError:
            # python.org builds on macOS ship without root certificates; curl uses the system store.
            text = subprocess.run(["curl", "-sSf", "--max-time", "60", url],
                                  capture_output=True, text=True, check=True).stdout
        rates = {date.fromisoformat(r["TIME_PERIOD"]): float(r["OBS_VALUE"])
                 for r in csv.DictReader(io.StringIO(text)) if r.get("OBS_VALUE")}
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        with self.cache.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "usd_per_eur"])
            for d in sorted(rates):
                w.writerow([d.isoformat(), rates[d]])
        return rates


def _last_business_day() -> date:
    d = date.today() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d
