"""Build the FARS slice's metadata records from the Analytical User's Manual and the 2024 CSV files.

Usage:
    python build/build_slice.py

Reads build/slice.yaml, data/interim/manual.txt (the 1975-2024 Analytical
User's Manual, converted with `pdftotext -layout`) and the 2024 national CSV
files. Writes metadata/releases/2024/<table>/variables.jsonl,
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
1975-1979 | 1980-2008 | 2009 | 2010-2017 | 2018-Later. The 2024 codes are the
column ending in "Later". Each element's data file is in the page's running
header ("... The PERSON Data File").
"""

import collections
import csv
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
META = ROOT / "metadata"

MANUAL = "fars-manual-1975-2024"
RULE_VERSION = "0.1"

# ------------------------------------------------------------ the manual

RUNNING_HEADER = re.compile(r"Data Element Definitions and Codes\s+(?:The (\w+) Data File|(Key Data Elements))")
PAGE_NOISE = re.compile(r"^\s*(\d+|[A-H]-\d+)\s*$|Fatality Analysis Reporting System Analytical User’s Manual")
ELEMENT_ID = re.compile(r"^([A-Z]{1,3}\d+[A-Za-z]?)\.\s+(.*)$")
YEAR_TOKEN = re.compile(r"^(\d{4}-?|\d{4}-\d{4}|\d{4}-Later|Later)$")


def clean_lines(path):
    """The manual's lines without page numbers and running headers, each with its data file."""
    current_file = None
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = RUNNING_HEADER.search(line)
        if m:
            current_file = m.group(1) or "KEY"
            continue
        if PAGE_NOISE.search(line):
            continue
        out.append((current_file, line.rstrip()))
    return out


def is_year_header(line):
    tokens = line.split()
    return bool(tokens) and all(YEAR_TOKEN.match(t) for t in tokens)


def parse_elements(path):
    """Return {(data file, SAS name): element} for every element in the manual."""
    lines = clean_lines(path)
    elements = {}
    for i, (fname, line) in enumerate(lines):
        if line.strip() != "SAS Name":
            continue
        # The SAS name(s): the next non-blank line.
        j = i + 1
        while j < len(lines) and not lines[j][1].strip():
            j += 1
        names = [n.strip() for n in re.split(r"[,\s]+", lines[j][1].strip()) if n.strip()]
        # Back up to "Definition"; the title is the line before it.
        k = i
        while k > 0 and lines[k][1].strip() != "Definition":
            k -= 1
        title_line = next((lines[t][1].strip() for t in range(k - 1, max(k - 4, -1), -1) if lines[t][1].strip()), "")
        m = ELEMENT_ID.match(title_line)
        element_id, title = (m.group(1), m.group(2)) if m else (None, title_line)
        body = [l for _, l in lines[k + 1:i]]
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
                   "codes": parse_code_blocks(codes)}
        for name in names:
            key = (lines[i][0], name)
            # A discontinued element can share a SAS name with the current one; keep the one coded for 2024.
            if key in elements and current_codes(elements[key])[0] is not None:
                continue
            elements[key] = element
    return elements


ROW_CELL = re.compile(r"^(--|-?\d+(-\d+)?|x+)$")


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
        if len(tokens) > k and all(ROW_CELL.match(c) for c in tokens[:k]):
            blocks[-1]["rows"].append((tokens[:k], " ".join(tokens[k:])))
        elif blocks[-1]["rows"] and len(line) - len(line.lstrip()) >= 10:
            cells, label = blocks[-1]["rows"][-1]
            blocks[-1]["rows"][-1] = (cells, label + " " + line.strip())  # a wrapped label
        else:
            break  # the table has ended
    return blocks


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
# report didn't say, recorded here as dk with this note.
MISSING_LABELS = {
    "not reported": ["no_answer"],
    "reported as unknown": ["dk"],
    "unknown": ["dk"],
}


def missing_types(label):
    key = label.strip().lower()
    if key in MISSING_LABELS:
        return MISSING_LABELS[key]
    if key.startswith("unknown") or key.startswith("reported as unknown"):
        return ["dk"]
    return None


SCALE_SIGNATURES = {("No", "Yes"): "yes_no"}

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


