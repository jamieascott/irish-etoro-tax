# irish-etoro-tax

Works out Irish tax on investments from etoro annual account statements
(`../etoro-account-statement-1-1-YYYY-12-31-YYYY.xlsx`). Built for Irish retail investors.
**Not tax advice.** Always consult a qualified accountant for complex situations.

There are two ways to use it:

- **Web demo:** open `index.html`, the landing page, then click through to
  `irish_etoro_tax_assistant.html`. Upload etoro statements, add disposals and dividends from other brokers
  under **Manual entry**, or do both. Everything runs in the browser and nothing is uploaded. The page uses the
  same rules as the local report: ECB FX, CGT with carry-forward and 4-week flags, dividends, and exit tax.
  Manual entries can be exported and imported as CSV. Run `python3 build_web_rates.py` to refresh the bundled
  `ecb_rates.js`. Without it, the page downloads any newer rates it needs from the ECB.
- **Local report:** `irish_tax_report.py` builds a fuller HTML dashboard and CSVs from the statements on disk.

## Local report

```sh
python3 irish_tax_report.py                  # every statement in ../, report for the latest year
python3 irish_tax_report.py --income-tax 0.2 --usc 0.03   # change the dividend tax rates
python3 irish_tax_report.py --losses-bf 250  # CGT losses from before the earliest statement
python3 irish_tax_report.py --manual other-brokers.csv   # add trades from other brokers (repeatable)
python3 irish_tax_report.py --offline        # use cached ECB rates only
open output/irish_tax_report.html
```

Outputs in `output/`: `irish_tax_report.html`, `cgt_disposals.csv` (every disposal, all years),
`dividends.csv`, `deemed_disposal_lots.csv`. It uses only the Python standard library.

| Module | What it does |
|---|---|
| `etoro.py` | xlsx reader, `Statement`, and `History`, which holds the amount paid per position and acquisition dates across statements |
| `fx.py` | ECB USD/EUR reference rates, downloaded once and cached in `fx/ecb_usd_per_eur.csv` |
| `cgt.py` | Disposals in EUR, the 4-week rule, the yearly CGT computation with loss carry-forward |
| `dividends.py` | Gross-up, treaty credit, income tax/USC/PRSI, exit tax on fund distributions |
| `deemed_disposal.py` | ETF lots, the 8-year deemed disposal, exit tax, the account summary |
| `manual.py` | Manual entries for other brokers, from the web app's CSV export |
| `report.py` | HTML and CSV output |

### Other brokers (`--manual`)

The local report reads the same CSV the web app exports from **Manual entry → Export CSV**, so both tools give
the same totals. You can also write the file by hand. The web app's **Download blank template** gives an example, and `examples/sample-manual-entries.csv` is a made-up set of entries to try.

```
record,broker,asset,isin,type,currency,acquired,disposed,units,cost,proceeds,fees,paid,net,wht
disposal,Trading 212,Apple Inc,US0378331005,Stocks,EUR,2024-03-01,2025-06-15,10,1550.00,1820.00,1.50,,,
dividend,Trading 212,Apple Inc,US0378331005,Dividend,EUR,,,,,,,2025-05-15,2.12,0.38
```

- `type`: `Stocks`, `ETF`, `Crypto`, `CFD` or `Other` for disposals. `Dividend` or `Fund distribution` for dividends.
- `currency`: `EUR`, or `USD`, which is converted at the ECB rate. Cost uses the acquisition date. Proceeds,
  fees and dividends use the disposal or payment date.
- Cost should include buying fees and stamp duty. `fees` means selling fees.
- Manual entries are combined with etoro data by tax year. The ISIN is used for the 4-week rule across brokers,
  and an EU/EEA ISIN on an ETF means exit tax.
- Validation is all-or-nothing: any bad row stops the run, and every problem is listed.
- With no etoro statements in `../`, the report runs on manual entries alone.

### How it calculates

- **FX:** cost = USD amount invested ÷ ECB rate on the open date. Proceeds = (amount + profit) ÷ ECB rate on
  the close date. For CFDs, leveraged and short positions the stake is only margin, so profit ÷ ECB rate on the
  close date. Open ETF lots use the real amount paid from Account Activity, scaled for partial closes.
- **CGT:** 33% on gains less current-year losses, less losses brought forward, less the €1,270 exemption.
  The tax is split into Jan–Nov (pay by 15 Dec) and December (pay by 31 Jan). There are two scenarios: losses
  allowed in full, and a conservative one where losses caught by the 4-week rule are disallowed. A loss is
  caught if the same ISIN was bought within 28 days either side of the sale.
- **Dividends:** gross = net + withholding tax (WHT). The credit is the lowest of WHT, the treaty rate × gross,
  and the income tax due. WHT above the treaty rate is shown as reclaimable from the source country.
  Irish/EEA fund distributions are taxed at exit tax. Default rates are 40% income tax, 8% USC, and PRSI at
  the rate on the payment date.
- **Exit tax:** 41% for disposals up to 2025, 38% from 2026. EEA ETFs are excluded from CGT. US ETFs are taxed under CGT.

### Not modelled yet

- FIFO share identification (etoro closes specific positions)
- CGT on holding USD cash
- Corporate actions
- Partial-quantity matching for the 4-week rule
- Losses from before the earliest statement (use `--losses-bf`)
- ADR dividends, which are withheld by the issuer's home country but show a US ISIN

`archive/` holds the originals: the single-file ETF tracker, replaced by `irish_tax_report.py`, and the first web page (v1), which used etoro's own EUR figures.
