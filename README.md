# IPPS Pricer

Local web interface for the CMS IPPS Pricer (FY2019–FY2027). See [ipps-pricer-ui/README.md](ipps-pricer-ui/README.md)
for setup, daily use, and notes on the pricer's behaviour.

Quick start (needs Java 17 and Python 3):

```bash
python3 ipps-pricer-ui/pricer_ui.py
```

## How to use

### 1. Price a single claim
Pick the hospital, enter the DRG, discharge date, covered charges and length of stay, then press **Price claim**.
The DRG description and the hospital's wage index are looked up as you type. The result panel shows the total
payment, the pricer's return code, and the payment breakdown. Review code, lifetime reserve days and diagnosis or
procedure codes are under *Advanced claim details*.

![Quick price](docs/1-quick-price.jpg)

### 2. Add your hospitals
On the **Hospitals** tab, press **New**, fill in the *Essentials* group from the hospital's Provider Specific File,
and press **Save profile**. Everything outside Essentials defaults to what a standard hospital has. Profiles are
saved to `ipps-pricer-ui/profiles.json` and matched by CCN everywhere else in the app.

![Hospital profiles](docs/2-hospitals.jpg)

### 3. Price a spreadsheet of claims
On the **Batch** tab, download the template, fill one row per claim (label, ccn, drg, discharge date, charges, los,
covered days), drop the file on the page and press **Price N rows**. Each row is matched to a hospital by CCN.
Files are priced on the server with parallel workers, so size is not a concern: 50,000 claims take about 10 seconds.
The page shows progress, a summary (paid / not paid / rejected counts and total payment) and the first 300 rows;
the .xlsx and .csv downloads contain every row. Click any row in the table to see that claim's full response.
Result files are kept in `ipps-pricer-ui/batches/` until you delete them.

![Batch pricing](docs/3-batch.jpg)
