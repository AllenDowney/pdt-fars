PYTHON ?= python
# A PublicDataToolkit checkout. Or `pip install -e $(PDT)` and run `pdt check`.
PDT ?= ../PublicDataToolkit

.PHONY: build data views check

## Build metadata records from data/raw (see build/build_slice.py)
build:
	PYTHONPATH=$(PDT)/lib $(PYTHON) build/build_slice.py

## Write the data release in the common format, data/processed/ (see scripts/build_data.py)
data:
	PYTHONPATH=$(PDT)/lib $(PYTHON) scripts/build_data.py

## Generate agent/, docs/ and export/ from the records (pdt views)
views:
	PYTHONPATH=$(PDT)/lib $(PYTHON) -m pdt views .

## Check the repo against the PublicDataToolkit standard (pdt check)
check:
	PYTHONPATH=$(PDT)/lib $(PYTHON) -m pdt check .
