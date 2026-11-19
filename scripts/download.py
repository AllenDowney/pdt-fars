#!/usr/bin/env python3
"""Check the FARS files this repo is built from, as sources.yaml lists them.

    python scripts/download.py            # what is present, and whether it matches; no network
    python scripts/download.py --release 2017

Every FARS source is manual (`acquisition: manual`): nhtsa.gov blocks scripts and
headless browsers, so a person downloads the files in a browser, following each
source's manual_steps. This script never fetches anything. It always runs
`pdt download --check`, which reports each file's state and prints the steps for
any that are missing or don't match their recorded SHA-256.

Uses the PublicDataToolkit library (pdt.download); `make download` sets PYTHONPATH.
"""

import sys
from pathlib import Path

try:
    from pdt.download import main
except ImportError:
    sys.exit("Needs the PublicDataToolkit library: pip install -e ../PublicDataToolkit, "
             "or run through `make download`.")

ROOT = Path(__file__).resolve().parent.parent

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--check"]
    if any(a in ("--accept-terms", "--force") for a in args):
        sys.exit("Every FARS source is fetched by hand (see sources.yaml); this script only checks them.")
    sys.exit(main([str(ROOT), "--check", *args]))
