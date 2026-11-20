"""CLI 端到端冒烟测试与输入体检。"""
import io
import json
import sys
from contextlib import redirect_stdout

from run_cli import main
from tests._base import AppCase


class CliSmokeTest(AppCase):
    def _run(self, *argv):
        buf = io.StringIO()
        old = sys.argv
        sys.argv = ["run_cli.py", *argv]
        try:
            with redirect_stdout(buf):
                main()
        finally:
            sys.argv = old
        return json.loads(buf.getvalue())

    def test_quality_reports_known_quirks(self):
        d = self._run("quality")
        self.assertEqual(d["switchover_total"], 181)
        self.assertIn("LIVE", d["censored_switchovers"])
        self.assertEqual(d["tool_log_duplicate_rows"], 23)
        self.assertTrue(d["tool_cross_line_conflicts"])
        # 45 条历史 + LIVE 共 46 次首件失败
        self.assertEqual(len(d["first_article_failures"]), 46)

    def test_estimates_command(self):
        rows = self._run("estimates")
        self.assertEqual(len(rows), 36)
        self.assertTrue(all("line_band" in r for r in rows))

    def test_live_command(self):
        d = self._run("live", "--switch", "LIVE", "--review-min", "21645")
        self.assertEqual(d["spec_at_start"], "V1")
        self.assertFalse(d["line_released"])

    def test_compare_command_has_four_scenarios(self):
        d = self._run("compare", "--review-min", "21645")
        names = [s["scenario"] for s in d["scenarios"]]
        self.assertEqual(
            names, ["baseline", "no_early_prep", "best_sequence",
                    "extra_shared_tool"])
        # 解释口径存在
        self.assertIn("前移到", d["interpretation"])

    def test_gaps_uncovered_directions(self):
        d = self._run("gaps")
        self.assertIn("uncovered_directions", d)
        pairs = [tuple(x["pair"]) for x in d["uncovered_directions"]]
        # 订单方向 B→C（洁净变化）必然在未覆盖清单
        self.assertIn(("B", "C"), pairs)

    def test_revisions_command(self):
        d = self._run("revisions", "--pair", "A,B", "--factory", "一厂")
        self.assertEqual([c["spec"] for c in d["revision_chain"]],
                         ["V1", "V2"])
