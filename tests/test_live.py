"""执行中换型测试：版本锁定、实际阶段优先、首件失败 hold。"""
from changeover.live import assess_live
from tests._base import AppCase


class LiveTest(AppCase):
    def test_live_keeps_start_spec_when_new_version_effective(self):
        # LIVE 按 V1 开工，V2 在 21635 已生效
        fact = self.facts["LIVE"]
        est = self.app.estimates[("A", "B", "一厂", "V1")]
        a = assess_live(fact, est, self.app.library, 21645)
        self.assertEqual(a.spec_at_start, "V1")
        self.assertEqual(a.current_effective_spec, "V2")
        self.assertTrue(a.version_switch_pending)
        self.assertTrue(any("不回溯" in r for r in a.reasons))

    def test_live_rejects_new_spec_estimate(self):
        fact = self.facts["LIVE"]
        est_v2 = self.app.estimates[("A", "B", "一厂", "V2")]
        a = assess_live(fact, est_v2, self.app.library, 21645)
        # 传入新版本估计必须被拒绝，仍按实际阶段+开工版本推算
        self.assertTrue(any("拒绝" in r for r in a.reasons))
        self.assertEqual(a.spec_at_start, "V1")

    def test_completed_stages_use_actuals(self):
        fact = self.facts["LIVE"]
        est = self.app.estimates[("A", "B", "一厂", "V1")]
        a = assess_live(fact, est, self.app.library, 21645)
        done = {s["stage"]: s for s in a.completed_stages}
        # 清洗实际 14 分钟（21605→21619），不再出现在剩余分解里
        self.assertEqual(done["清洗"]["actual_min"], 14)
        self.assertNotIn("清洗", a.remaining_breakdown)
        # 重检仍在进行
        self.assertEqual(a.open_stage, "重检")
        self.assertGreaterEqual(a.remaining_minutes, 0)

    def test_first_article_failure_holds_line(self):
        fact = self.facts["LIVE"]
        est = self.app.estimates[("A", "B", "一厂", "V1")]
        a = assess_live(fact, est, self.app.library, 21645)
        self.assertTrue(a.first_article_failed)
        self.assertFalse(a.line_released)
        self.assertIsNotNone(a.hold_until_min)
        self.assertGreaterEqual(a.hold_until_min, 21645)

    def test_observed_line_minutes(self):
        fact = self.facts["LIVE"]
        est = self.app.estimates[("A", "B", "一厂", "V1")]
        a = assess_live(fact, est, self.app.library, 21645)
        self.assertEqual(a.observed_line_minutes, 21645 - 21605)

    def test_historical_censored_switch_treated_same_way(self):
        fact = self.facts["C0"]
        self.assertTrue(fact.reinspect_open)
        est = self.app.estimates[("A", "B", "一厂", "V1")]
        a = assess_live(fact, est, self.app.library, 60)
        self.assertTrue(a.first_article_failed)
        self.assertFalse(a.line_released)

    def test_open_stage_over_time_limit_flagged(self):
        # C0 重检始于 56；复核时刻 300 时已持续 244 分钟 > 180 时限
        fact = self.facts["C0"]
        est = self.app.estimates[("A", "B", "一厂", "V1")]
        a = assess_live(fact, est, self.app.library, 300,
                        missing_stage_time_limit_min=180)
        self.assertTrue(any("缺工时限" in r for r in a.reasons))
        open_row = next(s for s in a.completed_stages
                        if s["stage"] == "重检")
        self.assertTrue(open_row["over_time_limit"])
