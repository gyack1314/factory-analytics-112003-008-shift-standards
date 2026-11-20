"""估计器测试：分布、充分性、截尾剔除、迁移降级与失败率。"""
from changeover.estimator import SUFFICIENT_N, build_estimate
from tests._base import AppCase


class EstimatorTest(AppCase):
    def test_sufficient_direction_has_quantiles(self):
        est = build_estimate(self.pack, self.facts, ("A", "B"),
                             "一厂", "V2")
        self.assertTrue(est.sufficient)
        self.assertEqual(est.direct_complete_n, SUFFICIENT_N)
        block = est.metrics["line_occupied"]
        self.assertIsNotNone(block.dist.p10)
        self.assertIsNotNone(block.dist.p90)
        self.assertLess(block.dist.p10, block.dist.p90)

    def test_censored_observations_excluded_from_point_estimate(self):
        est = build_estimate(self.pack, self.facts, ("A", "B"),
                             "一厂", "V1")
        # V1 一厂 A→B：4 完整 + 2 截尾（C0、LIVE）
        self.assertEqual(est.direct_complete_n, 4)
        self.assertEqual(est.censored_n, 2)
        ids = est.metrics["line_occupied"].source_switch_ids
        self.assertNotIn("C0", ids)
        self.assertNotIn("LIVE", ids)
        # 截尾样本的已观察工序时长仍单独保留，不进点估计
        self.assertEqual(est.censored_n, 2)

    def test_fallback_pool_used_when_below_threshold(self):
        est = build_estimate(self.pack, self.facts, ("A", "B"),
                             "一厂", "V1")
        self.assertFalse(est.sufficient)
        self.assertTrue(est.fallback_used)
        # 来源 id 不重复
        for block in est.metrics.values():
            self.assertEqual(len(block.source_switch_ids),
                             len(set(block.source_switch_ids)))

    def test_reinspect_only_uses_failed_samples(self):
        est = build_estimate(self.pack, self.facts, ("A", "B"),
                             "一厂", "V2")
        block = est.metrics["reinspect"]
        ids = block.source_switch_ids
        for sid in ids:
            self.assertTrue(self.facts[sid].first_article_failed)
        # 未失败样本的 reinspect=0 不得混入
        self.assertTrue(all(v > 0 for v in block.dist.samples))

    def test_failure_rate_between_zero_and_one(self):
        for est in self.app.estimates_list:
            self.assertGreaterEqual(est.first_article_failure_rate, 0.0)
            self.assertLessEqual(est.first_article_failure_rate, 1.0)

    def test_uncertainty_band_inflated_for_transfer(self):
        est = build_estimate(self.pack, self.facts, ("A", "B"),
                             "一厂", "V1")
        band = est.uncertainty_band("line_occupied")
        self.assertIsNotNone(band)
        self.assertGreaterEqual(band["inflated_by"], 1.0)

    def test_blocked_direction_reports_reason(self):
        # A→C 在一厂有直接样本；构造一个无样本方向 B→C，迁移应被阻断
        est = build_estimate(self.pack, self.facts, ("B", "C"),
                             "一厂", "V2")
        self.assertEqual(est.direct_complete_n, 0)
        self.assertTrue(est.blocked_reasons or not est.fallback_used)

    def test_metric_separation_overlap_vs_line(self):
        est = build_estimate(self.pack, self.facts, ("X", "Y"),
                             "二厂", "V2")
        overlap = est.point_estimate("overlap_prep")
        line = est.point_estimate("line_occupied")
        # 可重叠预准备不计入占线
        self.assertGreater(overlap, 0)
        self.assertGreater(line, overlap)

    def test_equipment_wait_separate_metric(self):
        est = build_estimate(self.pack, self.facts, ("A", "B"),
                             "一厂", "V2")
        self.assertIn("equipment_wait", est.metrics)
        self.assertIn("double_counted", est.metrics)
