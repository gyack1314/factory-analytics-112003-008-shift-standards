"""排产方案构建与对比。

每个方案把订单排成产线上的有序操作序列，每张订单前的换型拆成：

* ``overlap_prep_min``  可在上一批运行时做的预准备——只是前移到前一班的
  人工时间，不减少占线；
* ``line_minutes``      停线后真正压在产线上的时间——顺序优化、提前准备、
  增加共用工具只能改变这部分；
* ``equipment_wait_min`` 设备故障等待，单列、不与人工口径混淆。

资源可行性同时检查产线、共用工具与合格技师；首件失败未放行的产线
带 hold，不能释放给下张订单。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from itertools import permutations

from .models import DataPack, Order
from .resources import ResourceCalendar
from .standards import StandardsLibrary
from .timeutils import split_by_shifts


@dataclass
class PlannedChangeover:
    line: str
    factory: str
    previous_product: str | None
    next_product: str
    spec: str
    overlap_prep_min: int       # 前移人工（不占线）
    post_stop_prep_min: int
    cleaning_min: int
    first_article_min: int
    reinspect_allowance_min: int
    equipment_wait_min: int
    line_minutes: int           # 真正占线时间
    manual_minutes: int         # 人工总量（含前移部分）
    estimate_source: str
    blocked_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScheduledOrder:
    order: Order
    line: str
    factory: str
    changeover_start_min: int   # 预准备开始（可能在上一班）
    stop_min: int               # 产线停线点
    run_start_min: int          # 量产开始 = 停线 + 占线
    run_end_min: int
    changeover: PlannedChangeover
    feasible: bool
    issues: list[str] = field(default_factory=list)
    promise_kept: bool = True

    def to_dict(self) -> dict:
        d = {
            "order_id": self.order.id,
            "product": self.order.product,
            "qty": self.order.qty,
            "locked": self.order.locked,
            "line": self.line,
            "factory": self.factory,
            "changeover_start_min": self.changeover_start_min,
            "stop_min": self.stop_min,
            "run_start_min": self.run_start_min,
            "run_end_min": self.run_end_min,
            "promise_min": self.order.promise_min,
            "promise_kept": self.promise_kept,
            "feasible": self.feasible,
            "issues": self.issues,
            "changeover": self.changeover.to_dict(),
        }
        return d


@dataclass
class ScenarioPlan:
    name: str
    description: str
    scheduled: list[ScheduledOrder]
    shift_boundaries: tuple[int, ...]
    reopen_margin_min: int

    # —— 汇总口径 ——
    total_line_minutes: int = 0
    total_overlap_prep_min: int = 0
    total_manual_minutes: int = 0
    total_equipment_wait_min: int = 0
    idle_gap_minutes: int = 0
    promise_breaches: list[str] = field(default_factory=list)
    resource_conflicts: list[dict] = field(default_factory=list)

    def metrics(self) -> dict:
        """结合订单批量与允诺时点的方案级指标。"""
        total_qty = sum(s.order.qty for s in self.scheduled)
        return {
            "scenario": self.name,
            "description": self.description,
            "orders": len(self.scheduled),
            "total_qty": total_qty,
            "total_line_minutes": self.total_line_minutes,
            "total_overlap_prep_min": self.total_overlap_prep_min,
            "total_manual_minutes": self.total_manual_minutes,
            "total_equipment_wait_min": self.total_equipment_wait_min,
            "idle_gap_minutes": self.idle_gap_minutes,
            "line_minutes_per_100_units": round(
                self.total_line_minutes / total_qty * 100, 2)
            if total_qty else None,
            "promise_breaches": self.promise_breaches,
            "resource_conflicts": self.resource_conflicts,
        }

    def shift_view(self) -> list[dict]:
        """按班次拆分人工与占线，暴露「前移到前一班的人工」。"""
        rows = []
        for s in self.scheduled:
            co = s.changeover
            prep_interval = (s.changeover_start_min, s.stop_min)
            line_interval = (s.stop_min, s.run_start_min)
            prep_shifts = split_by_shifts(prep_interval, self.shift_boundaries)
            line_shifts = split_by_shifts(line_interval, self.shift_boundaries)
            rows.append({
                "order_id": s.order.id,
                "line": s.line,
                "prep_by_shift": [
                    {"shift": k, "minutes": end - start,
                     "interval": [start, end]}
                    for k, (start, end) in prep_shifts],
                "line_by_shift": [
                    {"shift": k, "minutes": end - start,
                     "interval": [start, end]}
                    for k, (start, end) in line_shifts],
                "prep_moved_to_earlier_shift": sum(
                    end - start for k, (start, end) in prep_shifts
                    if k < _shift_of(s.stop_min, self.shift_boundaries)),
            })
        return rows

    def to_dict(self) -> dict:
        return {
            **self.metrics(),
            "scheduled": [s.to_dict() for s in self.scheduled],
            "shift_view": self.shift_view(),
        }


def _shift_of(t: int, boundaries: tuple[int, ...]) -> int:
    for k in range(len(boundaries) - 1):
        if boundaries[k] <= t < boundaries[k + 1]:
            return k
    return len(boundaries) - 1


class Scheduler:
    """基于标准估计与资源日历的方案构建器。"""

    def __init__(self, pack: DataPack, estimates: dict,
                 library: StandardsLibrary,
                 calendar: ResourceCalendar,
                 review_min: int,
                 facts: dict | None = None):
        self.pack = pack
        self.estimates = estimates          # (pair, factory, spec) -> estimate
        self.library = library
        self.calendar = calendar
        self.review_min = review_min
        self.facts = facts or {}
        self._line_factory = {(c.line, c.factory): c for c in pack.capabilities}
        self._line_default_factory = {
            c.line: c.factory for c in pack.capabilities}

    # ------------------------------------------------------------------
    def _estimate_for(self, previous: str | None, next_product: str,
                      factory: str, at_min: int) -> tuple[PlannedChangeover, list[str]]:
        reasons: list[str] = []
        if previous is None:
            # 首单无前批，只做基础准备
            co = PlannedChangeover(
                line="", factory=factory,
                previous_product=None, next_product=next_product,
                spec="-", overlap_prep_min=0, post_stop_prep_min=0,
                cleaning_min=0, first_article_min=12,
                reinspect_allowance_min=0, equipment_wait_min=0,
                line_minutes=12, manual_minutes=12,
                estimate_source="首单基础准备（无换型）")
            return co, reasons

        pair = (previous, next_product)
        active = self.library.active_version(pair, factory, at_min)
        spec = active.spec if active else self._latest_spec(pair, factory)
        est = self.estimates.get((pair, factory, spec))
        if est is None:
            # 无该方向直接估计：按迁移链找属性相容的替代方向
            est = self._transferable_estimate(pair, factory, spec)
            if est is not None:
                transfer_notes = sorted({
                    t.reasons[0]
                    for blk in est.metrics.values()
                    if blk.transfer and blk.transfer.reasons
                    for t in [blk.transfer]})
                reasons.append(
                    f"方向 {pair[0]}→{pair[1]} 无直接样本，迁移来源："
                    + "；".join(transfer_notes))
        if est is None:
            reasons.append(f"无 {pair[0]}→{pair[1]} {spec} 估计且无可用迁移来源，"
                           "按保守占位 60 分钟占线")
            return PlannedChangeover(
                line="", factory=factory, previous_product=previous,
                next_product=next_product, spec=spec,
                overlap_prep_min=15, post_stop_prep_min=5,
                cleaning_min=18, first_article_min=12,
                reinspect_allowance_min=10, equipment_wait_min=0,
                line_minutes=60, manual_minutes=45,
                estimate_source="保守占位"), reasons

        def m(name, default=0):
            block = est.metrics.get(name)
            return round(block.dist.mean) if block and block.dist.mean is not None else default

        overlap_prep = m("overlap_prep")
        post_prep = m("post_stop_prep")
        cleaning = m("cleaning")
        fa = m("first_article")
        reinspect = self._reinspect_allowance(est)
        equip = m("equipment_wait")
        line_minutes = m("line_occupied")
        manual = m("manual")
        # 设备等待若与工序并行，占线分布已含；保留单列值用于报告
        usable, why = self.library.usable_for_scheduling(
            pair, factory, spec, at_min)
        if not usable:
            reasons.extend(why)
        if not est.sufficient:
            reasons.append(est.sufficiency_note)
        co = PlannedChangeover(
            line="", factory=factory, previous_product=previous,
            next_product=next_product, spec=spec,
            overlap_prep_min=overlap_prep, post_stop_prep_min=post_prep,
            cleaning_min=cleaning, first_article_min=fa,
            reinspect_allowance_min=reinspect,
            equipment_wait_min=equip,
            line_minutes=line_minutes, manual_minutes=manual,
            estimate_source=(
                f"{spec} 直接样本 n={est.direct_complete_n}"
                + ("（含迁移样本）" if est.fallback_used else "")))
        return co, reasons

    def _latest_spec(self, pair, factory) -> str:
        specs = [k[2] for k in self.estimates
                 if k[0] == pair and k[1] == factory]
        return sorted(specs)[-1] if specs else "V1"

    def _transferable_estimate(self, pair, factory, spec):
        """无直接方向时，按迁移链（同属性跨族→可替代厂）构造估计。"""
        from .estimator import build_estimate
        target_site = next(
            ((c.factory, c.line) for c in self.pack.capabilities
             if c.factory == factory), (factory, ""))
        candidate = build_estimate(
            self.pack, self.facts, pair, factory, spec, target_site)
        if candidate.fallback_used and any(
                m.dist.n for m in candidate.metrics.values()):
            return candidate
        return None

    def _reinspect_allowance(self, est) -> int:
        """重检预留：只取首件失败样本的重检工时均值 × 经验失败率。"""
        block = est.metrics.get("reinspect")
        if block is None or block.dist.n == 0:
            return 0
        fail_rate = self._failure_rate(est)
        return round(block.dist.mean * fail_rate)

    def _failure_rate(self, est) -> float:
        # 从来源换型中统计首件失败比例
        return getattr(est, "first_article_failure_rate", 0.0)

    # ------------------------------------------------------------------
    def build_scenario(self, name: str, description: str,
                       assignments: list[tuple[Order, str, str]],
                       start_after: dict[str, int] | None = None,
                       extra_tools: dict[str, list[str]] | None = None,
                       prep_early: bool = True,
                       initial_products: dict[str, str] | None = None) -> ScenarioPlan:
        """按给定 (订单, 产线, 工厂) 顺序构建方案。

        assignments 即排产顺序；start_after 给出每条产线的最早可用时刻；
        extra_tools 模拟「增加共用工具」后的产线工具配置；
        initial_products 给出每条线上一批在制产品（来自执行中换型），
        使首单换型也能按有向方向取标准。
        """
        start_after = dict(start_after or {})
        extra_tools = extra_tools or {}
        initial_products = dict(initial_products or {})
        calendar = ResourceCalendar(self.pack)
        # 继承当前日历中未结束的占用
        for occ in self.calendar.occupancies:
            calendar.add(occ.resource, occ.kind, occ.interval,
                         occ.owner, occ.note, occ.hold)

        scheduled: list[ScheduledOrder] = []
        last_product: dict[str, str] = dict(initial_products)
        line_end: dict[str, int] = dict(start_after)
        conflicts: list[dict] = []

        for order, line, factory in assignments:
            cap = self.pack.capability_index[(factory, line)]
            earliest = max(order.release_min,
                           line_end.get(line, order.release_min))
            # hold：首件失败未放行的产线不能给下张订单
            hold = calendar.line_hold(line, earliest)
            issues: list[str] = []
            if hold:
                earliest = max(earliest, hold.interval[1])
                issues.append(
                    f"产线处于首件失败 hold，最早 {hold.interval[1]} 释放")

            previous = last_product.get(line)
            at_min = max(self.review_min, earliest)
            co, est_reasons = self._estimate_for(
                previous, order.product, factory, at_min)
            co.line = line
            issues.extend(est_reasons)

            # 提前准备：预准备在上一批运行窗口内完成（前移人工）
            prep_span = co.overlap_prep_min + co.post_stop_prep_min
            if prep_early and co.overlap_prep_min:
                co_start = earliest - co.overlap_prep_min
            else:
                # 不允许提前：全部预准备压到停线后，占线变长
                co_start = earliest
                if not prep_early:
                    co.line_minutes += co.overlap_prep_min
                    co.overlap_prep_min = 0
            stop = earliest
            run_start = stop + co.line_minutes
            run_minutes = self._run_minutes(order, cap.rate_per_hour)
            run_end = run_start + run_minutes

            # 资源可行性（本订单自己的工具/技师冲突）
            own_conflicts_before = sum(
                1 for c in conflicts if c["order_id"] == order.id)
            self._check_resources(calendar, order, line, factory, co,
                                  co_start, stop, run_start, run_end,
                                  extra_tools, issues, conflicts)
            new_conflicts = [c for c in conflicts
                             if c["order_id"] == order.id][
                                own_conflicts_before:]
            feasible = not new_conflicts
            promise_kept = run_end <= order.promise_min
            if not promise_kept:
                issues.append(
                    f"完工 {run_end} 晚于允诺 {order.promise_min}")

            sched = ScheduledOrder(
                order=order, line=line, factory=factory,
                changeover_start_min=co_start, stop_min=stop,
                run_start_min=run_start, run_end_min=run_end,
                changeover=co, feasible=feasible, issues=issues,
                promise_kept=promise_kept)
            scheduled.append(sched)

            calendar.add(line, "line", (stop, run_end), order.id,
                         f"{order.id} 换型+量产")
            line_end[line] = run_end + self.pack.planning.reopen_margin_min
            last_product[line] = order.product

        return self._assemble(name, description, scheduled, conflicts)

    def _run_minutes(self, order: Order, rate_per_hour: int) -> int:
        import math
        return max(1, math.ceil(order.qty / rate_per_hour * 60))

    def _check_resources(self, calendar: ResourceCalendar, order: Order,
                         line: str, factory: str, co: PlannedChangeover,
                         co_start: int, stop: int, run_start: int,
                         run_end: int, extra_tools: dict,
                         issues: list[str], conflicts: list[dict]) -> None:
        cap = self.pack.capability_index[(factory, line)]
        tools = list(cap.tools) + extra_tools.get(line, [])
        # 工具：换型窗口内任一共用工具被占即冲突（报告实际相交时段）
        # 同一订单自己的计划预占用不算冲突
        for tool in tools:
            hits = [h for h in calendar.conflicts(
                "tool", tool, (co_start, run_start))
                if h.owner != order.id]
            for hit in hits:
                lo = max(co_start, hit.interval[0])
                hi = min(run_start, hit.interval[1])
                conflicts.append({
                    "order_id": order.id, "resource": tool, "kind": "tool",
                    "requested_interval": [co_start, run_start],
                    "conflict_interval": [lo, hi],
                    "with": hit.owner, "note": hit.note})
                issues.append(
                    f"共用工具 {tool} 在 {lo}–{hi} 与 {hit.owner} 冲突"
                    f"（{hit.note}）")
        # 技师：换型窗口需要合格技师
        worker_window = (co_start, run_start)
        available = [w for w in cap.qualified_workers
                     if calendar.is_free("worker", w, worker_window)]
        if not available:
            hits = []
            for w in cap.qualified_workers:
                hits.extend(calendar.conflicts("worker", w, worker_window))
            conflicts.append({
                "order_id": order.id,
                "resource": list(cap.qualified_workers), "kind": "worker",
                "requested_interval": list(worker_window),
                "conflict_interval": list(worker_window),
                "with": sorted({h.owner for h in hits}),
                "note": "无合格技师可用"})
            issues.append("换型窗口无合格技师可用")
        else:
            chosen = available[0]
            calendar.add(chosen, "worker", worker_window, order.id,
                         f"{order.id} 换型技师")
            # 只登记实际需要的工具窗口：停线后到量产开始
            for tool in tools[:1]:
                calendar.add(tool, "tool", (stop, run_start), order.id,
                             f"{order.id} 借用 {tool}")

    def _assemble(self, name, description, scheduled,
                  conflicts) -> ScenarioPlan:
        plan = ScenarioPlan(
            name=name, description=description, scheduled=scheduled,
            shift_boundaries=self.pack.planning.shift_boundaries_min,
            reopen_margin_min=self.pack.planning.reopen_margin_min,
            resource_conflicts=conflicts)
        plan.total_line_minutes = sum(
            s.changeover.line_minutes for s in scheduled)
        plan.total_overlap_prep_min = sum(
            s.changeover.overlap_prep_min for s in scheduled)
        plan.total_manual_minutes = sum(
            s.changeover.manual_minutes for s in scheduled)
        plan.total_equipment_wait_min = sum(
            s.changeover.equipment_wait_min for s in scheduled)
        plan.promise_breaches = [
            s.order.id for s in scheduled if not s.promise_kept]
        # 产线空闲缺口
        by_line: dict[str, list[ScheduledOrder]] = {}
        for s in scheduled:
            by_line.setdefault(s.line, []).append(s)
        idle = 0
        for rows in by_line.values():
            rows.sort(key=lambda s: s.stop_min)
            for a, b in zip(rows, rows[1:]):
                idle += max(0, b.stop_min - a.run_end_min
                            - self.pack.planning.reopen_margin_min)
        plan.idle_gap_minutes = idle
        return plan

    # ------------------------------------------------------------------
    def enumerate_orders(self, orders: list[Order],
                         max_permutations: int = 24,
                         start_after: dict[str, int] | None = None,
                         initial_products: dict[str, str] | None = None
                         ) -> list[ScenarioPlan]:
        """小订单集下枚举顺序（保持产线归属），供顺序优化对比。"""
        if len(orders) > max_permutations:
            raise ValueError("订单过多，穷举不可行，请用启发式")
        plans = []
        for i, perm in enumerate(permutations(orders)):
            assignments = [(o, o.original_line,
                            self._factory_of(o.original_line))
                           for o in perm]
            plans.append(self.build_scenario(
                f"perm-{i}", f"顺序枚举 {[o.id for o in perm]}",
                assignments, start_after=start_after,
                initial_products=initial_products))
        return plans

    def _factory_of(self, line: str) -> str:
        caps = [c for c in self.pack.capabilities if c.line == line]
        return caps[0].factory if caps else ""
