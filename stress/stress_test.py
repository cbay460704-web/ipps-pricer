#!/usr/bin/env python3
"""Stress and correctness suite for the IPPS Pricer UI.

Runs against a live pricer_ui.py (default http://localhost:8765). Produces
stress/report.md and leaves raw outputs in stress/out/.

Parts (select with --only, default all):
  sweep     every DRG x every hospital through the server batch endpoint
  inputs    ugly single-claim values straight through /api/v2/price-claim
  sheets    ugly spreadsheets through /batch/headers and /batch
  load      concurrency sweep, parallel jobs, memory of the two processes
  soak      N minutes of continuous pricing (default 0 = skip; --soak 60)

Rule for every case: a readable return code or validation message, never a
crash, a hung job, a 5xx, or a silently wrong number.
"""
import argparse, base64, csv, io, json, os, random, re, statistics, subprocess, sys, time, urllib.request, urllib.error
import concurrent.futures as cf
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
UI = "http://localhost:8765"
PRICER = "http://localhost:3000"
APP = os.path.join(os.path.dirname(HERE), "ipps-pricer-ui")

findings = []          # (severity, title, detail)
BIG = False            # include the 50 MB spreadsheet case (--big)
sections = []          # markdown chunks

def note(sev, title, detail=""):
    findings.append((sev, title, detail))
    print(f"[{sev}] {title}" + (f" :: {detail[:200]}" if detail else ""), flush=True)

def md(s):
    sections.append(s); print(s, flush=True)

def http(method, url, body=None, headers=None, timeout=600):
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()

def jget(url, **kw):
    s, b = http("GET", url, **kw)
    return s, (json.loads(b) if b[:1] in (b"{", b"[") else b.decode(errors="replace"))

# --------------------------------------------------------------------------
# shared: the UI's field rules, mirrored from index.html (kept in sync by hand)
# --------------------------------------------------------------------------
def ui_context(header_map, current_provider, concurrency=8):
    cf_ = [("providerCcn","text",None,None),("diagnosisRelatedGroup","text",None,None),("dischargeDate","date",None,None),
           ("coveredCharges","number",None,None),("lengthOfStay","number",None,None),("coveredDays","number",None,None),
           ("lifetimeReserveDays","number",0,None),("reviewCode","select","00",None),("diagnosisCodes","list",None,None),
           ("procedureCodes","list",None,None),("patientDateOfBirth","date",None,None)]
    pd_ = [("costOfLivingAdjustment",1),("vbpAdjustment",1),("hrrAdjustment",1),("hrrParticipantIndicator","1"),("vbpParticipantIndicator","Y"),
           ("hospitalQualityIndicator","1"),("hacReductionParticipantIndicator","N"),("ehrReductionIndicator","Y"),("capitalPpsPaymentCode","B"),
           ("providerType","00"),("waiverIndicator","N"),("newHospital","N"),("medicaidRatio",0),("supplementalSecurityIncomeRatio",0),
           ("uncompensatedCareAmount",0),("internsToBedsRatio",0),("capitalIndirectMedicalEducationRatio",0),("lowVolumeAdjustmentFactor",0),
           ("bundleModel1Discount",0),("capitalExceptionPaymentRate",0),("oldCapitalHoldHarmlessRate",0),("ppsFacilitySpecificRate",0),
           ("passThroughTotalAmount",0),("passThroughAmountForDirectMedicalEducation",0),("passThroughAmountForOrganAcquisition",0)]
    fields = lambda L: [{"n": n, "t": t, "d": d, "dFrom": df} for n, t, d, df in L]
    prov = [{"n": n, "t": "number" if isinstance(d, (int, float)) else "select", "d": d, "dFrom": None} for n, d in pd_]
    prov += [{"n": "fiscalYearBeginDate", "t": "date", "d": None, "dFrom": "effectiveDate"},
             {"n": "cbsaWageIndexLocation", "t": "text", "d": None, "dFrom": "cbsaActualGeographicLocation"},
             {"n": "cbsaStandardizedAmountLocation", "t": "text", "d": None, "dFrom": "cbsaActualGeographicLocation"},
             {"n": "bedSize", "t": "number", "d": None, "dFrom": None}, {"n": "effectiveDate", "t": "date", "d": None, "dFrom": None}]
    return {"claimFields": fields(cf_), "providerFields": prov, "headerMap": header_map,
            "pad": {"claim.providerCcn": 6, "claim.diagnosisRelatedGroup": 3, "claim.reviewCode": 2},
            "outCols": [["rc", "returnCodeData.code"], ["rcDescription", "returnCodeData.description"], ["totalPayment", "paymentData.totalPayment"],
                        ["operatingFederal", "paymentData.operatingFederalSpecificPortionPart"], ["outlier", "paymentData.operatingOutlierPaymentPart"],
                        ["capitalTotal", "additionalCalculationVariables.additionalCapitalVariables.capitalTotalPayment"],
                        ["ucp", "additionalCalculationVariables.additionalPaymentInformation.uncompensatedCarePayment"],
                        ["wi", "paymentData.finalWageIndex"], ["hospital", "_hospital"], ["httpStatus", "_status"], ["error", "_error"]],
            "currentCcn": None, "currentProvider": current_provider, "concurrency": concurrency}

