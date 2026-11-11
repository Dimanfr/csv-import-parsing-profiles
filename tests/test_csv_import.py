import contextlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import csv_import as ci

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ci.load_profile(ROOT / "examples" / "profile_default_de.json")


def run(text: str, encoding="utf-8", profile=PROFILE):
    return ci.import_csv(text.encode(encoding), profile)


class DecodingTest(unittest.TestCase):
    def test_cp1252_example_file(self):
        r = ci.import_csv((ROOT / "examples" / "shop_export_cp1252.csv").read_bytes(), PROFILE)
        self.assertEqual((r.encoding, r.delimiter), ("cp1252", ";"))
        self.assertEqual(r.records[1]["product"], "Kompressionsstrümpfe")

    def test_utf8_bom_and_utf16(self):
        text = "Name;Artikel;Kaufdatum\nMax Mustermann;Einlagen;01.11.2026\n"
        self.assertEqual(ci.import_csv(b"\xef\xbb\xbf" + text.encode("utf-8"), PROFILE).encoding, "utf-8")
        r16 = ci.import_csv(text.encode("utf-16"), PROFILE)
        self.assertEqual((r16.encoding, len(r16.records)), ("utf-16", 1))

    def test_undecodable_bytes_are_refused_not_guessed(self):
        bad = "Name;Artikel;Kaufdatum\nMax;A;01.11.2026\n".encode("cp1252") + b"\x81\xff\n"
        with self.assertRaises(ci.ProfileError):
            ci.import_csv(bad, PROFILE)

    def test_explicit_encoding_wins(self):
        p = ci.Profile(**{**PROFILE.__dict__, "encoding": "latin-1"})
        r = ci.import_csv("Name;Artikel;Kaufdatum\nGröße;A;01.11.2026\n".encode("latin-1"), p)
        self.assertEqual((r.encoding, r.records[0]["name"]), ("latin-1", "Größe"))


class HeaderTest(unittest.TestCase):
    def test_aliases_ignore_case_spaces_and_dashes(self):
        r = run("KUNDE,e_mail,ARTIKEL,kauf datum\nMax Mustermann,max@example.com,Einlagen,01.11.2026\n")
        self.assertEqual(r.records[0]["email"], "max@example.com")

    def test_missing_required_column_is_refused_up_front(self):
        with self.assertRaises(ci.ProfileError) as cm:
            run("Name;Artikel\nMax;Einlagen\n")
        self.assertIn("purchase_date", str(cm.exception))

    def test_two_columns_for_one_field_is_refused(self):
        with self.assertRaises(ci.ProfileError):
            run("Name;Kunde;Artikel;Kaufdatum\nA;B;C;01.11.2026\n")

    def test_delimiters_inside_quoted_headers_are_ignored(self):
        self.assertEqual(ci.detect_delimiter('"Customer, surname, first name";Date\nA;B\n'), ";")
        self.assertEqual(ci.detect_delimiter('"a;b",c,d\n'), ",")
        r = run('"Name";"Artikel, Variante";Kaufdatum;Artikel\nMax;rot, Gr. 42;01.11.2026;Einlagen\n')
        self.assertEqual((r.delimiter, r.records[0]["product"]), (";", "Einlagen"))

    def test_delimiter_detection(self):
        self.assertEqual(run("Name\tArtikel\tKaufdatum\nMax\tA\t01.11.2026\n").delimiter, "\t")
        with self.assertRaises(ci.ProfileError):
            run("Name\nMax\n")


class RowTest(unittest.TestCase):
    def test_example_file_errors_are_precise(self):
        r = ci.import_csv((ROOT / "examples" / "shop_export_cp1252.csv").read_bytes(), PROFILE)
        self.assertEqual([rec["_row"] for rec in r.records], [2, 3])
        self.assertEqual([(e.row, e.field) for e in r.errors],
                         [(4, "purchase_date"), (6, "email"), (8, "product"), (9, None)])
        self.assertEqual(r.duplicates, [7])        # same as row 2 apart from upper-case e-mail
        self.assertEqual(r.skipped_empty, 1)

    def test_invalid_date_is_an_error_not_today(self):
        r = run("Name;Artikel;Kaufdatum\nMax;A;2026-13-01\n")
        self.assertEqual(r.records, [])
        self.assertEqual(r.errors[0].field, "purchase_date")

    def test_date_formats_in_profile_order(self):
        r = run("Name;Artikel;Kaufdatum\nA;X;04/11/2026\nB;Y;31.12.2026 09:30\n")
        self.assertEqual([x["purchase_date"] for x in r.records], [date(2026, 11, 4), date(2026, 12, 31)])

    def test_phone_needs_minimum_digits(self):
        r = run("Name;Telefon;Artikel;Kaufdatum\nMax;12-34;A;01.11.2026\n")
        self.assertEqual(r.errors[0].field, "phone")

    def test_quoted_fields_with_delimiters_and_short_rows(self):
        r = ci.import_csv((ROOT / "examples" / "shop_export_utf8_comma.csv").read_bytes(), PROFILE)
        self.assertEqual(r.records[1]["product"], "Compression stockings, size 2")
        self.assertIsNone(r.records[1]["phone"])
        short = run("Name;Telefon;Artikel;Kaufdatum\nMax;030 23125 310;A;01.11.2026\nShort;x\n")
        self.assertEqual(short.errors[0].row, 3)


class ProfileTest(unittest.TestCase):
    def write(self, d, **change):
        data = json.loads((ROOT / "examples" / "profile_default_de.json").read_text(encoding="utf-8"))
        data.update(change)
        p = Path(d) / "p.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_invalid_profiles_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            for change in (dict(delimiter="|"), dict(dedup_fields=["nope"]),
                           dict(fields=[{"name": "a", "aliases": ["X"]}, {"name": "b", "aliases": ["x"]}]),
                           dict(fields=[{"name": "a", "aliases": ["X"], "type": "money"}])):
                with self.subTest(change=change):
                    with self.assertRaises(ci.ProfileError):
                        ci.load_profile(self.write(d, **change))


class CliTest(unittest.TestCase):
    def test_cli_exit_codes(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(ci.main([str(ROOT / "examples" / "profile_default_de.json"),
                                      str(ROOT / "examples" / "shop_export_cp1252.csv")]), 0)
            self.assertEqual(ci.main([]), 2)
            with tempfile.TemporaryDirectory() as d:
                bad = Path(d) / "bad.csv"
                bad.write_text("Name;Artikel\nMax;A\n", encoding="utf-8")
                self.assertEqual(ci.main([str(ROOT / "examples" / "profile_default_de.json"), str(bad)]), 1)
        self.assertIn("records=2 errors=4 duplicates=1", out.getvalue())
        self.assertIn("refused: required field purchase_date", err.getvalue())


if __name__ == "__main__":
    unittest.main()
