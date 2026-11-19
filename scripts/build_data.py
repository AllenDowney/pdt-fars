#!/usr/bin/env python3
"""Write the FARS data release in the common format (STANDARD.md §8).

Reads each release's national CSV zip in data/raw and writes
data/processed/fars-<release>-<table>.parquet, one per table, holding the
variables this repo has records for. Each file is read back and every cell
compared with the CSV, so the conversion is lossless.
"""

import sys
from pathlib import Path

try:
    from pdt.readers import read_csv_zip, zip_member
    from pdt.release import data_source, write_release
except ImportError:
    sys.exit("Needs the PublicDataToolkit library: pip install -e ../PublicDataToolkit, "
             "or run through `make data`.")

ROOT = Path(__file__).resolve().parent.parent


def read(release, table, names):
    """The table's CSV inside the release's national CSV zip (accident.csv, Person.CSV, ...)."""
    source = data_source(ROOT, release, table, format="csv")
    path = ROOT / source["path"]
    if not path.exists():
        sys.exit(f"{path} is missing; see its manual_steps in sources.yaml")
    return read_csv_zip(path, zip_member(path, rf"(^|/){table}\.csv$"), names), source["id"]


if __name__ == "__main__":
    write_release(ROOT, read)
