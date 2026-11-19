# pdt-fars

Metadata for the [Fatality Analysis Reporting System](https://www.nhtsa.gov/research-data/fatality-analysis-reporting-system-fars) (FARS), built to the [PublicDataToolkit](https://github.com/AllenDowney/PublicDataToolkit) standard.

**Status:** preliminary. A 32-variable slice of the 2024 national files, in three tables: crashes (14 variables), vehicles (8) and people (10). **Not for publication** until the terms are reviewed.

## What the dataset is

FARS is NHTSA's census of fatal motor vehicle crashes in the US since 1975, coded from police reports and other records. Each year's data has about 30 files, linked by crash (`ST_CASE`), vehicle (`VEH_NO`) and person (`PER_NO`).

## Terms of use

Not yet reviewed by a person. NHTSA's manual states no license or restriction, and it is a US government work; see [`dataset.yaml`](dataset.yaml).

## How to get the data

From NHTSA's FARS data downloads, in a browser (the site blocks scripts); [`sources.yaml`](sources.yaml) lists the files and checksums.

## The metadata

- `metadata/releases/2024/<table>/variables.jsonl` for `accident`, `vehicle` and `person`: each element's title, its definition and additional information (in `notes`), its 2024 codes with "Not Reported" and "Unknown" typed by rule, and where each fact came from.
- The manual gives each element's codes for every year since 1975. This slice uses the 2024 column.

## How to cite

National Center for Statistics and Analysis. (2026, April). Fatality Analysis Reporting System analytical user's manual, 1975-2024 (Report No. DOT HS 813 794). National Highway Traffic Safety Administration. https://doi.org/10.21949/dz60-ft73

## How to rebuild

With the sources in `data/raw/`, the manual converted to `data/interim/manual.txt`, and a PublicDataToolkit checkout next to this repo:

```bash
make build
make check
```
