# IPPS Pricer UI stress test

Run 2026-10-27 against pricer 2.14.1 (calc 2027.0), 3,124 hospital profiles.


## Sweep: every DRG x every hospital (FY2027)

768 DRGs x 3124 hospitals = 2,399,232 claims, $60,000 charges, LOS = each DRG's geometric mean LOS rounded (min 1), discharge 2026-10-15.
  chunk 001-135: 312,400 rows in 125.4s (elapsed 115.9s), xlsx=ok
  chunk 136-257: 312,400 rows in 131.1s (elapsed 120.9s), xlsx=ok
  chunk 263-376: 312,400 rows in 127.6s (elapsed 118.5s), xlsx=ok
  chunk 377-494: 312,400 rows in 137.5s (elapsed 128.2s), xlsx=ok
  chunk 495-617: 312,400 rows in 129.1s (elapsed 119.1s), xlsx=ok
  chunk 618-754: 312,400 rows in 133.2s (elapsed 123.2s), xlsx=ok
  chunk 755-881: 312,400 rows in 125.6s (elapsed 115.6s), xlsx=ok
  chunk 882-999: 212,432 rows in 78.9s (elapsed 72.8s), xlsx=ok

**Result:** 2,399,232 claims in 988s wall (2,427/s incl. upload, parse, export). Paid 2,124,288 · not paid 274,944 · HTTP errors 0.

| Return code | Count |
|---|---|
| 14 PAID DRG WITH PERDIEM | 2,099,802 |
| 55 DISCHRG DT LT EFF START DT | 139,008 |
| 68 INVALID VBPF IN PSF | 71,424 |
| 52 INVALID WAGE INDEX | 64,512 |
| 44 PAID DRG WITH PERDIEM (Outlier) | 16,286 |
| 16 PAID AS COST OUTLIER WITH PERDIEM | 8,200 |
[FAIL] sweep: 980 rows with paid but total <= 0 :: 998/010016 $0.0; 998/010044 $0.0; 998/010046 $0.0; 998/010073 $0.0; 998/010125 $0.0

DRG sanity: median payment vs DRG weight across 768 DRGs; 0 DRGs whose median is out of step with their weight by more than 2x (0 expected from base rate alone; outlier/transfer/per-diem rules legitimately break the ordering).
Lowest-weight DRG median $838 (998, wt 0.0), highest $366,164 (018, wt 45.1464).

## Ugly single-claim inputs (POST /api/v2/price-claim)

Expected: HTTP 200 with a return code, or HTTP 400 with named violations. Never 5xx/0.

