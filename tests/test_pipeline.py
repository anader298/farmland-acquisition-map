"""Unit tests. Run: python -m unittest discover tests"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from pipeline import farm_rule as FR
from pipeline import llc_registry as LR
from pipeline import resolve_owners as RO
from pipeline import valuation as V
from pipeline.owner_type import owner_type


class OwnerType(unittest.TestCase):
    def test_types(self):
        cases = {
            "QUILLBY JOHN A": "person",
            "QUILLBY JOHN A REVOCABLE TRUST": "trust",
            "FIRST EXAMPLE BANK TR #1042": "trust",
            "EXAMPLE PRAIRIE FARMS LLC": "entity",
            "QUILLBY JOHN A ESTATE": "estate",
            "QUILLBY HEIRS": "estate",
            "EXAMPLE TOWNSHIP": "government",
            "EXAMPLE REAL ESTATE LLC": "entity",       # REAL ESTATE is a trade word
            "EXAMPLE EST LLC": "entity",               # a registered suffix beats EST
            "QUILLBY MARY LIFE ESTATE": "person",      # a life tenant is alive
            "LODGE HENRY": "person",                   # LODGE as a surname
            "SMITH JOHN C/O EXAMPLE AG MGMT": "person",  # care-of tail is not the owner
        }
        for name, want in cases.items():
            with self.subTest(name=name):
                self.assertEqual(owner_type(name), want)


class FarmRule(unittest.TestCase):
    def test_selftest(self):
        self.assertEqual(FR._selftest(), 0)

    def test_text_class_from_manifest(self):
        s = FR.signals({"tax_class": "Farmland"}, {"class_field": "tax_class", "farm_class_values": ["FARMLAND"]})
        self.assertTrue(s["farm_class"])


class Registry(unittest.TestCase):
    def test_join(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "llcallnam.txt").write_text(
                "RUN DATE\n"
                f"{'00000001'}{'EXAMPLE OAK FARMS LLC':<60}\n"
                f"{'00000002'}{'EXAMPLE TWIN LLC':<60}\n"
                f"{'00000003'}{'EXAMPLE TWIN LLC':<60}\n")
            # right-trimmed lines, as published: padding must restore the fields
            (d / "llcallmgr.txt").write_text(
                "RUN DATE\n" + f"00000001{'QUILLBY JOHN A':<60}{'1 SAMPLE AVE':<45}{'ANYTOWN':<30}IA00007".rstrip()
                + "\n" + f"00000002{'FENWORTH MARY':<60}{'2 EXAMPLE RD':<45}{'SAMPLETON':<30}IL00001\n")
            parcels = [{"owner": "EXAMPLE OAK FARMS L.L.C.", "owner_type": "entity", "gis_acres": 80, "county": "A"},
                       {"owner": "EXAMPLE TWIN LLC", "owner_type": "entity", "gis_acres": 40, "county": "A"}]
            rows, st = LR.join_registry(LR.farmland_llcs(parcels), d)
            by = {r["llc_name"]: r for r in rows}
            oak = by["EXAMPLE OAK FARMS L.L.C."]
            self.assertEqual(oak["people"], "QUILLBY JOHN A")
            self.assertEqual(oak["absentee"], 1)
            self.assertIn("ANYTOWN IA 00007", oak["addresses"])
            twin = by["EXAMPLE TWIN LLC"]
            self.assertEqual((twin["sos_ambiguous"], twin["people"]), (1, ""))   # two file numbers: no one


class Resolve(unittest.TestCase):
    def o(self, name, county="A", mail=None, files=()):
        return {"name": name, "counties": {county}, "mails": {mail} if mail else set(), "files": set(files)}

    def test_merges_and_refusals(self):
        owners = [
            self.o("QUILLBY JOHN A", mail="1 EXAMPLE RD|00001"),
            self.o("JOHN A QUILLBY", mail="1 EXAMPLE RD|00001"),       # word order
            self.o("QUILBY JOHN A", mail="1 EXAMPLE RD|00001"),        # typo
            self.o("QUILLBY JOHN A & MARY B", mail="1 EXAMPLE RD|00001"),  # two holders
            self.o("EXAMPLE OAK FARMS LLC"), self.o("EXAMPLE OAK FARMS L.L.C."),
            self.o("EXAMPLE OAK FARMS LLC C/O SAMPLE FARM MGMT"),
            self.o("EXAMPLE OAK FARM TRUST"),                          # company vs trust
            self.o("FIRST EXAMPLE BANK TR #1042"), self.o("FIRST EXAMPLE BANK TR #1043"),
        ]
        oid, grp, _ = RO.resolve(owners)
        same = lambda a, b: oid[a] == oid[b]  # noqa: E731
        self.assertTrue(same("QUILLBY JOHN A", "JOHN A QUILLBY"))
        self.assertTrue(same("QUILLBY JOHN A", "QUILBY JOHN A"))
        self.assertFalse(same("QUILLBY JOHN A", "QUILLBY JOHN A & MARY B"))
        self.assertEqual(grp["QUILLBY JOHN A"], grp["QUILLBY JOHN A & MARY B"])   # same family group
        self.assertTrue(same("EXAMPLE OAK FARMS LLC", "EXAMPLE OAK FARMS L.L.C."))
        self.assertTrue(same("EXAMPLE OAK FARMS LLC", "EXAMPLE OAK FARMS LLC C/O SAMPLE FARM MGMT"))
        self.assertFalse(same("EXAMPLE OAK FARMS LLC", "EXAMPLE OAK FARM TRUST"))
        self.assertFalse(same("FIRST EXAMPLE BANK TR #1042", "FIRST EXAMPLE BANK TR #1043"))

    def test_token_guard(self):
        self.assertTrue(RO.token_guard("QUILLBY JOHN A", "QUILLBY JOHN"))
        self.assertFalse(RO.token_guard("QUILLBY JOHN", "QUILLBY JOHN JR"))
        self.assertFalse(RO.token_guard("QUILLBY DOUGLAS J", "QUILLBY DOUGLAS & JULIE"))


class Valuation(unittest.TestCase):
    def test_agree_and_thin(self):
        parcels, sales = {}, []
        for i in range(30):
            key = ("A", f"p{i}")
            parcels[key] = {"pi": 130, "xy": (-89.0 + i * 0.01, 38.3), "rent": 260}
            sales.append({"key": key, "date": date(2025, 6, 1), "price": 8000 * 80, "acres": 80, "flags": set()})
        parcels[("A", "t")] = {"pi": 130, "xy": (-88.9, 38.3), "rent": 260}
        v = V.Valuer(sales, parcels, {"A": set()}, date(2026, 11, 1))
        self.assertAlmostEqual(v.cap, 260 / 8000)
        out = v.value(("A", "t"), 130, 80, (-88.9, 38.3), 260)
        self.assertEqual(out["status"], "agree")
        self.assertAlmostEqual(out["comps"], 8000)
        self.assertEqual(v.value(("B", "t"), 130, 80, (-88.9, 38.3), 260)["status"], "thin")

    def test_rent_clamp(self):
        self.assertEqual(V.rent_estimate(200, 250, 120), 350)   # +40% cap
        self.assertAlmostEqual(V.rent_estimate(125, 250, 120), 250 + 3.21 * 5)

    def test_wmedian(self):
        self.assertEqual(V.wmedian([1, 2, 3], [1, 1, 5]), 3)


if __name__ == "__main__":
    unittest.main()
