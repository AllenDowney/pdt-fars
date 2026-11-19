"""Build the FARS slice's metadata records from the Analytical User's Manual and the national CSV files.

Usage:
    python build/build_slice.py

Reads build/slice.yaml, data/interim/manual.txt (the 1975-2024 Analytical
User's Manual, converted with `pdftotext -layout`) and each release's national
CSV files. Writes metadata/releases/<release>/<table>/variables.jsonl,
metadata/histories.jsonl, metadata/links.jsonl,
metadata/derived/scale_signature.jsonl and build/report.md.

Where each field comes from:
- label (the element's title), definition and additional information (in
  notes), codes and labels for 2024: the manual
- which codes occur, and the data's own labels (the CSV's <NAME>NAME columns):
  the CSV files, as a cross-check
- measure: build/slice.yaml (hand)
- which codes are missing, and why: a rule over labels (fars_missing_from_labels),
  because the manual doesn't mark missing codes

The manual gives each element's codes as a matrix of year ranges, e.g.
1975-1979 | 1980-2008 | 2009 | 2010-2017 | 2018-Later. A release's codes are the
column whose years include the release's data year (its id). Each element's data file is in the page's running
header ("... The PERSON Data File").
"""

import collections
import csv
import json
import re
from pathlib import Path

import yaml

# Shared rules from the PublicDataToolkit library (STANDARD.md §6): `make build` puts it on
# PYTHONPATH; otherwise use a checkout next to this repo, or $PDT.
try:
    from pdt.derived import scale_signatures
except ImportError:
    import os, sys
    sys.path.insert(0, str(Path(os.environ.get("PDT", Path(__file__).resolve().parents[2] / "PublicDataToolkit")) / "lib"))
    from pdt.derived import scale_signatures

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
META = ROOT / "metadata"

MANUAL = "fars-manual-1975-2024"
RULE_VERSION = "0.1"
MISSING_RULE_VERSION = "0.2"  # fars_missing_from_labels; 0.2 types "Unknown" as unknown (STANDARD.md §4.2), not dk

# ------------------------------------------------------------ the manual

RUNNING_HEADER = re.compile(r"Data Element Definitions and Codes\s+(?:The (\w+) Data File|(Key Data Elements))")
PAGE_NOISE = re.compile(r"^\s*(\d+|[A-H]-\d+)\s*$|Fatality Analysis Reporting System Analytical User’s Manual")
ELEMENT_ID = re.compile(r"^([A-Z]{1,3}\d+[A-Za-z]?)\.\s+(.*)$")
YEAR_TOKEN = re.compile(r"^(\d{4}-?|\d{4}-\d{4}|\d{4}-Later|Later)$")


PAGE_NUMBER = re.compile(r"^\s*(\d+|[A-H]-\d+)\s*$")


def clean_lines(path):
    """The manual's lines without page numbers and running headers, as (data file, line, page).

    The page number is the footer that follows the line.
    """
    current_file = None
    out, pending = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = RUNNING_HEADER.search(line)
        if m:
            current_file = m.group(1) or "KEY"
            continue
        if (p := PAGE_NUMBER.match(line)):
            for k in pending:
                out[k] = out[k][:2] + (p.group(1),)
            pending = []
        if PAGE_NOISE.search(line):
            continue
        pending.append(len(out))
        out.append((current_file, line.rstrip(), None))
    return out


def is_year_header(line):
    tokens = line.split()
    return bool(tokens) and all(YEAR_TOKEN.match(t) for t in tokens)


def index_elements(element_list):
    """Return {(data file, SAS name): element}, the element each name refers to in 2024."""
    elements = {}
    for element in element_list:
        for name, years in element["names"]:
            key = (element["file"], name)
            # A discontinued element can share a SAS name with the current one, and a name can be
            # an older name of another element (PARKWORK's HARM_EV, 2005-2009). Keep the element
            # coded for 2024 under the name it uses now.
            if key in elements and is_current(elements[key], dict(elements[key]["names"])[name]):
                continue
            elements[key] = element
    return elements


