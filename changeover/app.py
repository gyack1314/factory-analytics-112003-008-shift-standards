"""应用装配：把数据、解析、估计、台账、资源日历与排产器串成统一上下文。"""
from __future__ import annotations

from pathlib import Path

from .estimator import StandardEstimate, build_all_estimates, build_estimate
from .live import assess_live
from .models import DataPack, load_pack
from .parsing import ChangeoverFact, parse_all
from .resources import ResourceCalendar, build_historical_calendar
from .scheduling import Scheduler
from .standards import StandardsLibrary


class Application:
    """无状态业务服务的装配根，CLI 与测试都通过它访问领域能力。"""

    def __init__(self, data_dir: str | Path,
                 standards_state: str | Path | None = None):
        self.pack: DataPack = load_pack(data_dir)
        self.facts: dict[str, ChangeoverFact] = parse_all(self.pack)
        self.estimates_list = build_all_estimates(self.pack, self.facts)
        self.estimates: dict[
            tuple[str, str, str, str], StandardEstimate] = {
            (e.previous, e.next, e.factory, e.spec): e
            for e in self.estimates_list}
        state_path = Path(standards_state) if standards_state else None
        if state_path and state_path.exists():
            self.library = StandardsLibrary.load(state_path)
        else:
            self.library = StandardsLibrary.from_input_ledger(
                self._ledger_rows(data_dir))
        self._state_path = state_path

    @staticmethod
    def _ledger_rows(data_dir) -> list[dict]:
        import json
        return json.loads(
            (Path(data_dir) / "standards_ledger.json").read_text(
                encoding="utf-8"))

    def save_library(self) -> None:
        if self._state_path:
            self.library.save(self._state_path)

    def estimate_for(self, pair, factory, spec=None) -> StandardEstimate:
        """取某有向切换的估计；spec 缺省取该厂该方向最新版本。"""
        if spec is not None:
            return build_estimate(self.pack, self.facts, pair, factory, spec)
        specs = sorted(k[3] for k in self.estimates
                       if k[0] == pair[0] and k[1] == pair[1]
                       and k[2] == factory)
        if not specs:
            raise KeyError(f"无 {pair} @ {factory} 的历史样本")
        return self.estimates[(pair[0], pair[1], factory, specs[-1])]

    def calendar_at(self, review_min: int) -> ResourceCalendar:
        return build_historical_calendar(self.pack, self.facts, review_min)

    def scheduler(self, review_min: int) -> Scheduler:
        return Scheduler(self.pack, self.estimates, self.library,
                         self.calendar_at(review_min), review_min,
                         facts=self.facts)

    def live_switch(self, switch_id: str, review_min: int):
        fact = self.facts[switch_id]
        est = self.estimates.get(
            (fact.previous, fact.next, fact.factory, fact.spec))
        return assess_live(
            fact, est, self.library, review_min,
            self.pack.planning.missing_stage_time_limit_min)

    # ------------------------------------------------------------------
    def data_quality_report(self) -> dict:
        """输入资料的重复、矛盾与截尾体检。"""
        from collections import defaultdict
        exact = defaultdict(int)
        for log in self.pack.tool_logs:
            key = (log.tool, log.worker, log.line, log.switch,
                   log.start_min, log.end_min)
            exact[key] += 1
        dup_rows = sum(c - 1 for c in exact.values() if c > 1)

        # 同工具跨线时间矛盾
        by_tool = defaultdict(list)
        for log in self.pack.tool_logs:
            by_tool[log.tool].append(log)
        cross_conflicts = []
        for tool, rows in by_tool.items():
            rows.sort(key=lambda r: r.start_min)
            for a, b in zip(rows, rows[1:]):
                if b.start_min < a.end_min and (a.line, a.switch) != (
                        b.line, b.switch):
                    cross_conflicts.append(
                        {"tool": tool,
                         "a": [a.switch, a.line, a.start_min, a.end_min],
                         "b": [b.switch, b.line, b.start_min, b.end_min]})

        censored = [f.switch_id for f in self.facts.values() if f.censored]
        open_stages = [
            {"switch": f.switch_id, "open_stage": f.open_stage}
            for f in self.facts.values() if f.open_stage]
        return {
            "switchover_total": len(self.facts),
            "censored_switchovers": censored,
            "open_stages": open_stages,
            "tool_log_duplicate_rows": dup_rows,
            "tool_cross_line_conflicts": cross_conflicts,
            "equipment_wait_switchovers": sorted(
                f.switch_id for f in self.facts.values()
                if f.equipment_wait_min),
            "first_article_failures": sorted(
                f.switch_id for f in self.facts.values()
                if f.first_article_failed),
        }