STD_MAP = {"label": "label", "ccn": "claim.providerCcn", "drg": "claim.diagnosisRelatedGroup", "discharge date": "claim.dischargeDate",
           "charges": "claim.coveredCharges", "los": "claim.lengthOfStay", "covered days": "claim.coveredDays"}

def run_batch(name, csv_text, ctx, poll=1.0):
    b64 = base64.b64encode(csv_text.encode()).decode()
    t0 = time.time()
    s, b = http("POST", f"{UI}/batch", json.dumps({"name": name, "file": b64, "context": ctx}).encode())
    if s != 200:
        return None, {"error": f"POST /batch -> {s}: {b[:200]!r}"}
    job = json.loads(b)
    while True:
        time.sleep(poll)
        s2, st = jget(f"{UI}/batch/{job['id']}")
        if s2 != 200:
            return job, {"error": f"status poll -> {s2}"}
        if st["status"] == "done":
            st["wall"] = round(time.time() - t0, 1)
            return job, st
        if time.time() - t0 > 3600:
            return job, {"error": "job did not finish within an hour", **st}

def results_rows(job):
    path = os.path.join(APP, "batches", f"{job['id']}-results.csv")
    with open(path, newline="") as f:
        return list(csv.DictReader(f))

def mem_mb(pattern):
    try:
        out = subprocess.run(["ps", "-axo", "rss,command"], capture_output=True, text=True).stdout
        return sum(int(l.split()[0]) for l in out.splitlines()[1:] if pattern in l and "grep" not in l) // 1024
    except Exception:
        return -1

