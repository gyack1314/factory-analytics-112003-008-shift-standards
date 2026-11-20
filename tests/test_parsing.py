"""换型时间线重建测试：重叠、重复计入、截尾、设备等待。"""
from tests._base import AppCase


class ParsingTest(AppCase):
    def test_overlap_prep_is_before_stop(self):
        # 全部 181 条预准备都有 17 分钟落在停线前（LIVE/C0 等截尾同样可算）
        for fact in self.facts.values():
            self.assertGreaterEqual(fact.overlap_prep_min, 0)
        c1 = self.facts["C1"]
        self.assertEqual(c1.overlap_prep_min, 17)   # 123→140
        self.assertEqual(c1.post_stop_prep_min, 6)  # 140→146

    def test_line_occupied_uses_restart_minus_stop(self):
        c1 = self.facts["C1"]
        sw = self.pack.switch_index["C1"]
        self.assertEqual(c1.line_occupied_min,
                         sw.restart_min - sw.stop_min)
        self.assertEqual(c1.line_occupied_min, 37)

    def test_double_counting_is_same_worker_parallelism(self):
        # C1: 预准备 123-146 与清洗 141-162 同技师 E0，并行 5 分钟
        c1 = self.facts["C1"]
        self.assertEqual(c1.double_counted_min, 5)
        # 并集人工 = 工序和 − 重复
        self.assertEqual(c1.manual_min,
                         c1.sum_stage_min - c1.double_counted_min)

    def test_equipment_wait_excluded_from_manual(self):
        c0 = self.facts["C0"]
        self.assertEqual(c0.equipment_wait_min, 7)
        # 设备等待无技师，绝不进人工口径
        worker_stages = [s for s in c0.stages
                         if s.stage == "设备等待"]
        self.assertTrue(all(s.worker is None for s in worker_stages))
        manual_without_wait = c0.manual_min
        self.assertNotIn("设备等待",
                         [s.stage for s in c0.stages if s.worker])

    def test_censored_switch_has_no_line_estimate(self):
        for sid in ("C0", "C52", "LIVE"):
            fact = self.facts[sid]
            self.assertTrue(fact.censored)
            self.assertIsNone(fact.line_occupied_min)
            self.assertIsNotNone(fact.open_stage)

    def test_completed_switch_not_censored(self):
        c1 = self.facts["C1"]
        self.assertFalse(c1.censored)
        self.assertIsNone(c1.open_stage)

    def test_first_article_failure_flag(self):
        self.assertTrue(self.facts["C4"].first_article_failed)
        self.assertTrue(self.facts["LIVE"].first_article_failed)
        self.assertFalse(self.facts["C1"].first_article_failed)

    def test_tool_log_dedup(self):
        # 23 条精确重复转抄，去重后区间不重复
        total_dup = sum(u.duplicate_rows
                        for f in self.facts.values()
                        for u in f.tool_uses)
        self.assertEqual(total_dup, 23)
        for f in self.facts.values():
            for u in f.tool_uses:
                # 并集区间互不重叠
                ivs = sorted(u.intervals)
                for a, b in zip(ivs, ivs[1:]):
                    self.assertGreaterEqual(b[0], a[1])

    def test_live_tool_use_preserved(self):
        live = self.facts["LIVE"]
        self.assertEqual(len(live.tool_uses), 1)
        self.assertEqual(live.tool_uses[0].tool, "M0")
        self.assertEqual(live.tool_uses[0].intervals, [(21642, 21668)])

    def test_adjacent_intervals_not_counted_as_duplicates(self):
        from changeover.models import ToolLog
        from changeover.parsing import _dedup_tool_logs
        logs = [
            ToolLog("M0", "E0", "L0", 10, 30, "X"),
            ToolLog("M0", "E0", "L0", 10, 30, "X"),   # 精确重复
            ToolLog("M0", "E0", "L0", 30, 40, "X"),   # 相邻，非重复
        ]
        uses = _dedup_tool_logs(logs)
        self.assertEqual(uses[0].intervals, [(10, 40)])
        self.assertEqual(uses[0].duplicate_rows, 1)
        self.assertEqual(uses[0].raw_rows, 3)
