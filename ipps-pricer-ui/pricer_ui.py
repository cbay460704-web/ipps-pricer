#!/usr/bin/env python3
"""Local web UI for the CMS IPPS Pricer.

Starts the CMS pricer jar (a Dropwizard REST server) as a child process,
serves a browser form on http://localhost:8765, proxies form submissions
to the pricer's /price-claim endpoint, stores hospital profiles, and runs
large spreadsheet batches server-side.

Usage:  python3 pricer_ui.py [--jar PATH] [--port 8765] [--pricer-port 3000]
No third-party packages are needed (xlsx files are read and written with the
standard library).
"""
import argparse, base64, csv, io, json, os, re, shutil, subprocess, sys, threading, time, uuid, zipfile
import urllib.request, urllib.error
import concurrent.futures as cf
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_JAR = os.path.join(HERE, "ipps-pricer-application-2.14.1.jar")
PROFILES = os.path.join(HERE, "profiles.json")
BATCH_DIR = os.path.join(HERE, "batches")
YEARS = "2019,2020,2021,2022,2023,2024,2025,2026,2027"

# ----------------------------------------------------------------------------
# Pricer process
# ----------------------------------------------------------------------------
def find_java():
    cands = [os.environ.get("JAVA_HOME", "") and os.path.join(os.environ["JAVA_HOME"], "bin", "java"),
             "/opt/homebrew/opt/openjdk@17/bin/java",
             "/usr/local/opt/openjdk@17/bin/java",
             shutil.which("java")]
    for c in cands:
        if c and os.path.exists(c):
            return c
    sys.exit("Java 17 not found. Install with:  brew install openjdk@17")

def pricer_already_running(base):
    try:
        urllib.request.urlopen(base + "/openapi.json", timeout=1).read()
        return True
    except Exception:
        return False

def start_pricer(jar, port):
    base = f"http://localhost:{port}"
    if pricer_already_running(base):
        print(f"A pricer is already listening on port {port}; reusing it.", flush=True)
        return None, base
    java = find_java()
    cmd = [java, "--add-opens", "java.base/java.lang=ALL-UNNAMED",
           f"-Ddw.supportedYears={YEARS}",
           f"-Ddw.server.applicationConnectors[0].port={port}",
           f"-Ddw.server.adminConnectors[0].port={port + 100}",
           "-jar", jar, "server"]
    env = dict(os.environ, COLUMNS="100")
    log = open(os.path.join(HERE, "pricer.log"), "w")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)
    base = f"http://localhost:{port}"
    for _ in range(120):
        if proc.poll() is not None:
            sys.exit("Pricer exited early (see pricer.log)")
        try:
            urllib.request.urlopen(base + "/openapi.json", timeout=1).read()
            return proc, base
        except Exception:
            time.sleep(0.5)
    proc.kill()
    sys.exit("Pricer did not start within 60s (see pricer.log)")