# --------------------------------------------------------------------------
def part_sweep(profiles, sample, fy, chunk_drgs):
    s, drgs = jget(f"{PRICER}/v1/drg-entries/{fy}")
    drg_list = sorted({e["diagnosticRelatedGroup"] for e in drgs["tableEntries"]})
    ccns = sorted(profiles)
    md(f"## Sweep: every DRG x every hospital (FY{fy})\n\n{len(drg_list)} DRGs x {len(ccns)} hospitals = {len(drg_list)*len(ccns):,} claims, "
       f"$60,000 charges, LOS = each DRG's geometric mean LOS rounded (min 1), discharge {fy-1}-10-15.")
    gmlos = {e["diagnosticRelatedGroup"]: max(1, round(e["geometricMeanLengthOfStay"] or 1)) for e in drgs["tableEntries"]}
    zero_weight = {e["diagnosticRelatedGroup"] for e in drgs["tableEntries"] if not e["weight"]}   # 998/999: $0 base payment is correct
    ctx = ui_context(STD_MAP, sample["providerData"])
    totals = {"rows": 0, "wall": 0.0, "paid": 0, "notPaid": 0, "errors": 0}
    rc_counter, http_bad, anomalies, by_drg = {}, [], [], {}
    for ci in range(0, len(drg_list), chunk_drgs):
        chunk = drg_list[ci:ci + chunk_drgs]
        buf = io.StringIO(); w = csv.writer(buf); w.writerow(["label", "ccn", "drg", "discharge date", "charges", "los", "covered days"])
        for d in chunk:
            for c in ccns:
                w.writerow([f"{d}/{c}", c, d, f"{fy-1}-10-15", 60000, gmlos[d], gmlos[d]])
        t = time.time()
        job, st = run_batch(f"sweep-{chunk[0]}-{chunk[-1]}.csv", buf.getvalue(), ctx, poll=2)
        if not job or "error" in st:
            note("FAIL", f"sweep chunk {chunk[0]}-{chunk[-1]} failed", json.dumps(st)[:300]); continue
        rows = results_rows(job)
        totals["rows"] += len(rows); totals["wall"] += st["wall"]
        for k in ("paid", "notPaid", "errors"): totals[k] += st[k]
        print(f"  chunk {chunk[0]}-{chunk[-1]}: {len(rows):,} rows in {st['wall']}s (elapsed {st['elapsed']}s), xlsx={'ok' if st.get('xlsx') else st.get('xlsxError')}", flush=True)
        if not st.get("xlsx"):
            note("WARN", f"xlsx export failed for chunk {chunk[0]}-{chunk[-1]}", str(st.get("xlsxError")))
        for r in rows:
            key = f"{r['out.rc']} {r['out.rcDescription']}" if r["out.httpStatus"] == "200" else f"HTTP {r['out.httpStatus']}"
            rc_counter[key] = rc_counter.get(key, 0) + 1
            if r["out.httpStatus"] != "200":
                http_bad.append(r); continue
            rc = r["out.rc"]
            if not re.fullmatch(r"\d{2}", rc or ""):
                anomalies.append(("bad return code", r)); continue
            tp = float(r["out.totalPayment"] or 0)
            if int(rc) < 50:
                if tp <= 0 and r["drg"] not in zero_weight: anomalies.append(("paid but total <= 0", r))
                if tp > 2_000_000: anomalies.append(("total > $2M", r))
                by_drg.setdefault(r["drg"], []).append(tp)
            else:
                if tp != 0: anomalies.append(("error code but non-zero payment", r))
        os.remove(os.path.join(APP, "batches", f"{job['id']}-results.csv"))
        for ext in (".xlsx",):
            p = os.path.join(APP, "batches", f"{job['id']}-results{ext}")
            if os.path.exists(p): os.remove(p)
    md(f"\n**Result:** {totals['rows']:,} claims in {totals['wall']:.0f}s wall ({totals['rows']/max(totals['wall'],1):,.0f}/s incl. upload, parse, export). "
       f"Paid {totals['paid']:,} · not paid {totals['notPaid']:,} · HTTP errors {totals['errors']:,}.")
    md("\n| Return code | Count |\n|---|---|\n" + "\n".join(f"| {k} | {v:,} |" for k, v in sorted(rc_counter.items(), key=lambda x: -x[1])))
    if http_bad: note("FAIL", f"{len(http_bad)} sweep rows got a non-200 from the pricer", json.dumps(http_bad[0])[:300])
    if anomalies:
        kinds = {}
        for k, r in anomalies: kinds.setdefault(k, []).append(r)
        for k, rs in kinds.items():
            note("WARN" if k == "total > $2M" else "FAIL", f"sweep: {len(rs)} rows with {k}", "; ".join(f"{r['label']} ${r['out.totalPayment']}" for r in rs[:5]))
    # DRG-level sanity: payment should scale with weight; check rank correlation
    s, wts = jget(f"{PRICER}/v1/drg-entries/{fy}")
    weight = {e["diagnosticRelatedGroup"]: e["weight"] for e in wts["tableEntries"]}
    meds = sorted((statistics.median(v), weight[d], d) for d, v in by_drg.items() if d in weight)
    inv = sum(1 for i in range(1, len(meds)) if meds[i][1] < meds[i-1][1] * 0.5)
    md(f"\nDRG sanity: median payment vs DRG weight across {len(meds)} DRGs; {inv} DRGs whose median is out of step with their weight by more than 2x "
       f"(0 expected from base rate alone; outlier/transfer/per-diem rules legitimately break the ordering).")
    md(f"Lowest-weight DRG median ${meds[0][0]:,.0f} ({meds[0][2]}, wt {meds[0][1]}), highest ${meds[-1][0]:,.0f} ({meds[-1][2]}, wt {meds[-1][1]}).")
    return totals

