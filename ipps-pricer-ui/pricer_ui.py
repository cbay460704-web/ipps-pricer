#!/usr/bin/env python3
"""Local web UI for the CMS IPPS Pricer.

Starts the CMS pricer jar (a Dropwizard REST server) as a child process,
serves a browser form on http://localhost:8765, and proxies form submissions
to the pricer's /price-claim endpoint.

Usage:  python3 pricer_ui.py [--jar PATH] [--port 8765] [--pricer-port 3000]
"""
import argparse, json, os, shutil, signal, subprocess, sys, time, threading
import urllib.request, urllib.error
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_JAR = os.path.join(HERE, "ipps-pricer-application-2.14.1.jar")
YEARS = "2019,2020,2021,2022,2023,2024,2025,2026,2027"

def find_java():
    cands = [os.environ.get("JAVA_HOME", "") and os.path.join(os.environ["JAVA_HOME"], "bin", "java"),
             "/opt/homebrew/opt/openjdk@17/bin/java",
             "/usr/local/opt/openjdk@17/bin/java",
             shutil.which("java")]
    for c in cands:
        if c and os.path.exists(c):
            return c
    sys.exit("Java 17 not found. Install with:  brew install openjdk@17")

def start_pricer(jar, port):
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
            sys.exit(f"Pricer exited early (see pricer.log)")
        try:
            urllib.request.urlopen(base + "/openapi.json", timeout=1).read()
            return proc, base
        except Exception:
            time.sleep(0.5)
    proc.kill()
    sys.exit("Pricer did not start within 60s (see pricer.log)")

PROFILES = os.path.join(HERE, "profiles.json")

def read_profiles():
    """Provider profiles keyed by CCN, stored next to this script so they survive browser resets."""
    try:
        with open(PROFILES, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return b"{}"

class Handler(SimpleHTTPRequestHandler):
    base = None
    def __init__(self, *a, **k):
        super().__init__(*a, directory=os.path.join(HERE, "static"), **k)
    def log_message(self, fmt, *args):
        line = fmt % args if args else fmt
        if "/api/" in line or "/profiles" in line or "code " in line:
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
    def _send(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if self.path == "/profiles":
            return self._send(200, read_profiles())
        if self.path.startswith("/api/"):
            self._send(*self._proxy("GET", self.path[4:]))
        else:
            super().do_GET()
    def do_PUT(self):
        if self.path == "/profiles":
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n)
            try:
                data = json.loads(body)
                assert isinstance(data, dict)
            except Exception:
                return self._send(400, b'{"error":"profiles must be a JSON object keyed by CCN"}')
            with open(PROFILES, "w") as f:
                json.dump(data, f, indent=2)
            return self._send(200, b'{"ok":true}')
        self.send_error(404)
    def do_POST(self):
        if self.path.startswith("/api/"):
            n = int(self.headers.get("Content-Length", 0))
            self._send(*self._proxy("POST", self.path[4:], self.rfile.read(n)))
        else:
            self.send_error(404)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jar", default=DEFAULT_JAR)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--pricer-port", type=int, default=3000)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    print(f"Starting IPPS pricer from {a.jar} ...", flush=True)
    proc, base = start_pricer(a.jar, a.pricer_port)
    Handler.base = base
    print(f"Pricer up at {base}", flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://localhost:{a.port}/"
    print(f"UI at {url}  (Ctrl-C to stop)", flush=True)
    if not a.no_browser:
        threading.Timer(0.5, lambda: subprocess.run(["open", url]) if sys.platform == "darwin" else None).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        try: proc.wait(5)
        except subprocess.TimeoutExpired: proc.kill()

if __name__ == "__main__":
    main()
