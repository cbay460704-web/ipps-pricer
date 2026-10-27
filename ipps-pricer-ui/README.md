# IPPS Pricer UI

A local browser interface for the CMS IPPS Pricer (v2.14.1, FY2019–FY2027).
The pricer itself is `ipps-pricer-application-2.14.1.jar` (downloaded from cms.gov);
`pricer_ui.py` starts it, serves the page, and proxies requests to it.

## Requirements
- Java 17 (`brew install openjdk@17`)
- Python 3 (ships with macOS)

## Run
```bash
python3 /Users/cheaney/Documents/ipps-pricer/ipps-pricer-ui/pricer_ui.py
```
Opens http://localhost:8765. Ctrl-C stops both the UI and the pricer.
Options: `--port`, `--pricer-port` (default 3000), `--jar`, `--no-browser`, `--host 0.0.0.0` (allow other machines on the network).

## Daily use
1. **Hospitals tab** – enter each hospital once (CCN, dates, county, CBSA, beds, cost-to-charge ratios, DSH ratios,
   uncompensated care, IME ratios, VBP/HRRP factors). Only the *Essentials* group normally needs values; the other
   groups default to what a standard hospital has. Profiles are saved to `profiles.json` next to the app.
2. **Price a claim tab** – pick the hospital, enter DRG, discharge date, charges, length of stay, press *Price claim*.
   Covered days defaults to the length of stay. Review code, lifetime reserve days and diagnosis/procedure codes are
   under *Advanced claim details*.
3. **Batch tab** – download the 7-column template (`label, ccn, drg, discharge date, charges, los, covered days`),
   fill one row per claim, drop the file on the page, press *Price N rows*, download results as .xlsx or .csv.
   Dropped files are priced on the server with 8 parallel workers (about 4,000 claims/second; 50,000 rows in ~10 s)
   and written to `batches/<job>-results.csv|.xlsx`; the page shows progress, totals and the first 300 rows.
   Pasted rows are priced in the browser.
   Rows are matched to hospital profiles by CCN; a CCN with no profile uses the hospital currently selected.
   Extra columns named after any pricer field (e.g. `diagnosisCodes`, `provider.bedSize`) override values per row.
   Results keep every input column and add `out.*` columns (return code, total payment, operating/capital/outlier
   parts, wage index, DRG weight, which hospital profile was used, HTTP status, error text).
   Click a result row to see the claim's full response.
4. **Raw JSON tab** – the exact request sent to the pricer, with all defaults filled in; edit, paste, price, download.

## Defaults applied when a field is blank
COLA 1, VBP factor 1, HRRP factor 1, HRRP participant 1, VBP participant Y, hospital quality 1, HAC N, EHR Y,
capital payment code B, provider type 00, waiver N, new hospital N, and 0 for Medicaid/SSI ratios, uncompensated care,
IME ratios, low-volume factor, BPCI discount, capital exception rate, hold-harmless rate, facility-specific rate and
pass-through amounts. Fiscal year begin = effective date; wage-index and standardized-amount CBSA = hospital CBSA;
state code = first two digits of the county code.

## Notes learned from the pricer
- Capital PPS payment code must be `B` or `C` (otherwise return code 65).
- HRRP participant indicator must be `0` (with factor 0) or `1` (factor .97–1.0); blank also yields RC 65.
- VBP participant `Y` requires hospital quality indicator `1` and a VBP factor between .98 and 2, else RC 68.
- Return codes 00–49 are priced results (14 = normal, 16/44 = outlier, 02/06/10/12 = transfers); 50+ are errors.
- The pricer's own REST API is at http://localhost:3000 (`/v2/price-claim`, `/openapi.json`,
  `/v1/drg-entries/{year}/{drg}`, `/v1/cbsa-wage-indexes/{year}/{cbsa}`).
