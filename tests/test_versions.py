"""计划版本与重排测试：开工订单冻结、连锁交期冲突、原版本保留。"""
from changeover.versions import PlanVersionStore, assess_replan
from tests._base import AppCase

REVIEW = 21670


class ReplanTest(AppCase):
    def setUp(self):
        self.scheduler = self.app.scheduler(REVIEW)
        orders = sorted(self.pack.orders,
                        key=lambda o: o.original_start_min)
        self.assign = [
            (o, o.original_line,
             [c.factory for c in self.pack.capabilities
              if c.line == o.original_line][0])
            for o in orders]
        self.current = self.scheduler.build_scenario(
            "current", "当前计划", self.assign,
            start_after={"L0": 21649},
            initial_products={"L0": "A"})

    def test_started_order_cannot_be_replanned(self):
        started = {"PLAN1"}
        bad_assign = [a for a in self.assign if a[0].id == "PLAN1"]
        result = assess_replan(
            self.pack, self.scheduler, self.current, bad_assign,
            REVIEW, started, "bad", "试图重排已开工订单")
        self.assertFalse(result["feasible"])
        self.assertIn("PLAN1", result["reason"])

    def test_locked_order_knockon_conflict_reported(self):
        # PLAN3 锁定且允诺 21760；未开工订单重排若延期必须显式报告
        reopen = [a for a in self.assign if a[0].id != "PLAN1"]
        result = assess_replan(
            self.pack, self.scheduler, self.current, reopen,
            REVIEW, {"PLAN1"}, "cand", "未开工重排")
        self.assertTrue(result["requires_planner_confirmation"])
        impacts = {i["order_id"]: i for i in result["order_impacts"]}
        self.assertIn("PLAN3", impacts)
        if not result["feasible"]:
            self.assertTrue(result["knock_on_conflicts"]
                            or result["new_plan_metrics"]["promise_breaches"])

    def test_responsible_shift_matches_review_time(self):
        reopen = [a for a in self.assign if a[0].id != "PLAN1"]
        result = assess_replan(
            self.pack, self.scheduler, self.current, reopen,
            REVIEW, {"PLAN1"}, "cand", "未开工重排")
        boundaries = self.pack.planning.shift_boundaries_min
        expected = next(k for k in range(len(boundaries) - 1)
                        if boundaries[k] <= REVIEW < boundaries[k + 1])
        self.assertEqual(result["responsible_shift"], expected)

    def test_version_store_keeps_parent(self, tmp_path=None):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            store = PlanVersionStore(Path(td) / "versions.json")
            v1 = store.commit(self.current, REVIEW, "计划员", "V1/V2",
                              responsible_shift=0, confirmed=True)
            v2 = store.commit(self.current, REVIEW + 10, "计划员", "V2",
                              responsible_shift=0, parent=v1,
                              note="重排", confirmed=True)
            self.assertIsNone(v1.parent_version_id)
            self.assertEqual(v2.parent_version_id, v1.version_id)
            # 原版本仍在
            reloaded = PlanVersionStore(Path(td) / "versions.json")
            ids = [v.version_id for v in reloaded.versions]
            self.assertEqual(ids, [v1.version_id, v2.version_id])
            self.assertTrue(reloaded.versions[0].confirmed)

    def test_locked_orders_recorded_on_version(self, tmp_path=None):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            store = PlanVersionStore(Path(td) / "v.json")
            v = store.commit(self.current, REVIEW, "计划员", "std",
                             responsible_shift=0)
            self.assertIn("PLAN1", v.locked_orders)
            self.assertIn("PLAN3", v.locked_orders)
