import json
import tempfile
import unittest
from pathlib import Path

from bot.state import State


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nested" / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        state = State()
        state.mark("A", 1000)
        state.mark("B", 2000)
        state.last_run_ts = 2000
        state.runs = 3
        state.save(self.path)

        loaded = State.load(self.path)
        self.assertTrue(loaded.seen("A"))
        self.assertTrue(loaded.seen("B"))
        self.assertFalse(loaded.seen("C"))
        self.assertEqual(loaded.last_run_ts, 2000)
        self.assertEqual(loaded.runs, 3)

    def test_missing_file_starts_empty(self):
        self.assertEqual(State.load(self.path).processed, {})

    def test_corrupt_file_starts_empty_instead_of_crashing(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(State.load(self.path).processed, {})

    def test_legacy_list_layout_is_tolerated(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"processed": ["X", "Y"]}), encoding="utf-8")
        loaded = State.load(self.path)
        self.assertTrue(loaded.seen("X"))
        self.assertTrue(loaded.seen("Y"))

    def test_save_is_atomic_and_leaves_no_temp_file(self):
        state = State()
        state.mark("A", 1)
        state.save(self.path)
        siblings = list(self.path.parent.iterdir())
        self.assertEqual([p.name for p in siblings], ["state.json"])

    def test_prune_drops_entries_past_retention(self):
        state = State()
        now = 10_000_000
        state.mark("old", now - 20 * 86400)
        state.mark("new", now - 3600)
        removed = state.prune(retention_days=14, max_ids=1000, now=now)
        self.assertEqual(removed, 1)
        self.assertFalse(state.seen("old"))
        self.assertTrue(state.seen("new"))

    def test_prune_caps_by_count_keeping_newest(self):
        state = State()
        now = 10_000_000
        for index in range(10):
            state.mark(f"id{index}", now - index)
        removed = state.prune(retention_days=365, max_ids=3, now=now)
        self.assertEqual(removed, 7)
        self.assertEqual(sorted(state.processed), ["id0", "id1", "id2"])

    def test_merge_is_a_union_keeping_latest_timestamps(self):
        mine = State(processed={"A": 5, "B": 1}, last_run_ts=50, runs=2)
        theirs = State(processed={"B": 9, "C": 3}, last_run_ts=70, runs=5)
        merged = mine.merge(theirs)
        self.assertEqual(merged.processed, {"A": 5, "B": 9, "C": 3})
        self.assertEqual(merged.last_run_ts, 70)
        self.assertEqual(merged.runs, 5)

    def test_serialised_form_is_stable_for_clean_diffs(self):
        state = State()
        state.mark("B", 2)
        state.mark("A", 1)
        state.save(self.path)
        first = self.path.read_text(encoding="utf-8")
        State.load(self.path).save(self.path)
        self.assertEqual(first, self.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
