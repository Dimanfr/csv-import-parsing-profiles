"""Import shop / ERP CSV exports through a per-customer parsing profile.

Small businesses can often only export a CSV file, and every system writes it
differently: Windows-1252 or UTF-8, ";" or ",", "E-Mail" or "Email",
"31.12.2026" or "2026-12-31". A parsing profile (JSON) describes one
customer's export once; this module turns the file into clean records and a
precise error list, without guessing.

    profile = load_profile(Path("examples/profile_default_de.json"))
    result = import_csv(Path("examples/shop_export_cp1252.csv").read_bytes(), profile)
    result.records   # list of dicts with canonical field names
    result.errors    # [RowError(row=4, field="purchase_date", message=...)]

Standard library only. Row numbers are spreadsheet row numbers (header = 1).
"""

from __future__ import annotations

import codecs
import csv
import io
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

FIELD_TYPES = {"text", "email", "phone", "date"}


@dataclass(frozen=True)
class FieldSpec:
    name: str                  # canonical field name, e.g. "purchase_date"
    aliases: tuple[str, ...]   # header names this field may appear under
    type: str = "text"         # text | email | phone | date
    required: bool = False


@dataclass(frozen=True)
class Profile:
    name: str
    fields: tuple[FieldSpec, ...]
    encoding: str = "auto"                 # "auto" or an explicit codec name
    delimiter: str = "auto"                # "auto", ";", ",", "\t"
    date_formats: tuple[str, ...] = ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y")
    skip_empty_rows: bool = True
    dedup_fields: tuple[str, ...] = ()     # duplicates within one file are reported, not imported
    min_phone_digits: int = 7


@dataclass(frozen=True)
class RowError:
    row: int
    field: str | None
    message: str


@dataclass
class ImportResult:
    encoding: str
    delimiter: str
    records: list[dict] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    duplicates: list[int] = field(default_factory=list)
    skipped_empty: int = 0


class ProfileError(ValueError):
    """The profile is invalid or does not fit the file (e.g. a required column is missing)."""


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

def load_profile(path: Path) -> Profile:
    d = json.loads(path.read_text(encoding="utf-8"))
    try:
        fields = tuple(FieldSpec(f["name"], tuple(f["aliases"]), f.get("type", "text"), bool(f.get("required", False)))
                       for f in d["fields"])
        profile = Profile(
            name=d["name"], fields=fields,
            encoding=d.get("encoding", "auto"), delimiter=d.get("delimiter", "auto"),
            date_formats=tuple(d.get("date_formats", Profile.date_formats)),
            skip_empty_rows=bool(d.get("skip_empty_rows", True)),
            dedup_fields=tuple(d.get("dedup_fields", ())),
            min_phone_digits=int(d.get("min_phone_digits", 7)),
        )
    except (KeyError, TypeError) as e:
        raise ProfileError(f"profile {path}: {e}") from None
    validate_profile(profile)
    return profile


def validate_profile(p: Profile) -> None:
    names = [f.name for f in p.fields]
    if len(names) != len(set(names)):
        raise ProfileError("field names must be unique")
    seen = {}
    for f in p.fields:
        if f.type not in FIELD_TYPES:
            raise ProfileError(f"field {f.name}: unknown type {f.type!r}")
        if not f.aliases:
            raise ProfileError(f"field {f.name}: at least one alias is required")
        for a in f.aliases:
            key = _header_key(a)
            if key in seen and seen[key] != f.name:
                raise ProfileError(f"alias {a!r} is used by both {seen[key]} and {f.name}")
            seen[key] = f.name
    unknown = set(p.dedup_fields) - set(names)
    if unknown:
        raise ProfileError(f"dedup_fields not in profile: {sorted(unknown)}")
    if p.delimiter not in ("auto", ";", ",", "\t"):
        raise ProfileError(f"unsupported delimiter {p.delimiter!r}")


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

def decode(content: bytes, encoding: str = "auto") -> tuple[str, str]:
    """Return (text, encoding used). Never replaces characters silently.

    auto: UTF-16 with BOM -> UTF-8 (with or without BOM) -> Windows-1252.
    Windows-1252 leaves five byte values undefined (0x81 0x8D 0x8F 0x90 0x9D);
    a file containing them is rejected instead of being decoded wrongly.
    """
    if encoding != "auto":
        return content.decode(encoding), encoding
    if content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return content.decode("utf-16"), "utf-16"
    try:
        return content.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return content.decode("cp1252"), "cp1252"
    except UnicodeDecodeError as e:
        raise ProfileError(f"file is neither UTF-8 nor Windows-1252 (byte 0x{content[e.start]:02x} "
                           f"at offset {e.start}); set an explicit encoding in the profile") from None


