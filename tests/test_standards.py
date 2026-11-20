"""标准台账测试：审批、缺口、拒用、生效时点、改版追溯。"""
from changeover.estimator import build_estimate
from changeover.standards import ACTION_APPROVE, StandardsLibrary
from tests._base import AppCase


class StandardsTest(AppCase):
    def setUp(self):
        import json
        from pathlib import Path
        rows = json.loads((Path(__file__).resolve().parent.parent
                           / "data" / "standards_ledger.json").read_text(
            encoding="utf-8"))
        self.lib = StandardsLibrary.from_input_ledger(rows)

    def test_input_ledger_imported(self):
        self.assertEqual(len(self.lib.versions), 3)
        self.assertTrue(self.lib.is_approved(("A", "B"), "一厂", "V2"))

    def test_active_version_respects_effective_time(self):
        # 21635 之前 V1 生效，之后 V2 生效
        self.assertEqual(
            self.lib.active_version(("A", "B"), "一厂", 21600).spec, "V1")
        self.assertEqual(
            self.lib.active_version(("A", "B"), "一厂", 21635).spec, "V2")

    def test_returned_standard_not_usable(self):
        # STD3：A→C V2 退回补采
        ok, reasons = self.lib.usable_for_scheduling(
            ("A", "C"), "一厂", "V2", 22000)
        self.assertFalse(ok)
        self.assertTrue(any("未获" in r for r in reasons))

    def test_approve_records_evidence_warning(self):
        est = build_estimate(self.pack, self.facts, ("A", "B"),
                             "一厂", "V1")  # 样本不足
        v = self.lib.approve(("A", "B"), "一厂", "V3", 23000,
                             "工艺负责人", est, "试运行")
        self.assertEqual(v.action, ACTION_APPROVE)
        self.assertIn("证据不足", v.note)
        self.assertTrue(self.lib.is_approved(("A", "B"), "一厂", "V3"))

    def test_gap_request_persisted_and_listed(self):
        gap = self.lib.request_gap(
            ("B", "C"), "一厂", "V2", "洁净方向阻断", 21700, "工艺负责人")
        self.assertEqual(gap.status, "开放")
        self.assertEqual(len(self.lib.gaps), 1)

    def test_rejection_blocks_scheduling_and_resolvable(self):
        self.lib.raise_rejection(
            ("A", "B"), "一厂", "V2", "重检样本不足", 21700, "工艺负责人")
        ok, reasons = self.lib.usable_for_scheduling(
            ("A", "B"), "一厂", "V2", 21800)
        self.assertFalse(ok)
        self.assertIn("重检样本不足", reasons)
        rid = self.lib.rejections[0].id
        self.lib.resolve_rejection(rid)
        ok2, _ = self.lib.usable_for_scheduling(
            ("A", "B"), "一厂", "V2", 21800)
        self.assertTrue(ok2)

    def test_revision_chain_explains_change(self):
        chain = self.lib.revision_reasons(("A", "B"), "一厂")
        specs = [c["spec"] for c in chain]
        self.assertEqual(specs, ["V1", "V2"])
        self.assertEqual(chain[1]["action"], ACTION_APPROVE)

    def test_save_and_load_roundtrip(self, tmp_path=None):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            self.lib.request_gap(
                ("X", "Y"), "二厂", "V2", "测试缺口", 100, "张")
            self.lib.save(path)
            loaded = StandardsLibrary.load(path)
            self.assertEqual(len(loaded.gaps), 1)
            self.assertEqual(len(loaded.versions), 3)
