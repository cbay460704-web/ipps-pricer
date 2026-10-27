#!/bin/sh
# Downloads the CMS IPPS Pricer jar (v2.14.1, FY2027) that this app wraps. Run once from this folder.
set -e
cd "$(dirname "$0")"
curl -L -o pricer.zip "https://www.cms.gov/files/zip/ipps-pricer-2027-0-v2-14-1-executable-jar.zip"
unzip -o -j pricer.zip "ipps-pricer-application-2.14.1.jar" -d .
rm -f pricer.zip
echo "Got ipps-pricer-application-2.14.1.jar. Now run:  python3 pricer_ui.py"