| Case | Outcome |
|---|---|
| baseline | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| LOS 0 | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| LOS 999 | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| LOS 1000 (over max) | 400 · claimData.lengthOfStay must be less than or equal to 999 |
| LOS > covered days | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| covered days > LOS | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| LTR 60 | 200 · RC 62 INVALID NBR COVERED DAYS · $0.0 |
| LTR 61 | 200 · RC 61 LTR NOT NUM OR GT60 · $0.0 |
| LTR 99 (max) | 200 · RC 61 LTR NOT NUM OR GT60 · $0.0 |
| charges 0 | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| charges 999,999,999.99 | 200 · RC 16 PAID AS COST OUTLIER WITH PERDIEM · $158366880.9 |
| charges 1e9 (over max) | 400 · claimData.coveredCharges must be less than or equal to 999999999.99; claimData.coveredCharges numeric value out of bounds (<9 digits>.<2 digits> expected) |
| charges negative | 400 · claimData.coveredCharges must be greater than or equal to 0 |
| charges 3 decimals | 400 · claimData.coveredCharges numeric value out of bounds (<9 digits>.<2 digits> expected) |
| DRG 000 | 400 · claimData.diagnosisRelatedGroup must be three digits other than three zeros |
| DRG 999 (not in table) | 200 · RC 14 PAID DRG WITH PERDIEM · $1333.86 |
| DRG 47 (2 digits) | 400 · claimData.diagnosisRelatedGroup must be three digits other than three zeros |
| DRG letters | 400 · claimData.diagnosisRelatedGroup must be three digits other than three zeros |
| discharge Sept 30 FY boundary | 200 · RC 55 DISCHRG DT LT EFF START DT · $0.0 |
| discharge Oct 1 FY boundary | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| discharge 2019-10-01 (oldest FY) | 200 · RC 55 DISCHRG DT LT EFF START DT · $0.0 |
| discharge 2018-10-01 (unsupported FY) | 200 · RC 55 DISCHRG DT LT EFF START DT · $0.0 |
| discharge 2030-01-01 (future) | 400 · dischargeDate Claim year not supported: '2030-01-01' |
| discharge leap day | 200 · RC 55 DISCHRG DT LT EFF START DT · $0.0 |
| discharge invalid date | 400 · claimData.dischargeDate: Text '2026-02-30' could not be parsed: Invalid date 'FEBRUARY 30' |
| discharge US format | 400 · claimData.dischargeDate: Text '10/15/2026' could not be parsed at index 0 |
| discharge before provider effective | 200 · RC 55 DISCHRG DT LT EFF START DT · $0.0 |
| review code 11 | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| review code 99 | 200 · RC 57 REVIEW CODE INVALID · $0.0 |
| review code 1 digit | 400 · claimData.reviewCode must be two digits |
| 25 diagnosis codes | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| 26 diagnosis codes (over max) | 400 · claimData.diagnosisCodes size must be between 1 and 25 |
| empty diagnosis list | 400 · claimData.diagnosisCodes size must be between 1 and 25 |
| procedure codes | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| DOB 1900-01-01 | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| DOB after discharge | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| CCN 5 chars | 400 · claimData.providerCcn must be between six and thirteen digits or uppercase letters |
| CCN 13 chars | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| CCN lowercase | 400 · claimData.providerCcn must be between six and thirteen digits or uppercase letters |
| CCN mismatch claim vs provider | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| string where number expected | 400 · claimData.coveredCharges: Cannot deserialize value of type `java.math.BigDecimal` from String "sixty thousand": not a va |
| null required field | 400 · claimData.coveredDays must not be null |
| extra unknown field | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| demo code 61 (BPCI) | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| condition codes | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| midnight adj geolocation | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| provider: no CBSA at all | 200 · RC 52 INVALID WAGE INDEX · $0.0 |
| provider: CBSA 99999 (unknown) | 200 · RC 52 INVALID WAGE INDEX · $0.0 |
| provider: termination before discharge | 200 · RC 55 DISCHRG DT LT EFF START DT · $0.0 |
| provider: termination after discharge | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| provider: waiver Y | 200 · RC 53 WAIVER STATE NOT CALC · $0.0 |
| provider: new hospital Y, pay code A | 200 · RC 14 PAID DRG WITH PERDIEM · $14663.44 |
| provider: pay code A | 200 · RC 65 PAY CODE NOT ABC · $0.0 |
| provider: VBP Y factor 0.5 | 200 · RC 68 INVALID VBPF IN PSF · $0.0 |
| provider: HRRP 1 factor 0.9 | 200 · RC 65 PAY CODE NOT ABC · $0.0 |
| provider: HRRP blank | 200 · RC 65 PAY CODE NOT ABC · $0.0 |
| provider: medicaid ratio 0.9999 | 200 · RC 14 PAID DRG WITH PERDIEM · $17971.68 |
| provider: medicaid ratio 1.5 (over max) | 400 · providerData.medicaidRatio must be less than or equal to 0.9999 |
| provider: bed size 0 | 200 · RC 14 PAID DRG WITH PERDIEM · $15673.33 |
| provider: bed size 99999 | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| provider: op CCR 0 | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| provider: op CCR 9.999 | 200 · RC 16 PAID AS COST OUTLIER WITH PERDIEM · $447703.16 |
| provider: UCP 99,999,999.99 | 200 · RC 14 PAID DRG WITH PERDIEM · $100014389.98 |
| provider: low-volume factor 0.25 | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| provider: special wage index 1.5 + indicator | 200 · RC 14 PAID DRG WITH PERDIEM · $22153.29 |
| provider: effective date after discharge | 200 · RC 55 DISCHRG DT LT EFF START DT · $0.0 |
| provider: FY begin after effective | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| provider: EHR N (invalid per pricer) | 200 · RC 65 PAY CODE NOT ABC · $0.0 |
| provider: provider type 07 (RRC) | 200 · RC 14 PAID DRG WITH PERDIEM · $15723.85 |
| provider: provider type 17 (SCH) + FSR | 200 · RC 14 PAID DRG WITH PERDIEM · $19047.01 |
| provider: provider type 14 (MDH) + FSR | 200 · RC 14 PAID DRG WITH PERDIEM · $18216.22 |
| provider: provider type 08 (IHS) | 200 · RC 14 PAID DRG WITH PERDIEM · $21586.22 |
| provider: state code 3 digits | 400 · providerData.stateCode must be two digits |
| provider: county code letters | 400 · providerData.countyCode must be five digits |