# --------------------------------------------------------------------------
def part_inputs(profiles, sample, fy):
    md("\n## Ugly single-claim inputs (POST /api/v2/price-claim)\n\nExpected: HTTP 200 with a return code, or HTTP 400 with named violations. Never 5xx/0.\n")
    base_claim = {"providerCcn": "010001", "diagnosisRelatedGroup": "470", "dischargeDate": f"{fy-1}-10-15", "coveredCharges": 60000,
                  "lengthOfStay": 2, "coveredDays": 2, "lifetimeReserveDays": 0, "reviewCode": "00"}
    prov = {k: v for k, v in profiles["010001"].items() if k not in ("name", "source")}
    for f in ui_context({}, {})["providerFields"]:
        if f["d"] is not None: prov.setdefault(f["n"], f["d"])
    prov.setdefault("fiscalYearBeginDate", prov["effectiveDate"]); prov.setdefault("cbsaWageIndexLocation", prov["cbsaActualGeographicLocation"]); prov.setdefault("cbsaStandardizedAmountLocation", prov["cbsaActualGeographicLocation"])
    cases = [
        ("baseline", {}), ("LOS 0", {"lengthOfStay": 0, "coveredDays": 0}), ("LOS 999", {"lengthOfStay": 999, "coveredDays": 999}),
        ("LOS 1000 (over max)", {"lengthOfStay": 1000}), ("LOS > covered days", {"lengthOfStay": 10, "coveredDays": 2}),
        ("covered days > LOS", {"lengthOfStay": 2, "coveredDays": 10}), ("LTR 60", {"lifetimeReserveDays": 60}), ("LTR 61", {"lifetimeReserveDays": 61}),
        ("LTR 99 (max)", {"lifetimeReserveDays": 99}), ("charges 0", {"coveredCharges": 0}), ("charges 999,999,999.99", {"coveredCharges": 999999999.99}),
        ("charges 1e9 (over max)", {"coveredCharges": 1000000000}), ("charges negative", {"coveredCharges": -5}),
        ("charges 3 decimals", {"coveredCharges": 100.123}), ("DRG 000", {"diagnosisRelatedGroup": "000"}), ("DRG 999 (not in table)", {"diagnosisRelatedGroup": "999"}),
        ("DRG 47 (2 digits)", {"diagnosisRelatedGroup": "47"}), ("DRG letters", {"diagnosisRelatedGroup": "ABC"}),
        ("discharge Sept 30 FY boundary", {"dischargeDate": f"{fy-1}-09-30"}), ("discharge Oct 1 FY boundary", {"dischargeDate": f"{fy-1}-10-01"}),
        ("discharge 2019-10-01 (oldest FY)", {"dischargeDate": "2019-10-01"}), ("discharge 2018-10-01 (unsupported FY)", {"dischargeDate": "2018-10-01"}),
        ("discharge 2030-01-01 (future)", {"dischargeDate": "2030-01-01"}), ("discharge leap day", {"dischargeDate": "2024-02-29"}),
        ("discharge invalid date", {"dischargeDate": "2026-02-30"}), ("discharge US format", {"dischargeDate": "10/15/2026"}),
        ("discharge before provider effective", {"dischargeDate": f"{fy-1}-09-15"}),
        ("review code 11", {"reviewCode": "11"}), ("review code 99", {"reviewCode": "99"}), ("review code 1 digit", {"reviewCode": "1"}),
        ("25 diagnosis codes", {"diagnosisCodes": [f"A{i:02d}0" for i in range(25)]}), ("26 diagnosis codes (over max)", {"diagnosisCodes": [f"A{i:02d}0" for i in range(26)]}),
        ("empty diagnosis list", {"diagnosisCodes": []}), ("procedure codes", {"procedureCodes": ["0SRC0J9", "0SRD0J9"]}),
        ("DOB 1900-01-01", {"patientDateOfBirth": "1900-01-01"}), ("DOB after discharge", {"patientDateOfBirth": "2027-01-01"}),
        ("CCN 5 chars", {"providerCcn": "01000"}), ("CCN 13 chars", {"providerCcn": "0100010000000"}), ("CCN lowercase", {"providerCcn": "01000a"}),
        ("CCN mismatch claim vs provider", {"providerCcn": "999999"}), ("string where number expected", {"coveredCharges": "sixty thousand"}),
        ("null required field", {"coveredDays": None}), ("extra unknown field", {"somethingElse": 1}),
        ("demo code 61 (BPCI)", {"demoCodes": ["61"]}), ("condition codes", {"conditionCodes": ["ZA", "ZB"]}),
        ("midnight adj geolocation", {"midnightAdjustmentGeolocation": "6120"}),
    ]
    prov_cases = [
        ("provider: no CBSA at all", {"cbsaActualGeographicLocation": None, "cbsaWageIndexLocation": None, "cbsaStandardizedAmountLocation": None}),
        ("provider: CBSA 99999 (unknown)", {"cbsaActualGeographicLocation": "99999", "cbsaWageIndexLocation": "99999", "cbsaStandardizedAmountLocation": "99999"}),
        ("provider: termination before discharge", {"terminationDate": f"{fy-1}-10-10"}), ("provider: termination after discharge", {"terminationDate": f"{fy}-09-30"}),
        ("provider: waiver Y", {"waiverIndicator": "Y"}), ("provider: new hospital Y, pay code A", {"newHospital": "Y", "capitalPpsPaymentCode": "A"}),
        ("provider: pay code A", {"capitalPpsPaymentCode": "A"}), ("provider: VBP Y factor 0.5", {"vbpParticipantIndicator": "Y", "vbpAdjustment": 0.5}),
        ("provider: HRRP 1 factor 0.9", {"hrrParticipantIndicator": "1", "hrrAdjustment": 0.9}), ("provider: HRRP blank", {"hrrParticipantIndicator": None}),
        ("provider: medicaid ratio 0.9999", {"medicaidRatio": 0.9999}), ("provider: medicaid ratio 1.5 (over max)", {"medicaidRatio": 1.5}),
        ("provider: bed size 0", {"bedSize": 0}), ("provider: bed size 99999", {"bedSize": 99999}),
        ("provider: op CCR 0", {"operatingCostToChargeRatio": 0}), ("provider: op CCR 9.999", {"operatingCostToChargeRatio": 9.999}),
        ("provider: UCP 99,999,999.99", {"uncompensatedCareAmount": 99999999.99}), ("provider: low-volume factor 0.25", {"lowVolumeAdjustmentFactor": 0.25}),
        ("provider: special wage index 1.5 + indicator", {"specialWageIndex": 1.5, "specialPaymentIndicator": "1"}),
        ("provider: effective date after discharge", {"effectiveDate": f"{fy}-01-01", "fiscalYearBeginDate": f"{fy}-01-01"}),
        ("provider: FY begin after effective", {"fiscalYearBeginDate": f"{fy}-06-01"}),
        ("provider: EHR N (invalid per pricer)", {"ehrReductionIndicator": "N"}),
        ("provider: provider type 07 (RRC)", {"providerType": "07"}), ("provider: provider type 17 (SCH) + FSR", {"providerType": "17", "ppsFacilitySpecificRate": 9000}),
        ("provider: provider type 14 (MDH) + FSR", {"providerType": "14", "ppsFacilitySpecificRate": 9000}), ("provider: provider type 08 (IHS)", {"providerType": "08"}),
        ("provider: state code 3 digits", {"stateCode": "001"}), ("provider: county code letters", {"countyCode": "ABCDE"}),
    ]
    rows, bad = [], []
    for title, over in cases + prov_cases:
        c = dict(base_claim); p = dict(prov)
        (p if title.startswith("provider:") else c).update(over)
        c = {k: v for k, v in c.items() if v is not None or k == "coveredDays"}; p = {k: v for k, v in p.items() if v is not None}
        s, b = http("POST", f"{UI}/api/v2/price-claim", json.dumps({"claimData": c, "providerData": p}).encode(), timeout=60)
        try: j = json.loads(b)
        except Exception: j = {"raw": b[:120].decode(errors="replace")}
        if s == 200:
            rc = j.get("returnCodeData", {}); outcome = f"200 · RC {rc.get('code')} {rc.get('description','')} · ${j.get('paymentData',{}).get('totalPayment')}"
        elif s == 400:
            v = j.get("violations") or []
            outcome = f"400 · " + ("; ".join(f"{x.get('field','').replace('priceClaim.arg0.','')} {x.get('message','')}" for x in v[:2]) if v else (j.get("detail") or j.get("message") or j.get("raw") or "")[:120])
        else:
            outcome = f"{s} · {str(j)[:120]}"; bad.append((title, outcome))
        rows.append((title, outcome))
    md("| Case | Outcome |\n|---|---|\n" + "\n".join(f"| {t} | {o.replace('|','/')} |" for t, o in rows))
    for t, o in bad: note("FAIL", f"input case '{t}' got {o}")
    if not bad: md(f"\nAll {len(rows)} cases answered with 200 or 400.")

