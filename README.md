# irish-investment-tax

A mini project for working out Irish tax on investments, starting from an eToro
account statement (`../etoro-account-statement-1-1-2025-12-31-2025.xlsx`).

## Irish Deemed Disposal Tracker

`deemed_disposal_tracker.py` reads the **Holdings** and **Closed Positions** tabs and
builds an HTML dashboard and a CSV. For each ETF lot it shows:

- Date purchased, ETF name, ISIN, units purchased
- Purchase value and current value (EUR)
- Gain or loss
- 8-year deemed disposal date (each purchase has its own clock)
- Exit tax at 38% on gains only (41% for disposals before 1 Jan 2026)

It also lists the ETFs sold during the statement period (actual disposals), and has an
**account summary** panel from the Account Summary tab. That panel shows the realised
equity movement in USD and EUR, checks that it reconciles, and gives the unrealised
equity and unrealised gain on all open positions.

```sh
python3 deemed_disposal_tracker.py            # uses ../etoro-account-statement-…xlsx
python3 deemed_disposal_tracker.py other.xlsx --out some/dir
open output/deemed_disposal_tracker.html
```

It uses only the Python standard library, so there's nothing to install.

### Assumptions and limitations

- **Current value** comes from the latest Holdings snapshot (the statement end date).
- **Purchase value** is units × open price, converted at the snapshot FX rate,
  because Holdings has no FX rate at purchase. Treat it as an estimate.
- **Tax regime** is inferred from the ISIN prefix. EU/EEA funds (`IE`, `LU`, …) get exit
  tax and deemed disposal. US-domiciled ETFs (`US…`) are shown but treated as CGT.
  Other domiciles are flagged "Check".
- This is an aid for your own records, not tax advice.

## Ideas / next steps

- Historic ECB FX rates for accurate EUR purchase values
- CGT summary for stocks, crypto and CFDs (33%, €1,270 exemption, loss offset)
- Dividend income and withholding tax credits