All 74 cases answered with 200 or 400.

## Ugly spreadsheets (POST /batch/headers, /batch)

Expected: a header listing or a 400 with a message; a job that finishes; never a hang or a 5xx.

| Spreadsheet | Outcome |
|---|---|
| empty file | 0 rows → paid 0 / notPaid 0 / err 0 in 0.0s |
| header only | 0 rows → paid 0 / notPaid 0 / err 0 in 0.0s |
| no recognisable headers | 1 rows → paid 0 / notPaid 0 / err 1 in 0.0s · row1: HTTP 400 claimData.coveredCharges must not be null; claimData.covered · sent ccn=010001 drg=None date=None chg=None days=None |
| missing required columns (no drg) | 1 rows → paid 0 / notPaid 0 / err 1 in 0.0s · row1: HTTP 400 claimData.diagnosisRelatedGroup must not be null; claimData. · sent ccn=010001 drg=None date=None chg=5 days=2 |
| extra columns | 1 rows → paid 1 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=010001 drg=470 date=2026-10-15 chg=60000 days=2 |
| CCN lost leading zeros (10001) | 1 rows → paid 1 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=010001 drg=470 date=2026-10-15 chg=60000 days=2 |
| DRG as number 470.0 | 1 rows → paid 1 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=010001 drg=470 date=2026-10-15 chg=60000 days=2 |
| dates as Excel serial | 1 rows → paid 1 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=010001 drg=470 date=2026-10-15 chg=60000 days=2 |
| dates US format | 1 rows → paid 1 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=010001 drg=470 date=2026-10-15 chg=60000 days=2 |
| charges with $ and commas | 1 rows → paid 1 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=010001 drg=470 date=2026-10-15 chg=60000 days=2 |
| commas and quotes in label | 1 rows → paid 1 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=010001 drg=470 date=2026-10-15 chg=60000 days=2 |
| UTF-8 BOM + accents | 1 rows → paid 1 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=010001 drg=470 date=2026-10-15 chg=60000 days=2 |
| tab separated | 1 rows → paid 1 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=010001 drg=470 date=2026-10-15 chg=60000 days=2 |
| CRLF line endings | 1 rows → paid 1 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=010001 drg=470 date=2026-10-15 chg=60000 days=2 |
| blank rows in the middle | 2 rows → paid 2 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=010001 drg=470 date=2026-10-15 chg=60000 days=2 |
| 200 columns | 1 rows → paid 1 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=010001 drg=470 date=2026-10-15 chg=60000 days=2 |
| header aliases (DRG, Charges, Discharge Date) | 1 rows → paid 1 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=010001 drg=470 date=2026-10-15 chg=60000 days=2 |
| bare pricer field names | 1 rows → paid 1 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=010001 drg=470 date=2026-10-15 chg=60000 days=2 |
| provider override column | 1 rows → paid 1 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=010001 drg=470 date=2026-10-15 chg=60000 days=2 |
| unknown CCN (falls back to current hospital) | 1 rows → paid 1 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=999999 drg=470 date=2026-10-15 chg=60000 days=2 |
| xlsx normal | 1 rows → paid 1 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=010001 drg=470 date=2026-10-15 chg=60000 days=2 |
| xlsx numeric CCN and serial date | 1 rows → paid 1 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=010001 drg=470 date=2026-10-15 chg=60000 days=2 |
| xlsx with formulas-as-values and bools | 1 rows → paid 1 / notPaid 0 / err 0 in 0.0s · row1: RC 14 · sent ccn=010001 drg=470 date=2026-10-15 chg=60000 days=2 |
| not actually xlsx (zip of nothing) | headers: 400 · could not read file: not a valid .xlsx workbook (no worksheet inside) |
| binary junk .csv | headers: 400 · could not read file: new-line character seen in unquoted field - do you need to open the file with newline=''? |