# ----------------------------------------------------------------------------
# Profiles
# ----------------------------------------------------------------------------
def read_profiles():
    """Provider profiles keyed by CCN, stored next to this script."""
    try:
        with open(PROFILES, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return b"{}"

# ----------------------------------------------------------------------------
# Spreadsheet reading (xlsx via zip+xml, csv/tsv via csv module)
# ----------------------------------------------------------------------------
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

def _col_index(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + (ord(ch) - 64)
    return n - 1

def read_xlsx_rows(data):
    """Yield rows (lists of cell values) from the first worksheet of an xlsx file."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        names = z.namelist()
        if not any(n.startswith("xl/worksheets/sheet") for n in names):
            raise ValueError("no worksheet inside")
    except (zipfile.BadZipFile, ValueError) as e:
        raise ValueError(f"not a valid .xlsx workbook ({e})")
    shared = []
    if "xl/sharedStrings.xml" in names:
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(NS + "si"):
            shared.append("".join(t.text or "" for t in si.iter(NS + "t")))
    # first sheet in workbook order
    sheet = None
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rid = next(iter(wb.iter(NS + "sheet"))).get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        for r in rels:
            if r.get("Id") == rid:
                sheet = "xl/" + r.get("Target").lstrip("/").removeprefix("xl/")
    except Exception:
        pass
    if not sheet or sheet not in names:
        sheet = sorted(n for n in names if n.startswith("xl/worksheets/sheet"))[0]
    for _, row in ET.iterparse(io.BytesIO(z.read(sheet))):
        if row.tag != NS + "row":
            continue
        cells = []
        for c in row.findall(NS + "c"):
            idx = _col_index(c.get("r", "A1"))
            t = c.get("t")
            v = c.find(NS + "v")
            if t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(NS + "t"))
            elif v is None or v.text is None:
                val = ""
            elif t == "s":
                val = shared[int(v.text)]
            elif t == "b":
                val = v.text == "1"
            elif t == "str" or t == "e":
                val = v.text
            else:
                try:
                    f = float(v.text); val = int(f) if f.is_integer() else f
                except ValueError:
                    val = v.text
            while len(cells) < idx:
                cells.append("")
            cells.append(val)
        yield cells
        row.clear()

def read_table(name, data):
    """Return (headers, rows) where rows are dicts keyed by header text."""
    if name.lower().endswith((".xlsx", ".xlsm")):
        it = read_xlsx_rows(data)
    else:
        text = data.decode("utf-8-sig", errors="replace")
        delim = "\t" if "\t" in text.split("\n", 1)[0] else ","
        it = csv.reader(io.StringIO(text), delimiter=delim)
    headers = None
    rows = []
    for cells in it:
        if headers is None:
            if not any(str(c).strip() for c in cells):
                continue
            headers = [str(c).strip() for c in cells]
            continue
        if not any(str(c).strip() for c in cells):
            continue
        rows.append({h: (cells[i] if i < len(cells) else "") for i, h in enumerate(headers) if h})
    return headers or [], rows

# ----------------------------------------------------------------------------
# Spreadsheet writing (minimal xlsx with inline strings)
# ----------------------------------------------------------------------------
def _xml_escape(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def write_xlsx(path, headers, rows_iter):
    def cell(ref, v):
        if v is None or v == "":
            return ""
        if isinstance(v, bool):
            return f'<c r="{ref}" t="b"><v>{int(v)}</v></c>'
        if isinstance(v, (int, float)):
            return f'<c r="{ref}"><v>{v}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_xml_escape(v)}</t></is></c>'
    def col_letter(i):
        s = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26); s = chr(65 + r) + s
        return s
    letters = [col_letter(i) for i in range(len(headers))]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="results" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        with z.open("xl/worksheets/sheet1.xml", "w", force_zip64=True) as sheet:
            w = lambda t: sheet.write(t.encode("utf-8"))
            w('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>')
            w("<row r=\"1\">" + "".join(cell(f"{letters[i]}1", h) for i, h in enumerate(headers)) + "</row>")
            n = 1
            chunk = []
            for row in rows_iter:
                n += 1
                chunk.append(f'<row r="{n}">' + "".join(cell(f"{letters[i]}{n}", row[i]) for i in range(len(headers))) + "</row>")
                if len(chunk) >= 2000:
                    w("".join(chunk)); chunk = []
            w("".join(chunk) + "</sheetData></worksheet>")

# ----------------------------------------------------------------------------
# Row -> pricer request (mirrors the browser's rowToRequest; field rules come
# from the browser in the job context so the definitions live in one place)
# ----------------------------------------------------------------------------
def excel_date(serial):
    return (date(1899, 12, 30) + timedelta(days=int(serial))).isoformat()

def fmt_date(v):
    if isinstance(v, (int, float)) and 20000 < v < 80000:
        return excel_date(v)
    s = str(v).strip()
    if re.fullmatch(r"\d{5}(\.0+)?", s) and 20000 < float(s) < 80000:      # Excel serial that arrived as text (CSV)
        return excel_date(float(s))
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return s

def coerce(ctx, col, raw):
    sec, name = col.split(".", 1)
    f = ctx["fields"][sec].get(name)
    if f is None or raw is None or str(raw).strip() == "":
        return None
    t = f.get("t")
    if t == "number":
        try:
            x = float(str(raw).replace("$", "").replace(",", ""))
        except ValueError:
            return None
        return int(x) if x.is_integer() else x
    if t == "date":
        return fmt_date(raw)
    if t == "list":
        return [p for p in re.split(r"[;|,\s]+", str(raw)) if p]
    s = str(raw).strip()
    if isinstance(raw, (int, float)) and float(raw).is_integer():
        s = str(int(raw))
    elif re.fullmatch(r"\d+\.0+", s):                                       # "470.0" typed or exported as text
        s = s.split(".")[0]
    pad = ctx["pad"].get(col)
    if pad and s.isdigit():
        s = s.zfill(pad)
    if col.startswith("provider.cbsa") and s.isdigit():
        s = s.zfill(2 if int(s) < 100 else 5)
    return s

def apply_defaults(ctx, sec, out):
    for name, f in ctx["fields"][sec].items():
        if out.get(name) is None and f.get("d") is not None:
            out[name] = f["d"]
    for name, f in ctx["fields"][sec].items():
        if out.get(name) is None and f.get("dFrom") and out.get(f["dFrom"]) is not None:
            out[name] = out[f["dFrom"]]
    if sec == "claim" and out.get("coveredDays") is None and out.get("lengthOfStay") is not None:
        out["coveredDays"] = out["lengthOfStay"]
    if sec == "provider" and out.get("stateCode") is None and out.get("countyCode"):
        out["stateCode"] = str(out["countyCode"])[:2]
    return {k: v for k, v in out.items() if v is not None}

def row_to_request(ctx, profiles, row):
    claim, prov = {}, {}
    for header, raw in row.items():
        col = ctx["headerMap"].get(header)
        if not col or col == "label":
            continue
        v = coerce(ctx, col, raw)
        if v is None:
            continue
        (claim if col.startswith("claim.") else prov)[col.split(".", 1)[1]] = v
    ccn = claim.get("providerCcn") or ctx.get("currentCcn") or ctx["currentProvider"].get("providerCcn")
    if ccn in profiles:
        base = {k: v for k, v in profiles[ccn].items() if k not in ("name", "source")}
        hospital = f"{profiles[ccn].get('name') or ''} ({ccn})".strip()
    else:
        base = dict(ctx["currentProvider"])
        hospital = f"{ccn} (not saved; used current hospital form)" if ccn else "no hospital"
    provider = {**base, **prov}
    provider.setdefault("providerCcn", ccn)
    claim.setdefault("providerCcn", provider.get("providerCcn"))
    return {"claimData": apply_defaults(ctx, "claim", claim),
            "providerData": apply_defaults(ctx, "provider", provider)}, hospital

def dig(o, path):
    for k in path.split("."):
        if not isinstance(o, dict):
            return None
        o = o.get(k)
    return o

# ----------------------------------------------------------------------------
# Batch jobs
# ----------------------------------------------------------------------------
JOBS = {}
JOBS_LOCK = threading.Lock()
PREVIEW_ROWS = 300

def run_job(job, pricer_base, headers, rows, ctx):
    try:
        profiles = json.loads(read_profiles() or b"{}")
    except Exception:
        profiles = {}
    out_cols = ctx["outCols"]                      # [[name, jsonPath], ...]
    out_headers = headers + ["out." + c[0] for c in out_cols]
    csv_path = os.path.join(BATCH_DIR, job["id"] + "-results.csv")
    workers = max(1, min(32, int(ctx.get("concurrency", 8))))

    def price(i):
        req, hospital = row_to_request(ctx, profiles, rows[i])
        body = json.dumps(req).encode()
        r = urllib.request.Request(pricer_base + "/v2/price-claim", data=body, method="POST",
                                   headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                status, text = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read()
        except Exception as e:
            status, text = 0, json.dumps({"error": str(e)}).encode()
        try:
            res = json.loads(text)
        except Exception:
            res = {"raw": text.decode(errors="replace")[:300]}
        res["_status"] = status
        res["_hospital"] = hospital
        if status != 200:
            if res.get("violations"):
                res["_error"] = "; ".join(f"{v.get('field','').replace('priceClaim.arg0.','')} {v.get('message','')}" for v in res["violations"])
            else:
                res["_error"] = res.get("detail") or res.get("error") or res.get("raw", "")[:200]
        return i, req, res

    job.update(status="running", total=len(rows), done=0, paid=0, notPaid=0, errors=0, totalPayment=0.0, started=time.time())
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(out_headers)
        with cf.ThreadPoolExecutor(workers) as ex:
            for i, req, res in ex.map(price, range(len(rows))):
                row = rows[i]
                outs = []
                for name, path in out_cols:
                    v = dig(res, path)
                    outs.append("" if v is None else v)
                w.writerow([row.get(h, "") for h in headers] + outs)
                code = dig(res, "returnCodeData.code")
                paid = res["_status"] == 200 and isinstance(code, str) and code.isdigit() and int(code) < 50
                job["done"] += 1
                if res["_status"] != 200:
                    job["errors"] += 1
                elif paid:
                    job["paid"] += 1
                    job["totalPayment"] += float(dig(res, "paymentData.totalPayment") or 0)
                else:
                    job["notPaid"] += 1
                if i < PREVIEW_ROWS:
                    job["preview"].append({"i": i, "row": row, "req": req, "res": res})
    job["elapsed"] = round(time.time() - job["started"], 1)
    job["status"] = "writing"
    xlsx_path = csv_path[:-4] + ".xlsx"
    try:
        def rows_from_csv():
            with open(csv_path, newline="") as f:
                rd = csv.reader(f); next(rd)
                for r in rd:
                    yield [_num(c) for c in r]
        write_xlsx(xlsx_path, out_headers, rows_from_csv())
        job["xlsx"] = f"/batch/{job['id']}/results.xlsx"
    except Exception as e:
        job["xlsxError"] = str(e)
    job["csv"] = f"/batch/{job['id']}/results.csv"
    job["status"] = "done"

def _num(s):
    if s == "":
        return ""
    if re.fullmatch(r"-?\d+", s) and not (len(s) > 1 and s[0] == "0"):   # keep leading zeros as text
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s

# ----------------------------------------------------------------------------
# CMS Provider Specific File (PSF) import
#   Source: CMS Provider Data Service export, https://pds.webpricer.cms.gov/fiss/inpatient/export
#   (CSV, one record per provider per effective date; filters: fromDate, toDate, state, oscarNumber)
#   Names:  CMS Hospital General Information dataset (data.cms.gov provider-data, dataset xubh-q36u)
# ----------------------------------------------------------------------------
PSF_EXPORT = "https://pds.webpricer.cms.gov/fiss/inpatient/export"
HOSP_DIR_API = "https://data.cms.gov/provider-data/api/1/datastore/query/xubh-q36u/0"
NAMES_CACHE = os.path.join(HERE, "hospital-names.json")

# PSF column -> pricer field. Columns not listed are not used by the IPPS pricer.
PSF_MAP = {
    "oscarNumber": "providerCcn", "effectiveDate": "effectiveDate", "fiscalYearBeginningDate": "fiscalYearBeginDate",
    "terminationDate": "terminationDate", "waiverIndicator": "waiverIndicator", "intermediaryNumber": "intermediaryNumber",
    "providerType": "providerType", "temporaryReliefIndicator": "temporaryReliefIndicator", "federalPpsBlend": "federalPpsBlend",
    "state": "stateCode", "countyCode": "countyCode",
    "caseMixAdjustedCostPerDischarge_PpsFacilitySpecificRate": "ppsFacilitySpecificRate",
    "costOfLivingAdjustment": "costOfLivingAdjustment", "internsToBedsRatio": "internsToBedsRatio", "bedSize": "bedSize",
    "operatingCostToChargeRatio": "operatingCostToChargeRatio", "supplementalSecurityIncomeRatio": "supplementalSecurityIncomeRatio",
    "medicaidRatio": "medicaidRatio", "specialPaymentIndicator": "specialPaymentIndicator", "hospitalQualityIndicator": "hospitalQualityIndicator",
    "actualGeographicLocation_CBSA": "cbsaActualGeographicLocation", "wageIndexLocation_CBSA": "cbsaWageIndexLocation",
    "standardizedAmountLocation_CBSA": "cbsaStandardizedAmountLocation", "specialWageIndex": "specialWageIndex",
    "passThroughAmountForCapital": "passThroughAmountForCapital",
    "passThroughAmountForDirectMedicalEducation": "passThroughAmountForDirectMedicalEducation",
    "passThroughAmountForOrganAcquisition": "passThroughAmountForOrganAcquisition", "totalPassThroughAmount": "passThroughTotalAmount",
    "capitalPpsPaymentCode": "capitalPpsPaymentCode", "oldCapitalHoldHarmlessRate": "oldCapitalHoldHarmlessRate",
    "capitalCostToChargeRatio": "capitalCostToChargeRatio", "newHospital": "newHospital",
    "capitalIndirectMedicalEducationRatio": "capitalIndirectMedicalEducationRatio", "capitalExceptionPaymentRate": "capitalExceptionPaymentRate",
    "valueBasedPurchasingParticipant": "vbpParticipantIndicator", "valueBasedPurchasingAdjustment": "vbpAdjustment",
    "hospitalReadmissionsReductionProgramParticipant": "hrrParticipantIndicator", "hospitalReadmissionsReductionAdjustment": "hrrAdjustment",
    "bundleModel1Discount": "bundleModel1Discount", "hospitalAcquiredConditionReductionProgramParticipant": "hacReductionParticipantIndicator",
    "uncompensatedCareAmount": "uncompensatedCareAmount", "electronicHealthRecordsProgramReduction": "ehrReductionIndicator",
    "lowVolumeAdjustmentFactor": "lowVolumeAdjustmentFactor", "medicarePerformanceAdjustment": "medicarePerformanceAdjustment",
    "ltchDppIndicator": "ltchDppIndicator", "supplementalWageIndex": "supplementalWageIndex",
    "supplementalWageIndexFlag": "supplementalWageIndexIndicator",
    "passThroughAmountForAllogenicStemCellAcquisition": "passThroughAmountForAllogenicStemCellAcquisition",
    "passThroughAmountForSupplyChain": "passThroughAmountForSupplyChainCosts",
}
PSF_DATES = {"effectiveDate", "fiscalYearBeginDate", "terminationDate"}
PSF_NUMBERS = {"ppsFacilitySpecificRate","costOfLivingAdjustment","internsToBedsRatio","bedSize","operatingCostToChargeRatio",
    "supplementalSecurityIncomeRatio","medicaidRatio","specialWageIndex","passThroughAmountForCapital","passThroughAmountForDirectMedicalEducation",
    "passThroughAmountForOrganAcquisition","passThroughTotalAmount","oldCapitalHoldHarmlessRate","capitalCostToChargeRatio",
    "capitalIndirectMedicalEducationRatio","capitalExceptionPaymentRate","vbpAdjustment","hrrAdjustment","bundleModel1Discount",
    "uncompensatedCareAmount","lowVolumeAdjustmentFactor","medicarePerformanceAdjustment","supplementalWageIndex",
    "passThroughAmountForAllogenicStemCellAcquisition","passThroughAmountForSupplyChainCosts"}

def is_ipps_ccn(ccn):
    """Short-term acute-care hospitals carry CCN sequence numbers 0001-0879."""
    tail = ccn[-4:]
    return tail.isdigit() and 1 <= int(tail) <= 879

def psf_to_profile(r):
    out = {}
    for col, field in PSF_MAP.items():
        v = (r.get(col) or "").strip()
        if v == "":
            continue
        if field in PSF_DATES:
            if re.fullmatch(r"\d{8}", v):
                v = f"{v[:4]}-{v[4:6]}-{v[6:]}"
        elif field in PSF_NUMBERS:
            try:
                x = float(v); v = int(x) if field == "bedSize" else x
            except ValueError:
                continue
        elif field.startswith("cbsa"):
            v = v.zfill(2 if len(v) <= 2 else 5)
        elif field in ("vbpParticipantIndicator", "hacReductionParticipantIndicator"):
            v = {"1": "Y", "0": "N"}.get(v, v)
        out[field] = v
    # the PSF keeps historic termination dates on active records; the pricer only accepts one at/after the effective date
    if out.get("terminationDate") and out["terminationDate"] < out.get("effectiveDate", ""):
        del out["terminationDate"]
    if out.get("ehrReductionIndicator") not in (None, "Y"):
        del out["ehrReductionIndicator"]
    return out

def fetch_hospital_names():
    """CCN -> {name, city, state} from the CMS hospital directory, cached for a day."""
    try:
        st = os.stat(NAMES_CACHE)
        if time.time() - st.st_mtime < 86400:
            return json.load(open(NAMES_CACHE))
    except FileNotFoundError:
        pass
    names = {}
    off = 0
    while True:
        d = json.load(urllib.request.urlopen(f"{HOSP_DIR_API}?limit=1500&offset={off}", timeout=90))
        res = d.get("results", [])
        for h in res:
            names[h["facility_id"]] = {"name": h.get("facility_name", "").title(), "city": h.get("citytown", "").title(), "state": h.get("state", "")}
        if len(res) < 1500:
            break
        off += 1500
    json.dump(names, open(NAMES_CACHE, "w"))
    return names

def import_psf(rows, as_of, states, ipps_only, with_names):
    """rows: PSF CSV dict rows. Returns (profiles dict keyed by CCN, stats)."""
    as_of_c = as_of.replace("-", "")
    best = {}
    seen = skipped_type = skipped_state = 0
    for r in rows:
        seen += 1
        ccn = (r.get("oscarNumber") or "").strip()
        eff = (r.get("effectiveDate") or "").strip()
        if not ccn or not eff or eff > as_of_c:
            continue
        if ipps_only and not (is_ipps_ccn(ccn) and (r.get("capitalPpsPaymentCode") or "").strip() in ("B", "C")):
            skipped_type += 1; continue
        if states and (r.get("state") or "").strip() not in states:
            skipped_state += 1; continue
        if ccn not in best or eff > best[ccn]["effectiveDate"]:
            best[ccn] = r
    names = {}
    if with_names:
        try:
            names = fetch_hospital_names()
        except Exception as e:
            names = {"_error": str(e)}
    profiles = {}
    for ccn, r in best.items():
        prof = psf_to_profile(r)
        n = names.get(ccn)
        prof["name"] = f"{n['name']}, {n['city']} {n['state']}".strip(", ") if n else ""
        prof["source"] = f"CMS PSF effective {prof.get('effectiveDate','')}, imported {date.today().isoformat()}"
        profiles[ccn] = prof
    stats = {"recordsRead": seen, "hospitals": len(profiles), "skippedNotIpps": skipped_type, "skippedState": skipped_state,
             "named": sum(1 for p in profiles.values() if p["name"]), "namesError": names.get("_error")}
    return profiles, stats

STATE_ABBR = {"01":"AL","02":"AK","03":"AZ","04":"AR","05":"CA","06":"CO","07":"CT","08":"DE","09":"DC","10":"FL","11":"GA","12":"HI","13":"ID",
    "14":"IL","15":"IN","16":"IA","17":"KS","18":"KY","19":"LA","20":"ME","21":"MD","22":"MA","23":"MI","24":"MN","25":"MS","26":"MO","27":"MT",
    "28":"NE","29":"NV","30":"NH","31":"NJ","32":"NM","33":"NY","34":"NC","35":"ND","36":"OH","37":"OK","38":"OR","39":"PA","40":"PR","41":"RI",
    "42":"SC","43":"SD","44":"TN","45":"TX","46":"UT","47":"VT","48":"VI","49":"VA","50":"WA","51":"WV","52":"WI","53":"WY","54":"GU","55":"AS","56":"MP"}
ABBR_STATE = {v: k for k, v in STATE_ABBR.items()}

def run_psf_import(p):
    """p: {source:'cms'|'file', file?, name?, asOf, states?, ippsOnly, replace, names}"""
    as_of = p.get("asOf") or date.today().isoformat()
    abbrs = [x.strip().upper() for x in re.split(r"[,\s]+", p.get("states") or "") if x.strip()]
    state_codes = {ABBR_STATE[a] for a in abbrs if a in ABBR_STATE}
    if p.get("source") == "file":
        data = base64.b64decode(p["file"])
    else:
        # the PSF carries one record per effective date; 15 months back guarantees the current record for every provider
        y, m, d = map(int, as_of.split("-"))
        frm = date(y - 1 if m > 3 else y - 2, (m - 3 - 1) % 12 + 1, 1).isoformat()
        url = f"{PSF_EXPORT}?fromDate={frm}&toDate={as_of}" + "".join(f"&state={a}" for a in abbrs)
        data = urllib.request.urlopen(url, timeout=600).read()
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "oscarNumber" not in rows[0]:
        raise ValueError("that does not look like a CMS inpatient PSF export (no oscarNumber column)")
    profiles, stats = import_psf(rows, as_of, state_codes, p.get("ippsOnly", True), p.get("names", True))
    existing = {} if p.get("replace") else json.loads(read_profiles() or b"{}")
    existing.update(profiles)
    with open(PROFILES, "w") as f:
        json.dump(existing, f, indent=1)
    stats["totalProfiles"] = len(existing)
    stats["asOf"] = as_of
    return stats

# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------
class Handler(SimpleHTTPRequestHandler):
    base = None
    def __init__(self, *a, **k):
        super().__init__(*a, directory=os.path.join(HERE, "static"), **k)
    def log_message(self, fmt, *args):
        line = fmt % args if args else fmt
        if "/api/" in line or "/profiles" in line or "/batch" in line or "/psf" in line or "code " in line:
            sys.stderr.write(f"[ui] {line}\n")
    def _proxy(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            return 502, json.dumps({"error": f"pricer unreachable: {e}"}).encode()
    def _send(self, status, data, ctype="application/json", extra=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)
    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n)
    def _send_file(self, path, download_name):
        if not os.path.exists(path):
            return self._send(404, {"error": "not found"})
        ctype = "text/csv" if path.endswith(".csv") else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def do_GET(self):
        if self.path == "/profiles":
            return self._send(200, read_profiles())
        m = re.match(r"^/batch/([0-9a-f]{12})(?:/(results\.csv|results\.xlsx))?$", self.path)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                return self._send(404, {"error": "no such job"})
            if m.group(2):
                return self._send_file(os.path.join(BATCH_DIR, f"{job['id']}-{m.group(2)}"), f"{job['name']}-{m.group(2)}")
            pub = {k: v for k, v in job.items() if k != "preview"}
            if self.headers.get("X-Preview") == "1":
                pub["preview"] = job["preview"]
            return self._send(200, pub)
        if self.path.startswith("/api/"):
            return self._send(*self._proxy("GET", self.path[4:]))
        super().do_GET()

    def do_PUT(self):
        if self.path == "/profiles":
            try:
                data = json.loads(self._body())
                assert isinstance(data, dict)
            except Exception:
                return self._send(400, {"error": "profiles must be a JSON object keyed by CCN"})
            with open(PROFILES, "w") as f:
                json.dump(data, f, indent=2)
            return self._send(200, {"ok": True})
        self.send_error(404)

    def do_POST(self):
        if self.path == "/batch/headers":
            # Return just the header row of an uploaded file so the browser can map columns.
            try:
                p = json.loads(self._body())
                data = base64.b64decode(p["file"])
                headers, rows = read_table(p["name"], data)
                return self._send(200, {"headers": headers, "rows": len(rows)})
            except Exception as e:
                return self._send(400, {"error": f"could not read file: {e}"})
        if self.path == "/psf/import":
            try:
                stats = run_psf_import(json.loads(self._body()))
                return self._send(200, stats)
            except Exception as e:
                return self._send(400, {"error": str(e)})
        if self.path == "/batch":
            try:
                p = json.loads(self._body())
                data = base64.b64decode(p["file"])
                headers, rows = read_table(p["name"], data)
                ctx = p["context"]
                ctx["fields"] = {"claim": {f["n"]: f for f in ctx["claimFields"]}, "provider": {f["n"]: f for f in ctx["providerFields"]}}
            except Exception as e:
                return self._send(400, {"error": f"bad batch request: {e}"})
            os.makedirs(BATCH_DIR, exist_ok=True)
            job_id = uuid.uuid4().hex[:12]
            job = {"id": job_id, "name": re.sub(r"\.[^.]+$", "", os.path.basename(p["name"])) or "claims",
                   "status": "queued", "total": len(rows), "done": 0, "preview": [], "headers": headers}
            with JOBS_LOCK:
                JOBS[job_id] = job
            threading.Thread(target=run_job, args=(job, self.base, headers, rows, ctx), daemon=True).start()
            return self._send(200, {"id": job_id, "total": len(rows), "headers": headers})
        if self.path.startswith("/api/"):
            return self._send(*self._proxy("POST", self.path[4:], self._body()))
        self.send_error(404)

# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jar", default=DEFAULT_JAR)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--pricer-port", type=int, default=3000)
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to let other machines on the network connect")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    print(f"Starting IPPS pricer from {a.jar} ...", flush=True)
    proc, base = start_pricer(a.jar, a.pricer_port)
    Handler.base = base
    print(f"Pricer up at {base}", flush=True)
    try:
        srv = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError as e:
        if proc: proc.terminate()
        sys.exit(f"Port {a.port} is already in use (is another pricer_ui.py running?). "
                 f"Stop it or start with --port <other>. ({e})")
    url = f"http://localhost:{a.port}/"
    print(f"UI at {url}  (Ctrl-C to stop)", flush=True)
    if not a.no_browser:
        threading.Timer(0.5, lambda: subprocess.run(["open", url]) if sys.platform == "darwin" else None).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if proc:
            proc.terminate()
            try: proc.wait(5)
            except subprocess.TimeoutExpired: proc.kill()

if __name__ == "__main__":
    main()