# --------------------------------------------------------------------------
def part_sheets(profiles, sample):
    md("\n## Ugly spreadsheets (POST /batch/headers, /batch)\n\nExpected: a header listing or a 400 with a message; a job that finishes; never a hang or a 5xx.\n")
    ctx_std = ui_context(STD_MAP, sample["providerData"])
    good = "label,ccn,drg,discharge date,charges,los,covered days\nok,010001,470,2026-10-15,60000,2,2\n"
    sys.path.insert(0, APP); import pricer_ui as P
    def xlsx_bytes(headers, rows):
        p = os.path.join(OUT, "tmp.xlsx"); P.write_xlsx(p, headers, iter(rows)); return open(p, "rb").read()
    cases = [
        ("empty file", "a.csv", b""), ("header only", "a.csv", good.split("\n")[0].encode()),
        ("no recognisable headers", "a.csv", b"foo,bar,baz\n1,2,3\n"), ("missing required columns (no drg)", "a.csv", b"label,ccn,charges,los\nx,010001,5,2\n"),
        ("extra columns", "a.csv", b"label,ccn,drg,discharge date,charges,los,covered days,notes,zzz\nok,010001,470,2026-10-15,60000,2,2,hello,1\n"),
        ("CCN lost leading zeros (10001)", "a.csv", b"ccn,drg,discharge date,charges,los\n10001,470,2026-10-15,60000,2\n"),
        ("DRG as number 470.0", "a.csv", b"ccn,drg,discharge date,charges,los\n010001,470.0,2026-10-15,60000,2\n"),
        ("dates as Excel serial", "a.csv", b"ccn,drg,discharge date,charges,los\n010001,470,46310,60000,2\n"),
        ("dates US format", "a.csv", b"ccn,drg,discharge date,charges,los\n010001,470,10/15/2026,60000,2\n"),
        ("charges with $ and commas", "a.csv", b'ccn,drg,discharge date,charges,los\n010001,470,2026-10-15,"$60,000.00",2\n'),
        ("commas and quotes in label", "a.csv", b'label,ccn,drg,discharge date,charges,los\n"Smith, John ""JJ""",010001,470,2026-10-15,60000,2\n'),
        ("UTF-8 BOM + accents", "a.csv", "﻿label,ccn,drg,discharge date,charges,los\nJosé Ñandú,010001,470,2026-10-15,60000,2\n".encode()),
        ("tab separated", "a.tsv", b"label\tccn\tdrg\tdischarge date\tcharges\tlos\nx\t010001\t470\t2026-10-15\t60000\t2\n"),
        ("CRLF line endings", "a.csv", good.replace("\n", "\r\n").encode()), ("blank rows in the middle", "a.csv", good.encode() + b"\n\n,,,,,,\n" + good.split("\n")[1].encode() + b"\n"),
        ("200 columns", "a.csv", (",".join(["ccn", "drg", "discharge date", "charges", "los"] + [f"c{i}" for i in range(195)]) + "\n" + ",".join(["010001", "470", "2026-10-15", "60000", "2"] + ["x"] * 195) + "\n").encode()),
        ("header aliases (DRG, Charges, Discharge Date)", "a.csv", b"Label,CCN,DRG,Discharge Date,Charges,LOS\nx,010001,470,2026-10-15,60000,2\n"),
        ("bare pricer field names", "a.csv", b"providerCcn,diagnosisRelatedGroup,dischargeDate,coveredCharges,lengthOfStay\n010001,470,2026-10-15,60000,2\n"),
        ("provider override column", "a.csv", b"ccn,drg,discharge date,charges,los,provider.bedSize\n010001,470,2026-10-15,60000,2,1\n"),
        ("unknown CCN (falls back to current hospital)", "a.csv", b"ccn,drg,discharge date,charges,los\n999999,470,2026-10-15,60000,2\n"),
        ("xlsx normal", "a.xlsx", xlsx_bytes(["ccn", "drg", "discharge date", "charges", "los"], [["010001", "470", "2026-10-15", 60000, 2]])),
        ("xlsx numeric CCN and serial date", "a.xlsx", xlsx_bytes(["ccn", "drg", "discharge date", "charges", "los"], [[10001, 470, 46310, 60000.0, 2]])),
        ("xlsx with formulas-as-values and bools", "a.xlsx", xlsx_bytes(["ccn", "drg", "discharge date", "charges", "los", "flag"], [["010001", "470", "2026-10-15", 60000, 2, True]])),
        ("not actually xlsx (zip of nothing)", "a.xlsx", b"PK\x05\x06" + b"\x00" * 18), ("binary junk .csv", "a.csv", bytes(range(256)) * 10),
    ]
    if BIG:
        cases.append(("50 MB csv", "big.csv", (good.split("\n")[0] + "\n" + "\n".join(f"r{i},010001,470,2026-10-15,60000,2,2" for i in range(1_100_000)) + "\n").encode()))
    rows = []
    for title, name, data in cases:
        b64 = base64.b64encode(data).decode()
        s, b = http("POST", f"{UI}/batch/headers", json.dumps({"name": name, "file": b64}).encode(), timeout=300)
        try: j = json.loads(b)
        except Exception: j = {"raw": b[:100].decode(errors="replace")}
        if s != 200:
            rows.append((title, f"headers: {s} · {j.get('error', j)}"[:160]));
            if s not in (400,): note("FAIL", f"sheet '{title}' headers -> {s}", str(j)[:200])
            continue
        headers = j["headers"]
        # map like the browser does: aliases, bare names, claim./provider. prefixes
        hm = {}
        for h in headers:
            k = re.sub(r"[^a-z0-9.]", "", h.lower())
            alias = {"label": "label", "ccn": "claim.providerCcn", "providerccn": "claim.providerCcn", "drg": "claim.diagnosisRelatedGroup", "dischargedate": "claim.dischargeDate",
                     "charges": "claim.coveredCharges", "coveredcharges": "claim.coveredCharges", "los": "claim.lengthOfStay", "lengthofstay": "claim.lengthOfStay",
                     "covereddays": "claim.coveredDays", "diagnosisrelatedgroup": "claim.diagnosisRelatedGroup"}
            if k in alias: hm[h] = alias[k]
            elif k.startswith("provider."): hm[h] = "provider." + h.split(".", 1)[1]
        ctx = dict(ctx_std, headerMap=hm)
        t = time.time()
        # upload the raw bytes exactly as the browser would
        s2, b2 = http("POST", f"{UI}/batch", json.dumps({"name": name, "file": b64, "context": ctx}).encode(), timeout=600)
        if s2 != 200:
            rows.append((title, f"batch: {s2} · {b2[:120].decode(errors='replace')}"))
            if s2 != 400: note("FAIL", f"sheet '{title}' batch -> {s2}", b2[:200].decode(errors="replace"))
            continue
        jid = json.loads(b2)["id"]
        while True:
            time.sleep(0.5); s3, st = jget(f"{UI}/batch/{jid}")
            if st.get("status") == "done" or time.time() - t > 900: break
        s4, full = jget(f"{UI}/batch/{jid}", headers={"X-Preview": "1"})
        prev = full.get("preview", [])
        first = prev[0]["res"] if prev else {}
        summ = f"{st.get('total')} rows → paid {st.get('paid')} / notPaid {st.get('notPaid')} / err {st.get('errors')} in {st.get('elapsed')}s"
        if prev:
            rc = first.get("returnCodeData", {}).get("code"); summ += f" · row1: {'RC '+str(rc) if first.get('_status')==200 else 'HTTP '+str(first.get('_status'))+' '+str(first.get('_error'))[:60]}"
            req = prev[0]["req"]["claimData"]; summ += f" · sent ccn={req.get('providerCcn')} drg={req.get('diagnosisRelatedGroup')} date={req.get('dischargeDate')} chg={req.get('coveredCharges')} days={req.get('coveredDays')}"
        if st.get("status") != "done": note("FAIL", f"sheet '{title}' job never finished", json.dumps(st)[:200])
        rows.append((title, summ[:220]))
        for ext in ("csv", "xlsx"):
            p = os.path.join(APP, "batches", f"{jid}-results.{ext}")
            if os.path.exists(p): os.remove(p)
    md("| Spreadsheet | Outcome |\n|---|---|\n" + "\n".join(f"| {t} | {o.replace('|','/')} |" for t, o in rows))

