"""历史复核与时间区间工具测试。"""
from changeover.reconciliation import reconcile, reconcile_shift
from changeover.timeutils import (distribution, merge, overlap,
                                  quantile, split_by_shifts, union_len)
from tests._base import AppCase


class ReconciliationTest(AppCase):
    def test_completed_switch_reconciles_actuals(self):
        # C4：首件失败 + 重检 11 分钟
        report = reconcile(self.facts["C4"])
        self.assertEqual(report.first_article_failed, True)
        self.assertEqual(report.reinspect_actual_min, 11)
        self.assertGreater(report.double_counted_min, 0)
        self.assertGreater(report.overlap_prep_actual_min, 0)
        self.assertTrue(any("重检" in a for a in report.attributions))

    def test_naive_sum_equals_union_plus_double_count(self):
        for sid in ("C1", "C4", "C8"):
            r = reconcile(self.facts[sid])
            self.assertEqual(
                r.naive_stage_sum_min,
                r.actual_manual_minutes + r.double_counted_min)

    def test_equipment_wait_not_in_manual(self):
        # C11 有设备等待
        c11 = self.facts["C11"]
        r = reconcile(c11)
        self.assertGreater(r.equipment_wait_actual_min, 0)
        # 人工口径不含设备等待
        self.assertNotIn("设备等待",
                         [s.stage for s in c11.stages if s.worker])

    def test_shift_window_filter(self):
        reports = reconcile_shift(
            self.facts, shift_window=(100, 700))
        ids = {r.switch_id for r in reports}
        self.assertIn("C1", ids)
        self.assertNotIn("LIVE", ids)  # 截尾不复核
        for r in reports:
            self.assertIsNotNone(r.actual_line_minutes)

    def test_reinspect_attributed_when_not_planned(self):
        r = reconcile(self.facts["C4"])
        stg = next(s for s in r.stage_deviations if s.stage == "重检")
        self.assertIn("未预留", stg.note)


class TimeUtilsTest(AppCase):
    def test_overlap_and_union(self):
        self.assertEqual(overlap((1, 5), (3, 7)), 2)
        self.assertEqual(overlap((1, 3), (3, 5)), 0)  # 右端不包含
        self.assertEqual(union_len([(1, 5), (3, 7), (10, 12)]), 8)
        self.assertEqual(merge([(1, 4), (4, 6)]), [(1, 6)])

    def test_quantile_matches_linear_interpolation(self):
        self.assertEqual(quantile([1, 2, 3, 4], 0.5), 2.5)
        self.assertEqual(quantile([10], 0.9), 10.0)
        # numpy 'linear' 口径：pos=0.9*7=6.3 → 44 + 0.3*(47-44)
        self.assertAlmostEqual(
            quantile([35, 36, 37, 38, 40, 42, 44, 47], 0.9), 44.9)
        self.assertAlmostEqual(
            quantile([35, 36, 37, 38, 40, 42, 44, 47], 0.5), 39.0)

    def test_distribution_min_sample_gate(self):
        small = distribution([1, 2])
        self.assertIsNone(small.median)
        self.assertIsNotNone(small.mean)
        full = distribution([1, 2, 3, 4, 5])
        self.assertEqual(full.median, 3)

    def test_split_by_shifts(self):
        boundaries = (100, 200, 300)
        parts = split_by_shifts((150, 250), boundaries)
        self.assertEqual([p[0] for p in parts], [0, 1])
        self.assertEqual(parts[0][1], (150, 200))
        self.assertEqual(parts[1][1], (200, 250))
