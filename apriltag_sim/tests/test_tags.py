"""Tests for the AprilTag mapping rules."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tags import TAG_MAX_ID, build_entries, find_by_tag, parse_cabinet_number, summary  # noqa: E402


class Cabinet:
    def __init__(self, number, cabinet_id=1, name="零件库"):
        self.cabinet_number = number
        self.cabinet_id = cabinet_id
        self.equipment_name = name
        self.description = ""
        self.components = []


class TagMappingTests(unittest.TestCase):
    def test_numeric_cabinet_maps_to_tag(self):
        entry = parse_cabinet_number(Cabinet("11"))
        self.assertTrue(entry.valid)
        self.assertEqual(entry.tag_id, 11)
        self.assertTrue(entry.selectable)

    def test_leading_zero_is_parsed_but_noted(self):
        entry = parse_cabinet_number(Cabinet("007"))
        self.assertTrue(entry.valid)
        self.assertEqual(entry.tag_id, 7)
        self.assertIn("7", entry.reason)

    def test_non_numeric_and_negative_are_invalid(self):
        for number in ("A01", "-3", "", "  "):
            with self.subTest(number=number):
                entry = parse_cabinet_number(Cabinet(number))
                self.assertFalse(entry.valid)
                self.assertFalse(entry.selectable)

    def test_out_of_range_ids_are_invalid(self):
        entry = parse_cabinet_number(Cabinet(str(TAG_MAX_ID + 1)))
        self.assertFalse(entry.valid)
        self.assertIn("范围", entry.reason)

    def test_duplicate_after_normalisation_is_ambiguous(self):
        entries = build_entries([Cabinet("1", cabinet_id=1), Cabinet("01", cabinet_id=2)])
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(entry.ambiguous for entry in entries))
        self.assertFalse(any(entry.selectable for entry in entries))
        self.assertEqual(len(find_by_tag(entries, 1)), 2)

    def test_valid_entries_sort_before_invalid(self):
        entries = build_entries([Cabinet("A"), Cabinet("20"), Cabinet("5")])
        self.assertEqual([entry.tag_id for entry in entries[:2]], [5, 20])
        self.assertFalse(entries[-1].valid)

    def test_summary_counts(self):
        entries = build_entries([Cabinet("1", 1), Cabinet("01", 2), Cabinet("X", 3)])
        counts = summary(entries)
        self.assertEqual(counts["total"], 3)
        self.assertEqual(counts["valid"], 0)
        self.assertEqual(counts["ambiguous"], 2)
        self.assertEqual(counts["invalid"], 1)


if __name__ == "__main__":
    unittest.main()
