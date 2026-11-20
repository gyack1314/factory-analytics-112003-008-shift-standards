"""资源日历：产线、共用工具、合格技师的占用与可行性。

三类资源的占用都建模为半开区间 ``[start, end)``：

* 产线：换型占线（停线→量产重启）+ 量产运行；
* 共用工具：tool_logs 口径的借用区间，跨产线互斥；
* 技师：工序/借用工时，同一技师同一时刻只能出现在一处。

首件失败的换型在重检放行前，产线 **不能** 作为释放给下张订单的空闲
资源，本模块用 ``hold_until`` 显式表达该约束。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import DataPack
from .timeutils import Interval, merge, overlap, union_len


@dataclass
class Occupancy:
    resource: str
    kind: str            # line / tool / worker
    interval: Interval
    owner: str           # 订单或换型 id
    note: str = ""
    hold: bool = False   # 首件失败未放行的占用


@dataclass
class ResourceCalendar:
    pack: DataPack
    occupancies: list[Occupancy] = field(default_factory=list)

    def add(self, resource: str, kind: str, interval: Interval,
            owner: str, note: str = "", hold: bool = False) -> None:
        self.occupancies.append(
            Occupancy(resource, kind, interval, owner, note, hold))

    def of(self, kind: str, resource: str | None = None) -> list[Occupancy]:
        rows = [o for o in self.occupancies if o.kind == kind]
        if resource is not None:
            rows = [o for o in rows if o.resource == resource]
        return sorted(rows, key=lambda o: o.interval)

    def is_free(self, kind: str, resource: str,
                interval: Interval) -> bool:
        return all(overlap(interval, o.interval) == 0
                   for o in self.of(kind, resource))

    def conflicts(self, kind: str, resource: str,
                  interval: Interval) -> list[Occupancy]:
        return [o for o in self.of(kind, resource)
                if overlap(interval, o.interval) > 0]

    def busy_intervals(self, kind: str, resource: str) -> list[Interval]:
        return merge([o.interval for o in self.of(kind, resource)])

    def next_free_at(self, kind: str, resource: str,
                     after: int) -> int:
        """资源在 after 之后最早空闲时刻（含 hold 占用）。"""
        cursor = after
        for start, end in self.busy_intervals(kind, resource):
            if end <= cursor:
                continue
            if start <= cursor < end:
                cursor = end
            else:
                break
        return cursor

    # ------------------------------------------------------------------
    def seed_from_history(self, facts, review_min: int) -> None:
        """把历史与执行中换型的工具/技师占用播种到日历。

        只播种 review_min 时仍未结束或跨越 review 时刻的占用，
        用于评估当前等待的模具/技师对其他线顺序的影响。
        """
        for fact in facts.values():
            for use in fact.tool_uses:
                for start, end in use.intervals:
                    if end > review_min:
                        self.add(use.tool, "tool",
                                 (max(start, review_min), end),
                                 fact.switch_id,
                                 f"历史/在执行借用 @ {fact.line}",
                                 hold=fact.censored)
            for stg in fact.stages:
                if stg.worker and stg.end_min and stg.end_min > review_min \
                        and stg.start_min < review_min:
                    self.add(stg.worker, "worker",
                             (review_min, stg.end_min), fact.switch_id,
                             f"未完工序 {stg.stage} @ {fact.line}")

    def line_hold(self, line: str, at_min: int) -> Occupancy | None:
        """时刻 at_min 产线是否处于首件失败未放行的 hold。"""
        for o in self.of("line", line):
            if o.hold and o.interval[0] <= at_min < o.interval[1]:
                return o
        return None

    def contention_report(self, review_min: int,
                          pack=None) -> list[dict]:
        """当前等待中的模具/技师对其他产线可行顺序的影响。

        ``pack`` 给出时，额外列出同样需要该资源的其他产线，即被阻断方。
        """
        report = []
        for kind in ("tool", "worker"):
            resources = sorted({o.resource for o in self.of(kind)
                                if o.interval[1] > review_min})
            for resource in resources:
                holders = [o for o in self.of(kind, resource)
                           if o.interval[1] > review_min]
                holder_lines = {
                    note.split("@ ", 1)[1]
                    for o in holders if "@ " in o.note
                    for note in [o.note]}
                blocked_lines = []
                if pack is not None and kind == "tool":
                    blocked_lines = sorted({
                        f"{c.factory}/{c.line}"
                        for c in pack.capabilities
                        if resource in c.tools
                        and c.line not in holder_lines})
                elif pack is not None and kind == "worker":
                    blocked_lines = sorted({
                        f"{c.factory}/{c.line}"
                        for c in pack.capabilities
                        if resource in c.qualified_workers
                        and c.line not in holder_lines})
                report.append({
                    "resource": resource,
                    "kind": kind,
                    "occupied_until": max(o.interval[1] for o in holders),
                    "holders": [
                        {"owner": o.owner, "interval": list(o.interval),
                         "note": o.note, "hold": o.hold}
                        for o in holders],
                    "candidate_blocked_lines": blocked_lines,
                })
        return report


def build_historical_calendar(pack: DataPack, facts,
                              review_min: int) -> ResourceCalendar:
    """构建 review_min 时点的资源日历（含 LIVE 等在执行换型）。"""
    cal = ResourceCalendar(pack)
    cal.seed_from_history(facts, review_min)
    return cal


def worker_utilization(pack: DataPack, facts) -> dict[str, dict]:
    """技师维度的人工投入与并行重复统计（供复核与排产参考）。"""
    out: dict[str, dict] = {}
    for fact in facts.values():
        per_worker: dict[str, list[Interval]] = {}
        for stg in fact.stages:
            if stg.worker and stg.end_min:
                per_worker.setdefault(stg.worker, []).append(
                    (stg.start_min, stg.end_min))
        for worker, intervals in per_worker.items():
            rec = out.setdefault(worker, {"union_min": 0, "raw_min": 0,
                                          "switches": set()})
            rec["union_min"] += union_len(intervals)
            rec["raw_min"] += sum(end - start for start, end in intervals)
            rec["switches"].add(fact.switch_id)
    return {w: {"union_min": r["union_min"],
                "double_counted_min": r["raw_min"] - r["union_min"],
                "switch_count": len(r["switches"])}
            for w, r in sorted(out.items())}