def detect_delimiter(text: str, configured: str = "auto") -> str:
    if configured != "auto":
        return configured
    header = text.split("\n", 1)[0]
    # Count delimiters outside double quotes: "Customer, surname";Date is a ';' file.
    outside, quoted = [], False
    for ch in header:
        if ch == '"':
            quoted = not quoted
        elif not quoted:
            outside.append(ch)
    counts = {d: outside.count(d) for d in (";", ",", "\t")}
    best = max(counts, key=counts.get)
    if counts[best] == 0:
        raise ProfileError("cannot detect the delimiter: the header has no ';', ',' or TAB")
    return best


# ---------------------------------------------------------------------------
# Field parsing
# ---------------------------------------------------------------------------

_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _header_key(h: str) -> str:
    """Header matching ignores case, surrounding spaces, '-', '_' and spaces inside."""
    return re.sub(r"[\s_\-]+", "", h.strip().lower())


def parse_date(value: str, formats: tuple[str, ...]) -> date:
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"date {value!r} matches none of {list(formats)}")


def parse_value(spec: FieldSpec, raw: str, profile: Profile):
    if spec.type == "email":
        if not _EMAIL.match(raw):
            raise ValueError("invalid e-mail address")
        return raw.lower()
    if spec.type == "phone":
        digits = re.sub(r"\D", "", raw)
        if len(digits) < profile.min_phone_digits:
            raise ValueError(f"phone number has fewer than {profile.min_phone_digits} digits")
        return raw
    if spec.type == "date":
        return parse_date(raw, profile.date_formats)
    return raw


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def map_headers(headers: list[str], profile: Profile) -> dict[str, int]:
    """Canonical field -> column index. Raises if a required field has no column."""
    index = {}
    keys = [_header_key(h) for h in headers]
    for spec in profile.fields:
        wanted = {_header_key(a) for a in spec.aliases}
        hits = [i for i, k in enumerate(keys) if k in wanted]
        if len(hits) > 1:
            raise ProfileError(f"field {spec.name}: several columns match ({[headers[i] for i in hits]})")
        if hits:
            index[spec.name] = hits[0]
        elif spec.required:
            raise ProfileError(f"required field {spec.name} not found; expected one of {list(spec.aliases)}")
    return index


def import_csv(content: bytes, profile: Profile) -> ImportResult:
    text, used_encoding = decode(content, profile.encoding)
    delimiter = detect_delimiter(text, profile.delimiter)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        headers = next(reader)
    except StopIteration:
        raise ProfileError("file is empty") from None
    columns = map_headers(headers, profile)
    result = ImportResult(encoding=used_encoding, delimiter=delimiter)
    seen_keys: dict[tuple, int] = {}

    for row_no, row in enumerate(reader, start=2):
        cells = [c.strip() for c in row]
        if profile.skip_empty_rows and not any(cells):
            result.skipped_empty += 1
            continue
        if len(cells) > len(headers):
            result.errors.append(RowError(row_no, None, f"{len(cells)} values but {len(headers)} columns (shifted row?)"))
            continue
        record, row_errors = {}, []
        for spec in profile.fields:
            i = columns.get(spec.name)
            raw = cells[i] if i is not None and i < len(cells) else ""
            if raw == "":
                if spec.required:
                    row_errors.append(RowError(row_no, spec.name, "required value is empty"))
                record[spec.name] = None
                continue
            try:
                record[spec.name] = parse_value(spec, raw, profile)
            except ValueError as e:
                row_errors.append(RowError(row_no, spec.name, str(e)))
        if row_errors:
            result.errors.extend(row_errors)
            continue
        if profile.dedup_fields:
            key = tuple(str(record.get(f) or "").casefold() for f in profile.dedup_fields)
            if key in seen_keys:
                result.duplicates.append(row_no)
                continue
            seen_keys[key] = row_no
        record["_row"] = row_no
        result.records.append(record)
    return result


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 2:
        print("usage: python3 csv_import.py <profile.json> <export.csv>", file=sys.stderr)
        return 2
    profile = load_profile(Path(args[0]))
    try:
        r = import_csv(Path(args[1]).read_bytes(), profile)
    except ProfileError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    print(f"encoding={r.encoding} delimiter={r.delimiter!r} records={len(r.records)} "
          f"errors={len(r.errors)} duplicates={len(r.duplicates)} skipped_empty={r.skipped_empty}")
    for e in r.errors:
        print(f"  row {e.row}: {e.field or '-'}: {e.message}")
    for rec in r.records:
        print(json.dumps(rec, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