def parse_element_list(path):
    """Every element in the manual, in order, current and discontinued."""
    lines = clean_lines(path)
    out = []
    for i, (fname, line, _) in enumerate(lines):
        if line.strip() != "SAS Name":
            continue
        # The SAS name(s): one or more lines, each "NAME[, NAME...] [years]", e.g.
        # "HARM_EV 2005-2009" then "PHARM_EV 2010-Later" (a rename).
        names, j = [], i + 1
        while j < len(lines) and not lines[j][1].strip():
            j += 1
        while j < len(lines) and (m := SAS_NAME_LINE.match(lines[j][1].strip())):
            years = m.group(2)
            names += [(n, years) for n in re.split(r",\s*", m.group(1))]
            j += 1
            while j < len(lines) and not lines[j][1].strip() and j + 1 < len(lines) \
                    and SAS_NAME_LINE.match(lines[j + 1][1].strip()):
                j += 1
        j -= 1  # the last name line
        # Back up to "Definition"; the title is the line before it.
        k = i
        while k > 0 and lines[k][1].strip() != "Definition":
            k -= 1
        title_line = next((lines[t][1].strip() for t in range(k - 1, max(k - 4, -1), -1) if lines[t][1].strip()), "")
        m = ELEMENT_ID.match(title_line)
        element_id, title = (m.group(1), m.group(2)) if m else (None, title_line)
        body = [l for _, l, _ in lines[k + 1:i]]
        text = "\n".join(body)
        definition, _, additional = text.partition("Additional Information")
        # Attribute codes: after "Attribute Codes", up to the next element's "Definition" (minus its title).
        codes, a = [], j + 1
        while a < len(lines) and lines[a][1].strip() != "Attribute Codes":
            if lines[a][1].strip() == "Definition":
                break
            a += 1
        if a < len(lines) and lines[a][1].strip() == "Attribute Codes":
            b = a + 1
            while b < len(lines) and lines[b][1].strip() != "Definition" and lines[b][1].strip() != "SAS Name":
                codes.append(lines[b][1])
                b += 1
            if b < len(lines) and lines[b][1].strip() == "Definition":
                codes = codes[:-1] if codes and codes[-1].strip() else codes  # drop the next element's title
        element = {"id": element_id, "title": title, "file": lines[i][0],
                   "definition": " ".join(definition.split()), "additional": " ".join(additional.split()),
                   "names": names, "codes": parse_code_blocks(codes),
                   "page": lines[a][2] if a < len(lines) else None}  # where the code matrix starts
        out.append(element)
    return out


SAS_NAME_LINE = re.compile(r"^([A-Z][A-Z0-9_]*(?:,\s*[A-Z][A-Z0-9_]*)*)(?:\s+(\d{4}(?:-(?:\d{4}|Later))?))?$")


def is_current(element, years):
    """Whether the element is coded for 2024 under a name that's in use in 2024."""
    return current_codes(element)[0] is not None and (years is None or years.endswith("Later") or years == "2024")


ROW_CELL = re.compile(r"^(--|-?\d+(-\d+)?|x+)$")
OVERPRINTED_CELL = re.compile(r"^(--|\d+)(--|\d+)$")


def parse_code_blocks(lines):
    """Return a list of blocks: {"header": [...], "header2": [...], "rows": [(cells, label)]}.

    A table that continues onto the next page repeats its (one- or two-line)
    year header; that continues the same block. A wrapped label is indented to
    the label column; any other line that isn't a row ("More information on
    ...", the next element's title) ends the table.
    """
    blocks, skip_header2 = [], False
    for line in lines:
        if not line.strip():
            continue
        if is_year_header(line):
            tokens = line.split()
            block = blocks[-1] if blocks else None
            if skip_header2 and block is not None and tokens == block["header2"]:
                skip_header2 = False
            elif block is not None and block["rows"] and tokens == block["header"]:
                skip_header2 = bool(block["header2"])  # the header repeated after a page break
            elif block is not None and not block["rows"] and not block["header2"]:
                block["header2"] = tokens  # the second line of a two-line header
            else:
                blocks.append({"header": tokens, "header2": [], "rows": []})
                skip_header2 = False
            continue
        skip_header2 = False
        if not blocks:
            continue
        k = len(blocks[-1]["header"])
        tokens = line.split()
        if len(tokens) > k and all(OVERPRINTED_CELL.match(c) for c in tokens[:k]) \
                and not all(ROW_CELL.match(c) for c in tokens[:k]) \
                and len(re.split(r"(?<=[a-z)])(?=[A-Z])", " ".join(tokens[k:]), maxsplit=1)) == 2:
            # Two rows printed on one line, a pdftotext artifact (PARKWORK's PHARM_EV, codes 98):
            # "----  98--  ----  ----  --98  Not Reported (2010 Only)Harmful Event,". Split the
            # cells, and the label where a word runs into a capital; a wrapped line goes to the second.
            cells = [OVERPRINTED_CELL.match(c).groups() for c in tokens[:k]]
            first, second = re.split(r"(?<=[a-z)])(?=[A-Z])", " ".join(tokens[k:]), maxsplit=1)
            blocks[-1]["rows"].append(([c[0] for c in cells], first))
            blocks[-1]["rows"].append(([c[1] for c in cells], second))
        elif len(tokens) > k and all(ROW_CELL.match(c) for c in tokens[:k]):
            blocks[-1]["rows"].append((tokens[:k], " ".join(tokens[k:])))
        elif blocks[-1]["rows"] and len(line) - len(line.lstrip()) >= 10:
            cells, label = blocks[-1]["rows"][-1]
            blocks[-1]["rows"][-1] = (cells, label + " " + line.strip())  # a wrapped label
        else:
            break  # the table has ended
    return blocks