def build_variable(name, table, spec, measure, element, counts, data_labels):
    report = []
    record = {"dataset": "fars", "release": spec["release"], "table": table, "name": name}
    if element is None:
        report.append(f"`{table}/{name}`: not found in the manual")
        element = {"title": "", "definition": "", "additional": "", "codes": [], "id": None}
    if element["title"]:
        record["label"] = element["title"]
    present = {as_number(v): n for v, n in counts.items() if v.strip() != "" and as_number(v) is not None}
    if present:
        record["storage_type"] = "integer" if all(isinstance(v, int) for v in present) else "float"
    if name in measure:
        record["measure"] = measure[name]
    notes = []
    if element["definition"]:
        notes.append("Definition: " + element["definition"])
    if element["additional"]:
        notes.append("Additional information: " + element["additional"])
    if notes:
        record["notes"] = "\n".join(notes)

    codes, header = current_codes(element) if element["codes"] else ([], "")
    values, documented, ranges, actual_values = [], {}, [], None
    if codes is None:
        report.append(f"`{table}/{name}`: {header}")
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
        types = missing_types(label)
        if types:
            values.append({"code": code, "label": label, "kind": "missing", "missing_types": types,
                           "origin": {"origin": "rule", "rule": "fars_missing_from_labels", "rule_version": RULE_VERSION,
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
                           "origin": {"origin": "data_file", "source": spec["tables"][table]["source"],
                                      "note": f"Occurs {n} times in the 2024 data; not in the manual's 2024 codes."}})
            report.append(f"`{table}/{name}` code {code}: occurs {n} times, not in the manual; marked unresolved")
    for code, label in documented.items():
        seen = data_labels.get(str(code), {})
        if seen:
            data_label = seen.most_common(1)[0][0]
            if comparable(data_label) == comparable(label):
                continue
            if comparable(data_label).startswith(comparable(label)) or comparable(label).startswith(comparable(data_label)):
                minor.append((table, name, code))  # one adds examples or a qualifier
            else:
                report.append(f"`{table}/{name}` code {code}: manual says {label!r}, data says {data_label!r}")
    if values:
        record["values"] = values
    if "valid_range" not in record and (actual_values or measure.get(name) in {"identifier", "count", "continuous"}) and present:
        valid = [v for v in present if v not in documented or not missing_types(documented[v])]
        if valid:
            record["valid_range"] = {"min": min(valid), "max": max(valid),
                                     **({"note": actual_values} if actual_values else {})}

    n_valid = sum(n for v, n in present.items()
                  if v not in documented or not missing_types(documented[v]))
    record["availability"] = [{"wave": int(spec["release"]), "n": n_valid}]
    record["source_location"] = {"source": MANUAL, **({"anchor": element["id"]} if element.get("id") else {})}

    hand = {"origin": "hand", "author": spec["author"], "date": spec["date"]}
    fields = {"notes": {"origin": "publisher", "source": MANUAL, "note": "The element's Definition and Additional Information."},
              "values": {"origin": "publisher", "source": MANUAL,
                         "note": f"The manual's codes for 2024 (column '{header}'); which are missing by rule (see each code)."},
              "availability": {"origin": "data_file", "source": spec["tables"][table]["source"],
                               "note": "n counts rows whose value isn't a missing code."}}
    if "storage_type" in record:
        fields["storage_type"] = {"origin": "rule", "rule": "storage_type_from_values", "rule_version": RULE_VERSION,
                                  "source": spec["tables"][table]["source"]}
    if "measure" in record:
        fields["measure"] = hand
    if "valid_range" in record and not ranges:
        fields["valid_range"] = {"origin": "data_file", "source": spec["tables"][table]["source"],
                                 "note": "Min and max of the values that occur."}
    record["provenance"] = {"record": {"origin": "publisher", "source": MANUAL}, "fields": fields}
    return record, report


def main():
    spec = yaml.safe_load((ROOT / "build" / "slice.yaml").read_text())
    measure = {n: m for m, ns in spec["measure"].items() for n in ns}
    elements = parse_elements(ROOT / "data" / "interim" / "manual.txt")
    release_dir = META / "releases" / spec["release"]
    all_records, notes, summaries = [], [], []
    for table, t in spec["tables"].items():
        header, values, labels = read_csv(RAW / t["data"], t["variables"])
        file_names = {name for (f, name) in elements if f in (t["file"], "KEY")}
        data_cols = {c for c in header if not c.endswith("NAME")}
        summaries.append(f"- `{table}`: {len(t['variables'])} variables in the slice; data file has {len(header)} columns "
                         f"({len(data_cols)} without the NAME columns); manual elements for {t['file']}: {len(file_names)}; "
                         f"data columns not in the manual: {sorted(data_cols - file_names)[:12] or 'none'}")
        records = []
        for name in t["variables"]:
            element = elements.get((t["file"], name)) or elements.get(("KEY", name))
            record, report = build_variable(name, table, spec, measure, element, values[name], labels[name])
            records.append(record)
            notes += report
        write_jsonl(release_dir / table / "variables.jsonl", records)
        all_records += records

    signatures = [{"target": {"variable": r["name"], "table": r["table"]}, "property": "scale_signature",
                   "value": SCALE_SIGNATURES[lab], "rule": "scale_signature_from_valid_labels", "rule_version": RULE_VERSION}
                  for r in all_records
                  if (lab := tuple(v["label"] for v in r.get("values", []) if v["kind"] == "valid")) in SCALE_SIGNATURES]
    write_jsonl(META / "derived" / "scale_signature.jsonl", signatures)
    types = collections.Counter(tp for r in all_records for v in r.get("values", []) if v["kind"] == "missing"
                                for tp in v["missing_types"])
    lines = [f"# Build report: FARS slice, release {spec['release']}", "", "Generated by `build/build_slice.py`. Do not edit.", "",
             f"- Manual elements parsed: {len({id(e) for e in elements.values()})}", *summaries,
             f"- Unresolved codes: {sum(v['kind'] == 'unresolved' for r in all_records for v in r.get('values', []))}",
             f"- Codes whose manual and data labels differ only in detail (case, punctuation, year notes, added examples): {len(minor)}",
             f"- Scale signatures: {len(signatures)}", "", "## Missing types assigned, by number of codes", "",
             "| Type | Codes |", "|---|---|", *[f"| `{tp}` | {k} |" for tp, k in sorted(types.items())], "",
             "## Values and labels to look at", "", *([f"- {n}" for n in notes] or ["None."]), ""]
    (ROOT / "build" / "report.md").write_text("\n".join(lines))
    print(f"{len(all_records)} variables in {len(spec['tables'])} tables; see build/report.md")


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
