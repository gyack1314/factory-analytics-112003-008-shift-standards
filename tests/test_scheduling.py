"""排产与资源日历测试：占线/前移拆分、工具争抢、hold、方案对比。"""
from changeover.resources import ResourceCalendar
from tests._base import AppCase

REVIEW = 21645


class SchedulingTest(AppCase):
    def setUp(self):
        self.scheduler = self.app.scheduler(REVIEW)
        # 注入 PLAN1 工具预占用
        for log in self.pack.tool_logs:
            if log.switch.startswith("PLAN") and log.end_min > REVIEW:
                self.scheduler.calendar.add(
                    log.tool, "tool",
                    (max(log.start_min, REVIEW), log.end_min),
                    log.switch, f"计划预占用 @ {log.line}")
        self.ctx = {
            "initial_products": {"L0": "A"},   # LIVE 前批为 A
            "start_after": {"L0": 21649},      # LIVE hold 释放
        }
        orders = sorted(self.pack.orders,
                        key=lambda o: o.original_start_min)
        self.assign = [
            (o, o.original_line,
             [c.factory for c in self.pack.capabilities
              if c.line == o.original_line][0])
            for o in orders]

    def test_early_prep_separated_from_line_minutes(self):
        plan = self.scheduler.build_scenario(
            "early", "允许提前", self.assign, **self.ctx)
        no_prep = self.scheduler.build_scenario(
            "noearly", "禁止提前", self.assign, prep_early=False, **self.ctx)
        # 提前准备只增加前移人工，不减少总人工；禁止提前后占线变长
        self.assertGreater(plan.total_overlap_prep_min, 0)
        self.assertGreaterEqual(
            no_prep.total_line_minutes, plan.total_line_minutes)
        self.assertEqual(no_prep.total_overlap_prep_min, 0)

    def test_shift_view_exposes_moved_labor(self):
        plan = self.scheduler.build_scenario(
            "early", "允许提前", self.assign, **self.ctx)
        view = plan.shift_view()
        moved = sum(v["prep_moved_to_earlier_shift"] for v in view)
        self.assertGreaterEqual(moved, 0)
        for v in view:
            for seg in v["prep_by_shift"]:
                self.assertGreater(seg["minutes"], 0)

    def test_tool_contention_detected_with_real_overlap(self):
        plan = self.scheduler.build_scenario(
            "early", "允许提前", self.assign, **self.ctx)
        tool_conflicts = [c for c in plan.resource_conflicts
                          if c["kind"] == "tool"]
        # LIVE 在 21645–21668 占用 M0，L1 的 PLAN1 窗口与之相交
        self.assertTrue(tool_conflicts)
        for c in tool_conflicts:
            lo, hi = c["conflict_interval"]
            self.assertLess(lo, hi)
            self.assertTrue(any(
                lo >= h["interval"][0] and hi <= h["interval"][1]
                for h in [{"interval": [21645, 21668]}])
                or c["with"] != "LIVE")

    def test_extra_shared_tool_reduces_conflicts(self):
        base = self.scheduler.build_scenario(
            "base", "基线", self.assign, **self.ctx)
        extra = self.scheduler.build_scenario(
            "extra", "L1 增配 M1", self.assign,
            extra_tools={"L1": ["M1"]}, **self.ctx)
        base_tool = len([c for c in base.resource_conflicts
                         if c["kind"] == "tool"])
        extra_tool = len([c for c in extra.resource_conflicts
                          if c["kind"] == "tool"])
        self.assertLessEqual(extra_tool, base_tool)

    def test_line_hold_blocks_release(self):
        cal = self.scheduler.calendar
        cal.add("L0", "line", (21640, 21649), "LIVE",
                "首件失败 hold", hold=True)
        self.assertIsNotNone(cal.line_hold("L0", 21645))
        self.assertIsNone(cal.line_hold("L0", 21650))

    def test_permutation_enumeration_returns_all_orders(self):
        from itertools import permutations
        plans = self.scheduler.enumerate_orders(
            self.pack.orders, start_after=self.ctx["start_after"],
            initial_products=self.ctx["initial_products"])
        self.assertEqual(len(plans),
                         len(list(permutations(self.pack.orders))))

    def test_promise_breach_flagged(self):
        plan = self.scheduler.build_scenario(
            "early", "允许提前", self.assign, **self.ctx)
        for s in plan.scheduled:
            self.assertEqual(s.promise_kept,
                             s.run_end_min <= s.order.promise_min)
        self.assertEqual(set(plan.promise_breaches),
                         {s.order.id for s in plan.scheduled
                          if not s.promise_kept})

    def test_run_time_scales_with_qty_and_rate(self):
        plan = self.scheduler.build_scenario(
            "early", "允许提前", self.assign, **self.ctx)
        for s in plan.scheduled:
            cap = self.pack.capability_index[(s.factory, s.line)]
            import math
            expected = math.ceil(s.order.qty / cap.rate_per_hour * 60)
            self.assertEqual(s.run_end_min - s.run_start_min, expected)


class CalendarTest(AppCase):
    def test_seed_only_keeps_future_occupancy(self):
        cal = ResourceCalendar(self.pack)
        cal.seed_from_history(self.facts, REVIEW)
        for occ in cal.occupancies:
            self.assertGreater(occ.interval[1], REVIEW)

    def test_contention_lists_blocked_lines(self):
        cal = ResourceCalendar(self.pack)
        cal.seed_from_history(self.facts, REVIEW)
        report = cal.contention_report(REVIEW, self.pack)
        m0 = next(r for r in report if r["resource"] == "M0")
        self.assertGreaterEqual(m0["occupied_until"], REVIEW)
        # L1 也配置 M0，应出现在候选被阻断产线中
        self.assertTrue(any("L1" in line for line in
                            m0["candidate_blocked_lines"]))