def column_codes(element, year):
    """The column for one data year: (codes [(token, label)], its period, e.g. "2010-2017"), or (None, reason)."""
    for block in element["codes"]:
        for i, p in enumerate(block_periods(block)):
            if p["from"] <= year <= p.get("to", 9999):
                period = f"{p['from']}-{p.get('to', 'Later')}" if p.get("to") != p["from"] else str(p["from"])
                return [(cells[i], label) for cells, label in block["rows"] if cells[i] != "--"], period
    return None, f"no column for {year}"


def current_codes(element):
    """The 2024 column: (codes [(token, label)], header text), or (None, reason)."""
    for block in reversed(element["codes"]):
        header = " ".join(block["header"] + block.get("header2", []))
        if "Later" in header or "2024" in header:
            return [(cells[-1], label) for cells, label in block["rows"] if cells[-1] != "--"], header
    return None, "no column for 2024"


# ----------------------------------------------------------------- rules

# Rule fars_missing_from_labels: the manual doesn't mark missing codes. FARS is
# a census coded from police reports, not a survey, so "Unknown" means the
# records didn't say: the type `unknown`, not `dk`.
MISSING_LABELS = {
    "not reported": ["no_answer"],
    "reported as unknown": ["unknown"],
    "unknown": ["unknown"],
}


def missing_types(label):
    key = label.strip().lower()
    if key in MISSING_LABELS:
        return MISSING_LABELS[key]
    if key.startswith("unknown") or key.startswith("reported as unknown"):
        return ["unknown"]
    return None


# ----------------------------------------------------------------- build