## Load

Direct pricer throughput (1,000 identical claims per setting):

| Workers | Claims/s | Errors |
|---|---|---|
| 1 | 698 | 0 |
| 4 | 2,875 | 0 |
| 8 | 3,872 | 0 |
| 16 | 4,023 | 0 |
| 32 | 3,995 | 0 |

Through the UI proxy, 8 workers: 1,549 claims/s, 0 errors.

Three 20,000-row batch jobs started simultaneously: all finished cleanly in 26.4s total (individual elapsed: 24.4, 24.4, 25.1s).

Memory (RSS): UI server 622 → 756 MB, pricer JVM 539 → 598 MB across the parallel jobs.

## Findings and what was done about them

**Nothing crashed, hung, or returned a 5xx in any part.** 2,399,232 sweep claims, 74 ugly single-claim inputs, 26 ugly spreadsheets
(including a 50 MB / 1.1 M-row CSV), three simultaneous batch jobs and a concurrency sweep all produced either a pricer
return code or a named validation message.

Defects found in the wrapper and fixed in this commit:

1. **`470.0` in a CSV was sent to the pricer as text and rejected.** Spreadsheets exported from Excel often carry integers
   as `470.0`. Text fields now strip a trailing `.0` (server and browser).
2. **An Excel serial date arriving as text (CSV) was not recognised.** `46310` is now converted to `2026-10-15` just as it is
   when it arrives as a number from an .xlsx file.
3. **A corrupt .xlsx produced the message "list index out of range".** It now says "not a valid .xlsx workbook".
4. **The xlsx export built the whole sheet in memory.** The 1.1 M-row case pushed the UI process to 756 MB. The sheet is now
   streamed into the zip in 2,000-row chunks; peak memory on a 312 k-row job dropped from ~620 MB to under 200 MB.

Things that looked like failures but are correct pricer behaviour:

- **980 sweep rows "paid" at $0.** All DRG 998 (invalid principal diagnosis), whose FY2027 weight is 0. Zero weight gives a
  zero base payment; hospitals with no uncompensated-care amount therefore total $0 with return code 14. The check now
  ignores weight-zero DRGs. DRG 999 (ungroupable) is also weight 0 and is in the table, so it is paid, not rejected.
- **139,008 sweep rows with RC 55 (discharge before effective date).** 181 hospitals have a PSF record effective after the
  sweep's 2026-10-15 discharge date (CMS posts mid-month changes). See the design note below.
- **71,424 RC 68 and 64,512 RC 52.** Maryland and Puerto Rico hospitals (not paid under IPPS) and the 84 hospitals whose
  FY2027 PSF record CMS has not published yet, respectively; both documented in the README.
- **A DRG with no weight (998/999) is paid only its uncompensated-care amount**, and a claim whose CCN does not match the
  provider record is accepted: the pricer trusts its caller on both. The UI always sends matching CCNs.
- **LTR 60 with 2 covered days gives RC 62**, and **HRRP factor 0.9 gives RC 65**: pricer validation rules, as documented
  in the Hospitals tab hints.

Performance:

- Raw pricer: ~700 claims/s single-threaded, ~3,900/s at 8 workers, flat beyond 16. The batch path sustains ~2,400/s
  including upload, parsing and both export files; 2.4 M claims took 16.5 minutes.
- Through the UI's single-claim proxy: ~1,500/s (one extra HTTP hop; the batch path calls the pricer directly).
- Three 20,000-row jobs started together finished in 26 s total with no interference.
- JVM memory stayed between 540 and 850 MB throughout; no growth across the 2.4 M-claim sweep.

Design limitation worth fixing next (not a bug in what exists):

- **Profiles hold one PSF record per hospital** (the latest effective on or before the import's as-of date). Any claim
  discharged before that record's effective date returns RC 55, which means claims from earlier fiscal years cannot be
  priced from an imported profile even though the pricer supports FY2019–FY2027. The fix is to keep every effective-dated
  record per CCN and pick the one in force on the discharge date.

## How to re-run

```bash
python3 stress/stress_test.py                 # sweep + inputs + sheets + load, ~20 min
python3 stress/stress_test.py --only inputs sheets   # the fast parts, ~1 min
python3 stress/stress_test.py --big --soak 60 # add the 1.1 M-row sheet and an hour-long soak
```
