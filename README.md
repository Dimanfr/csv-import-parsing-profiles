# csv-import-parsing-profiles

Imports CSV exports from shop or ERP systems through a **parsing profile**:
one JSON file per customer describing their export. Out come clean records
with canonical field names, plus an error list that names the spreadsheet row
and the field. Nothing is guessed.

Standard-library Python, one module.

## Why it was built

The source is a CRM reminder service with file-based intake (watchdog): a shop
drops its sales export into a folder, and the service plans follow-up
reminders. Small retailers rarely have an API, only an "Export to CSV" button,
and every export looks different:

- Windows-1252 from older systems, UTF-8 from newer ones, sometimes UTF-16;
- `;` (German Excel) or `,`;
- `Kunde`, `Kundenname` or `Customer Name`; `E-Mail` or `Email`;
- `31.12.2026`, `2026-12-31` or `31/12/2026`.

A profile captures this once per customer, so the intake can run unattended.
When something does not fit, the operator gets a message they can act on
("row 4, purchase_date: date '31.02.2026' matches none of …") instead of a
silently wrong record.

## How it works

1. **Decode.** With `"encoding": "auto"`: UTF-16 with BOM, then UTF-8 (with or
   without BOM), then Windows-1252. Windows-1252 leaves five byte values
   undefined; a file containing them is refused rather than decoded wrongly.
   An explicit codec in the profile wins.
2. **Delimiter.** `;`, `,` or TAB, counted in the header line outside double
   quotes, or set in the profile.
3. **Map headers.** Each canonical field lists its aliases. Matching ignores
   case, spaces, `-` and `_` (`E-Mail` = `e_mail` = `EMAIL`). If a required
   field has no column, or two columns match one field, the whole file is
   refused before any row is read.
4. **Parse rows** by field type:

   | type | rule |
   |---|---|
   | `text` | trimmed |
   | `email` | basic syntax check, lower-cased |
   | `phone` | at least `min_phone_digits` digits; the number is kept as written |
   | `date` | tried against `date_formats` in order; no format matches → error, never "today" |

   Rows with more values than columns ("shifted" rows) are rejected.
5. **Duplicates.** Rows whose `dedup_fields` repeat an earlier row in the same
   file (case-insensitive) are listed and not imported.

```
$ python3 csv_import.py examples/profile_default_de.json examples/shop_export_cp1252.csv
encoding=cp1252 delimiter=';' records=2 errors=4 duplicates=1 skipped_empty=1
  row 4: purchase_date: date '31.02.2026' matches none of ['%d.%m.%Y', '%Y-%m-%d', '%d/%m/%Y', '%d.%m.%Y %H:%M']
  row 6: email: invalid e-mail address
  row 8: product: required value is empty
  row 9: -: 7 values but 6 columns (shifted row?)
{"name": "Erika Mustermann", "email": "erika@example.com", "phone": "030 23125 301", ...}
{"name": "Max Mustermann", "email": "max@example.com", "phone": "089 99998 302", ...}
```

The CLI exits with code 1 when a file is refused (e.g. a required column is
missing) and 2 on wrong usage.

## Run it

```sh
python3 csv_import.py examples/profile_default_de.json examples/shop_export_utf8_comma.csv
python3 -m unittest discover -s tests -t . -v
```

The examples are synthetic. Names are German placeholder names, e-mail
addresses use `example.com`, and phone numbers come from the Bundesnetzagentur
ranges for media use ("Drama Numbers").

## Status

| | |
|---|---|
| **Original use** | Intake step of a CRM reminder service with file-based intake (watchdog), a local prototype from 2025–2026. Parsing profiles were stored per tenant in PostgreSQL and applied with pandas, for CSV and XLSX. Tested locally with mock providers; never deployed, no real customer data. |
| **This public example** | A standalone rewrite of the profile logic for CSV, with 16 tests (11/2026). Compared with the original: German `;` files are detected (the original assumed `,`); an undecodable file is refused (the original's Windows-1252 fallback could fail on five byte values); a missing or unparseable date is an error (the original used today's date for a missing one and fell back to pandas date guessing); header matching tolerates case and separators; a missing required column refuses the whole file up front. XLSX and the database are not included. |

## Authorship

I define requirements, coordinate AI coding agents, test behaviour and operate the resulting systems; the agents implement the code.

The first version of the reminder service was written by me with Cursor. This
public rewrite was implemented by AI coding agents to my specification.

## Limitations

- CSV only. XLSX needs a separate reader (for example `openpyxl`) in front of
  the same mapping step.
- The e-mail and phone checks are plausibility checks, not verification.
- Date formats are tried in the profile's order. An ambiguous value such as
  `04/11/2026` is read with the first format that fits (day first in the
  example profile).
- Duplicates are detected within one file only, not against earlier imports.

## Licence

MIT, see `LICENSE`.