# --------------------------------------------------------------------------
def part_load(profiles, sample):
    md("\n## Load\n")
    body = json.dumps({"claimData": sample["claimData"], "providerData": sample["providerData"]}).encode()
    def one(_):
        s, b = http("POST", f"{PRICER}/v2/price-claim", body, timeout=60); return s
    md("Direct pricer throughput (1,000 identical claims per setting):\n\n| Workers | Claims/s | Errors |\n|---|---|---|")
    for w in (1, 4, 8, 16, 32):
        t = time.time()
        with cf.ThreadPoolExecutor(w) as ex: res = list(ex.map(one, range(1000)))
        md(f"| {w} | {1000/(time.time()-t):,.0f} | {sum(1 for s in res if s != 200)} |")
    # through the UI proxy
    def one_ui(_):
        s, b = http("POST", f"{UI}/api/v2/price-claim", body, timeout=60); return s
    t = time.time()
    with cf.ThreadPoolExecutor(8) as ex: res = list(ex.map(one_ui, range(1000)))
    md(f"\nThrough the UI proxy, 8 workers: {1000/(time.time()-t):,.0f} claims/s, {sum(1 for s in res if s != 200)} errors.")
    # three batch jobs at once
    ctx = ui_context(STD_MAP, sample["providerData"])
    def mk(n, tag):
        buf = io.StringIO(); w = csv.writer(buf); w.writerow(["label", "ccn", "drg", "discharge date", "charges", "los", "covered days"])
        ccns = sorted(profiles)
        for i in range(n): w.writerow([f"{tag}-{i}", ccns[i % len(ccns)], "470", "2026-10-15", 60000, 2, 2])
        return buf.getvalue()
    m0 = (mem_mb("pricer_ui.py"), mem_mb("ipps-pricer-application"))
    t = time.time()
    with cf.ThreadPoolExecutor(3) as ex:
        outs = list(ex.map(lambda k: run_batch(f"parallel-{k}.csv", mk(20000, f"p{k}"), ctx, poll=1), range(3)))
    wall = time.time() - t
    ok = all(j and st.get("status") == "done" and st["total"] == 20000 and st["errors"] == 0 for j, st in outs)
    md(f"\nThree 20,000-row batch jobs started simultaneously: {'all finished cleanly' if ok else 'PROBLEM'} in {wall:.1f}s total "
       f"(individual elapsed: {', '.join(str(st.get('elapsed')) for _, st in outs)}s).")
    if not ok: note("FAIL", "parallel batch jobs did not all complete", json.dumps([st for _, st in outs])[:400])
    for j, _ in outs:
        for ext in ("csv", "xlsx"):
            p = os.path.join(APP, "batches", f"{j['id']}-results.{ext}")
            if os.path.exists(p): os.remove(p)
    m1 = (mem_mb("pricer_ui.py"), mem_mb("ipps-pricer-application"))
    md(f"\nMemory (RSS): UI server {m0[0]} → {m1[0]} MB, pricer JVM {m0[1]} → {m1[1]} MB across the parallel jobs.")