def read_csv(path, names):
    """{name: Counter of values} and {name: {code: Counter of NAME labels}} for the wanted columns."""
    values = {n: collections.Counter() for n in names}
    labels = {n: collections.defaultdict(collections.Counter) for n in names}
    with open(path, encoding="latin-1", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
        for row in reader:
            for n in names:
                if n in row:
                    values[n][row[n]] += 1
                    if n + "NAME" in row:
                        labels[n][row[n]][row[n + "NAME"]] += 1
    return header, values, labels


def as_number(text):
    try:
        x = float(text)
    except ValueError:
        return None
    return int(x) if x.is_integer() else x


YEAR_NOTE = re.compile(r"\((?:since \d{4}|\d{4}-(?:later|\d{4}))\)", re.I)


def comparable(label):
    """A label reduced for comparison: no year notes, case, dashes, punctuation, or examples in parentheses."""
    text = YEAR_NOTE.sub("", label.lower())
    text = re.sub(r"\s*[(\[][^)\]]*[)\]]\s*$", "", text)  # a trailing parenthetical, e.g. examples
    return re.sub(r"[^a-z0-9]+", "", text)


minor = []  # label differences that are only wording detail


def build_variable(name, table, spec, rel, measure, element, counts, data_labels):
    """One release's record. `rel` is the release in slice.yaml, with its `id` (the data year) and `source`."""
    report, year = [], int(rel["id"])
    record = {"dataset": "fars", "release": rel["id"], "table": table, "name": name}
    if element is None:
        report.append(f"`{rel['id']}/{table}/{name}`: not found in the manual")
        element = {"title": "", "definition": "", "additional": "", "codes": [], "id": None}
    if element["title"]:
        record["label"] = element["title"]
    present = {as_number(v): n for v, n in counts.items() if v.strip() != "" and as_number(v) is not None}
    if present:
        record["storage_type"] = "integer" if all(isinstance(v, int) for v in present) else "float"
    if name in measure:
        record["measure"] = measure[name]
    if element["definition"]:
        record["description"] = element["definition"]
    if element["additional"]:
        record["notes"] = element["additional"]

    codes, header = column_codes(element, year) if element["codes"] else ([], "")
    values, documented, ranges, actual_values = [], {}, [], None
    if codes is None:
        report.append(f"`{rel['id']}/{table}/{name}`: {header}")
        codes = []
    for token, label in codes:
        if re.fullmatch(r"x+", token):
            actual_values = label  # actual values are valid, e.g. "xxxx Actual Model Year"
            continue
        rng = re.fullmatch(r"(-?\d+)-(-?\d+)", token)
        if rng:
            ranges.append((int(rng.group(1)), int(rng.group(2)), label))
            continue
        code = as_number(token)
        documented[code] = label
        types = missing_types(label)  # typed from the manual's label
        # The record uses the release's data file's own label when it has one (STANDARD.md §4.2):
        # the manual's labels cover 1975-2024 and can carry history ("(Since 2018)").
        seen = data_labels.get(str(code), {})
        manual_label, label = label, (seen.most_common(1)[0][0] if seen else label)
        if types:
            values.append({"code": code, "label": label, "kind": "missing", "missing_types": types,
                           "origin": {"origin": "rule", "rule": "fars_missing_from_labels", "rule_version": MISSING_RULE_VERSION,
                                      "source": MANUAL,
                                      "note": "The manual doesn't mark missing codes; kind and type from the label. "
                                              "FARS is coded from police reports, so 'Unknown' means the report didn't say."}})
        else:
            values.append({"code": code, "label": label, "kind": "valid"})
    if ranges:
        record["valid_range"] = {"min": min(r[0] for r in ranges), "max": max(r[1] for r in ranges),
                                 "note": "; ".join(f"{lo}-{hi}: {lab}" for lo, hi, lab in ranges)}

    # C4: codes in the data the manual doesn't list, and labels that disagree.
    for code, n in sorted(present.items(), key=lambda kv: kv[0]):
        in_range = any(lo <= code <= hi for lo, hi, _ in ranges)
        if code not in documented and not in_range and not actual_values and (values or ranges):
            values.append({"code": code, "label": "", "kind": "unresolved",
                           "origin": {"origin": "data_file", "source": rel["source"],
                                      "note": f"Occurs {n} times in the {year} data; not in the manual's codes for {year}."}})
            report.append(f"`{rel['id']}/{table}/{name}` code {code}: occurs {n} times, not in the manual; marked unresolved")
    for code, label in documented.items():  # documented holds the manual's labels
        seen = data_labels.get(str(code), {})
        if seen:
            data_label = seen.most_common(1)[0][0]
            if comparable(data_label) == comparable(label):
                continue
            if comparable(data_label).startswith(comparable(label)) or comparable(label).startswith(comparable(data_label)):
                minor.append((rel["id"], table, name, code))  # one adds examples or a qualifier
            else:
                report.append(f"`{rel['id']}/{table}/{name}` code {code}: manual says {label!r}, data says {data_label!r}")
    if values:
        record["values"] = values
    if "valid_range" not in record and (actual_values or measure.get(name) in {"identifier", "count", "continuous"}) and present:
        valid = [v for v in present if v not in documented or not missing_types(documented[v])]
        if valid:
            record["valid_range"] = {"min": min(valid), "max": max(valid),
                                     **({"note": actual_values} if actual_values else {})}

    n_valid = sum(n for v, n in present.items()
                  if v not in documented or not missing_types(documented[v]))
    record["availability"] = [{"wave": year, "n": n_valid}]
    record["source_location"] = {"source": MANUAL, **({"anchor": element["id"]} if element.get("id") else {})}

    hand = {"origin": "hand", "author": spec["author"], "date": spec["date"]}
    fields = {"description": {"origin": "publisher", "source": MANUAL, "note": "The element's Definition."},
              "notes": {"origin": "publisher", "source": MANUAL, "note": "The element's Additional Information."},
              "values": {"origin": "publisher", "source": MANUAL,
                         "note": f"Codes from the manual's column for {header}; labels from the {year} CSV's NAME "
                                 "column where it has one, else the manual; which codes are missing by rule (see each code)."},
              "availability": {"origin": "data_file", "source": rel["source"],
                               "note": "n counts rows whose value isn't a missing code."}}
    if "storage_type" in record:
        fields["storage_type"] = {"origin": "rule", "rule": "storage_type_from_values", "rule_version": RULE_VERSION,
                                  "source": rel["source"]}
    if "measure" in record:
        fields["measure"] = hand
    if "valid_range" in record and not ranges:
        fields["valid_range"] = {"origin": "data_file", "source": rel["source"],
                                 "note": "Min and max of the values that occur."}
    record["provenance"] = {"record": {"origin": "publisher", "source": MANUAL}, "fields": fields}
    return record, report


# ------------------------------------------------------------ histories

def span(years, default_from):
    """A name's years ("2005-2009", "2009", "2010-Later", or None for all) as {"from", "to"}."""
    if years is None:
        return {"from": default_from}
    lo, _, hi = years.partition("-")
    return {"from": int(lo)} if hi == "Later" else {"from": int(lo), "to": int(hi or lo)}


def block_periods(block):
    """A matrix's periods. A two-line header pairs each token ending in "-" with the next on line two."""
    second, out = list(block["header2"]), []
    for token in block["header"]:
        text = token + second.pop(0) if token.endswith("-") and second else token
        lo, _, hi = text.partition("-")
        out.append({"from": int(lo)} if hi == "Later" else {"from": int(lo), "to": int(hi or lo)})
    return out


def history_code(token):
    """A matrix cell: None for "--", a number, or a string for a range or a placeholder ("1-99", "xxxx")."""
    if token == "--":
        return None
    n = as_number(token)
    return token if n is None else n


def element_history(element, table, name):
    """The history (STANDARD.md §5.4) of one name of an element: its matrix, rows by periods."""
    periods, attributes = [], []
    for block in element["codes"]:
        ps = block_periods(block)
        before, after = len(periods), sum(len(block_periods(b)) for b in element["codes"]) - len(periods) - len(ps)
        attributes += [{"label": label, "codes": [None] * before + [history_code(c) for c in cells] + [None] * after}
                       for cells, label in block["rows"]]
        periods += ps
    # Names on one line are parallel variables (WEATHER1, WEATHER2); a line without this name
    # or a sibling's is an earlier name of all of them (WEATHER, 1975-2006).
    latest = latest_names(element)
    names = [{"name": n, "waves": span(y, periods[0]["from"])} for n, y in element["names"]
             if n == name or n not in latest]
    history = {"id": f"{table}/{name}", "table": table, "name": name,
               **({"element": element["id"]} if element["id"] else {}),
               "label": element["title"], "names": names, "periods": periods, "attributes": attributes,
               "origin": {"origin": "publisher", "source": MANUAL,
                          **({"location": f"p. {element['page']}"} if element["page"] else {})}}
    notes = []
    if len(element["codes"]) > 1:
        notes.append(f"The manual prints {len(element['codes'])} separate matrices; rows aren't matched across them.")
    if len(latest) > 1:
        notes.append(f"One element with several names: {', '.join(sorted(latest))}.")
    if notes:
        history["note"] = " ".join(notes)
    return history


def latest_names(element):
    """The names an element has in its last period: the names its histories are keyed by."""
    def end(years):
        return 9999 if years is None or years.endswith("Later") else int(years.split("-")[-1])
    last = max(end(y) for _, y in element["names"])
    return {n for n, y in element["names"] if end(y) == last}


def build_histories(spec, element_list, elements):
    """Histories for the slice's variables, and for discontinued elements that share their names."""
    histories, seen = [], set()
    for table, t in spec["tables"].items():
        files = (t["file"], "KEY")
        for name in t["variables"]:
            current = elements.get((t["file"], name)) or elements.get(("KEY", name))
            if current and current["codes"]:
                histories.append(element_history(current, table, name))
                seen.add((table, name))
            for other in element_list:
                if other is current or other["file"] not in files or not other["codes"] \
                        or name not in {n for n, _ in other["names"]}:
                    continue
                for last in sorted(latest_names(other)):
                    if (table, last) not in seen:
                        histories.append(element_history(other, table, last))
                        seen.add((table, last))
    return histories


def period_index(history, year):
    return next((i for i, p in enumerate(history["periods"]) if p["from"] <= year <= p.get("to", 9999)), None)


def history_column(history, year):
    """{attribute index: code} for the period that includes `year`."""
    i = period_index(history, year)
    if i is None:
        return {}
    return {k: a["codes"][i] for k, a in enumerate(history["attributes"]) if a["codes"][i] is not None}


def name_in(history, year):
    return next((n["name"] for n in history["names"]
                 if n["waves"]["from"] <= year <= n["waves"].get("to", 9999)), None)


def check_histories(histories, records):
    """Report lines: does each record's codes match its history's column for the record's year?"""
    by_id = {h["id"]: h for h in histories}
    lines, agree = [], 0
    for r in records:
        h = by_id.get(f"{r['table']}/{r['name']}")
        if h is None or "values" not in r:
            continue
        expected = [c for c in history_column(h, int(r["release"])).values() if isinstance(c, (int, float))]
        got = [v["code"] for v in r["values"] if v["kind"] != "unresolved"]
        if sorted(expected) == sorted(got):
            agree += 1
        else:
            lines.append(f"- `{r['release']}/{h['id']}`: the history has {sorted(set(expected) - set(got))} "
                         f"not in the record; the record has {sorted(set(got) - set(expected))} not in the history")
    return [f"- {len(histories)} histories; {agree} records with values agree with their history's column "
            "for the record's year", *lines]


def in_range(code, token):
    m = re.fullmatch(r"(-?\d+)-(-?\d+)", str(token))
    return bool(m) and int(m.group(1)) <= code <= int(m.group(2))


def compare_with_data(histories, release, data):
    """Test the histories against one release's data: each code that occurs, and its NAME label.

    `data` is {(table, name): (Counter of values, {value: Counter of NAME labels})}. Returns
    (summary counts, lines listing the disagreements).
    """
    counts, lines = collections.Counter(), []
    year = int(release)
    for h in histories:
        if (h["table"], h["name"]) not in data:
            continue
        values, labels = data[(h["table"], h["name"])]
        column = {code: h["attributes"][k]["label"] for k, code in history_column(h, year).items()}
        counts["histories"] += 1
        for raw, n in sorted(values.items(), key=lambda kv: (as_number(kv[0]) is None, as_number(kv[0]) or 0)):
            code = as_number(raw)
            if code is None:
                continue
            counts["codes"] += 1
            if code in column:
                seen = labels.get(raw)
                if not seen:
                    counts["predicted, no label in the data"] += 1
                    continue
                data_label, label = seen.most_common(1)[0][0], column[code]
                if data_label == label:
                    counts["predicted, same label"] += 1
                elif comparable(data_label) == comparable(label):
                    counts["predicted, label differs only in detail"] += 1
                else:
                    counts["predicted, label differs"] += 1
                    lines.append(f"- `{release}/{h['id']}` code {code}: history says {label!r}, data says {data_label!r}")
            elif any(in_range(code, t) or re.fullmatch(r"x+", str(t)) for t in column):
                counts["within a documented range"] += 1
            else:
                counts["not in the history"] += 1
                lines.append(f"- `{release}/{h['id']}` code {code}: occurs {n} times; not in the history's column for {year}")
    return counts, lines


LINK_RULE_VERSION = "0.1"
SINCE_NOTE = re.compile(r"\(Since (\d{4})\)", re.I)


def build_links(spec, histories, records):
    """Links between releases (STANDARD.md §5.2), by rule fars_links_from_history over each history's columns.

    - renamed: the history's name differs between the two years;
    - codes_changed: the set of codes differs, or one of the publisher's rows has a different code;
    - labels_changed: a code in both years belongs to rows with different labels (LGT_COND 9), or to
      one row whose label notes a change between the two years, "(Since 2018)": the manual keeps one
      row for WEATHER 99, "Unknown/Reported as Unknown (Since 2018)", and the data confirm the change;
    - same_item: none of these.
    """
    have = {(r["release"], r["table"], r["name"]) for r in records}
    links = []
    for pair in spec.get("links", []):
        a, b = pair["from"], pair["to"]
        ya, yb = int(a), int(b)
        for h in histories:
            na, nb = name_in(h, ya), name_in(h, yb)
            if (a, h["table"], na) not in have or (b, h["table"], nb) not in have:
                continue
            ca, cb = history_column(h, ya), history_column(h, yb)
            la = {code: h["attributes"][k]["label"] for k, code in ca.items()}
            lb = {code: h["attributes"][k]["label"] for k, code in cb.items()}
            changes, relations = [], []
            if na != nb:
                relations.append("renamed")
            recoded = [k for k in ca.keys() & cb.keys() if ca[k] != cb[k]]
            if set(la) != set(lb) or recoded:
                relations.append("codes_changed")
                changes += [f"{h['attributes'][k]['label']!r}: {ca[k]} -> {cb[k]}" for k in recoded]
                if set(la) - set(lb):
                    changes.append(f"dropped {sorted(set(la) - set(lb), key=str)}")
                if set(lb) - set(la):
                    changes.append(f"added {sorted(set(lb) - set(la), key=str)}")
            relabeled = [c for c in la.keys() & lb.keys() if la[c] != lb[c]]
            noted = [c for c in la.keys() & lb.keys() if la[c] == lb[c]
                     and any(ya < int(y) <= yb for y in SINCE_NOTE.findall(la[c]))]
            if relabeled or noted:
                relations.append("labels_changed")
                changes += [f"code {c}: {la[c]!r} -> {lb[c]!r}" for c in sorted(relabeled, key=str)]
                changes += [f"code {c}: {la[c]!r} notes a change" for c in sorted(noted, key=str)]
            link = {"from": {"release": a, "table": h["table"], "name": na},
                    "to": {"release": b, "table": h["table"], "name": nb},
                    "relations": relations or ["same_item"],
                    "origin": {"origin": "rule", "rule": "fars_links_from_history", "rule_version": LINK_RULE_VERSION,
                               "source": MANUAL,
                               "note": f"From the code history {h['id']} (metadata/histories.jsonl): the manual's "
                                       f"columns for {a} and {b}."}}
            if changes:
                more = f"; and {len(changes) - 8} more" if len(changes) > 8 else ""
                link["note"] = "; ".join(changes[:8]) + more
            links.append(link)
    return links


def check_keys_and_references(rel):
    """Check dataset.yaml's keys and references for this release against the data; return report lines.

    A key must be unique and never blank. A reference holds when every row it
    covers (those meeting `when`, or all rows) has its columns' values among the
    target table's.
    """
    dataset = yaml.safe_load((ROOT / "dataset.yaml").read_text())
    release = next(r for r in dataset["releases"] if r["id"] == rel["id"])
    rows = {}
    for t in release.get("tables", []):
        with open(RAW / rel["data"][t["id"]], encoding="latin-1", newline="") as f:
            rows[t["id"]] = list(csv.DictReader(f))
    lines = []
    for t in release.get("tables", []):
        data = rows[t["id"]]
        keys = [tuple(r[c] for c in t["key"]) for r in data]
        dupes = len(keys) - len(set(keys))
        blank = sum(any(v.strip() == "" for v in k) for k in keys)
        ok = "holds" if not dupes and not blank else "FAILS"
        lines.append(f"- `{rel['id']}/{t['id']}` key ({', '.join(t['key'])}): {ok}; {len(data):,} rows, "
                     f"{dupes} duplicates, {blank} blank")
        for ref in t.get("references", []):
            target = {tuple(r[c] for c in ref["columns"]) for r in rows[ref["table"]]}
            when = ref.get("when")
            covered = [r for r in data if when is None or as_number(r[when["column"]]) in when["in"]]
            missing = sum(tuple(r[c] for c in ref["columns"]) not in target for r in covered)
            cond = f" when {when['column']} in {when['in']}" if when else ""
            lines.append(f"- `{rel['id']}/{t['id']}` ({', '.join(ref['columns'])}) → `{ref['table']}`{cond}: "
                         f"{'holds' if not missing else 'FAILS'}; {len(covered):,} rows, {missing} without a match")
    return lines


def main():
    spec = yaml.safe_load((ROOT / "build" / "slice.yaml").read_text())
    measure = {n: m for m, ns in spec["measure"].items() for n in ns}
    element_list = parse_element_list(ROOT / "data" / "interim" / "manual.txt")
    elements = index_elements(element_list)
    histories = build_histories(spec, element_list, elements)
    write_jsonl(META / "histories.jsonl", histories)
    all_records, notes, summaries, sections = [], [], [], []
    for rid, rel in spec["releases"].items():
        rel = {"id": rid, **rel}
        data = {}
        for table, t in spec["tables"].items():
            header, values, labels = read_csv(RAW / rel["data"][table], t["variables"])
            header = [c.strip() for c in header]
            data.update({(table, n): (values[n], labels[n]) for n in t["variables"]})
            file_names = {name for (f, name) in elements if f in (t["file"], "KEY")}
            data_cols = {c for c in header if not c.endswith("NAME")}
            summaries.append(f"- `{rid}/{table}`: {len(t['variables'])} variables in the slice; data file has {len(header)} "
                             f"columns ({len(data_cols)} without the NAME columns); manual elements for {t['file']}: "
                             f"{len(file_names)}; data columns not in the manual: {sorted(data_cols - file_names)[:12] or 'none'}")
            records = []
            for name in t["variables"]:
                element = elements.get((t["file"], name)) or elements.get(("KEY", name))
                record, report = build_variable(name, table, spec, rel, measure, element, values[name], labels[name])
                records.append(record)
                notes += report
            write_jsonl(META / "releases" / rid / table / "variables.jsonl", records)
            all_records += records
        counts, disagreements = compare_with_data(histories, rid, data)
        sections += [f"### {rid}", "", "| Histories, and distinct codes in the data | Count |", "|---|---|",
                     *[f"| {k} | {v} |" for k, v in counts.items()], "",
                     *(disagreements or ["No disagreements."]), "",
                     "Keys and references in dataset.yaml, checked against the data:", "",
                     *check_keys_and_references(rel), ""]

    signatures = scale_signatures(all_records)
    write_jsonl(META / "derived" / "scale_signature.jsonl", signatures)
    links = build_links(spec, histories, all_records)
    write_jsonl(META / "links.jsonl", links)
    relations = collections.Counter(rel for link in links for rel in link["relations"])
    types = collections.Counter(tp for r in all_records for v in r.get("values", []) if v["kind"] == "missing"
                                for tp in v["missing_types"])
    lines = [f"# Build report: FARS slice, releases {', '.join(spec['releases'])}", "",
             "Generated by `build/build_slice.py`. Do not edit.", "",
             f"- Manual elements parsed: {len({id(e) for e in elements.values()})}", *summaries,
             f"- Unresolved codes: {sum(v['kind'] == 'unresolved' for r in all_records for v in r.get('values', []))}",
             f"- Codes whose manual and data labels differ only in detail (case, punctuation, year notes, added examples): {len(minor)}",
             f"- Scale signatures: {len(signatures)}", "", "## Missing types assigned, by number of codes", "",
             "| Type | Codes |", "|---|---|", *[f"| `{tp}` | {k} |" for tp, k in sorted(types.items())], "",
             "## Code histories (metadata/histories.jsonl)", "", *check_histories(histories, all_records), "",
             "## Histories tested against each release's data", "",
             "For each code that occurs in a release's data: is it in the history's column for that year, and does the "
             "history's label (the manual's, verbatim) match the CSV's NAME label?", "", *sections,
             "## Links (metadata/links.jsonl), by rule from the histories", "",
             *[f"- {', '.join(f'{p['from']} → {p['to']}' for p in spec.get('links', []))}: {len(links)} links"],
             *[f"- `{k}`: {v}" for k, v in sorted(relations.items())], "",
             *[f"- `{l['from']['table']}/{l['from']['name']}`: {', '.join(l['relations'])}. {l.get('note', '')}"
               for l in links if l["relations"] != ["same_item"]], "",
             "## Values and labels to look at", "", *([f"- {n}" for n in notes] or ["None."]), ""]
    (ROOT / "build" / "report.md").write_text("\n".join(lines))
    print(f"{len(all_records)} records in {len(spec['releases'])} releases, {len(links)} links; see build/report.md")


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