def part_soak(profiles, sample, minutes):
    md(f"\n## Soak: {minutes} minutes of continuous pricing\n")
    body = json.dumps({"claimData": sample["claimData"], "providerData": sample["providerData"]}).encode()
    t_end = time.time() + minutes * 60; samples = []; n = 0; errs = 0
    while time.time() < t_end:
        t = time.time()
        with cf.ThreadPoolExecutor(8) as ex:
            res = list(ex.map(lambda _: http("POST", f"{UI}/api/v2/price-claim", body, timeout=60)[0], range(2000)))
        n += 2000; errs += sum(1 for s in res if s != 200)
        samples.append((round((time.time() - t_end) / 60 + minutes, 1), round(2000 / (time.time() - t)), mem_mb("pricer_ui.py"), mem_mb("ipps-pricer-application")))
    md(f"{n:,} claims, {errs} errors. Samples (minute, claims/s, UI MB, JVM MB):\n\n" + "\n".join(f"- {s}" for s in samples[::max(1, len(samples)//12)]))
    if errs: note("FAIL", f"soak: {errs} errors")
    if samples and samples[-1][3] > samples[0][3] * 1.5: note("WARN", "soak: JVM memory grew by more than 50%", f"{samples[0][3]} -> {samples[-1][3]} MB")

# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=["sweep", "inputs", "sheets", "load"])
    ap.add_argument("--fy", type=int, default=2027)
    ap.add_argument("--chunk-drgs", type=int, default=100)
    ap.add_argument("--soak", type=int, default=0, help="minutes of soak test (0 = skip)")
    ap.add_argument("--big", action="store_true", help="include the 50 MB / 1.1M-row spreadsheet case")
    a = ap.parse_args()
    global BIG; BIG = a.big
    os.makedirs(OUT, exist_ok=True)
    s, info = jget(f"{UI}/api/v1/info")
    if s != 200: sys.exit("UI/pricer not reachable at " + UI)
    s, profiles = jget(f"{UI}/profiles")
    sample = json.load(open(os.path.join(APP, "static", "sample-claim.json")))
    md(f"# IPPS Pricer UI stress test\n\nRun {date.today().isoformat()} against pricer {info['apiVersion']} (calc {info['supportedVersions'][str(a.fy)]}), {len(profiles):,} hospital profiles.\n")
    t0 = time.time()
    if "sweep" in a.only: part_sweep(profiles, sample, a.fy, a.chunk_drgs)
    if "inputs" in a.only: part_inputs(profiles, sample, a.fy)
    if "sheets" in a.only: part_sheets(profiles, sample)
    if "load" in a.only: part_load(profiles, sample)
    if a.soak: part_soak(profiles, sample, a.soak)
    md(f"\n## Findings\n")
    if not findings: md("No failures. Every case produced a return code or a validation message.")
    for sev, title, detail in findings: md(f"- **{sev}** {title}" + (f": {detail}" if detail else ""))
    md(f"\nTotal run time {(time.time()-t0)/60:.1f} min.")
    with open(os.path.join(HERE, "report.md"), "w") as f: f.write("\n".join(sections) + "\n")
    print("\nreport written to", os.path.join(HERE, "report.md"))

if __name__ == "__main__":
    main()
