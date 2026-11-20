"""历史已完成班次的计划偏差复核。

把一条计划换型（或方案中的 :class:`ScheduledOrder`）与实际凭证对齐，
输出按工序可追溯的偏差分解。不同「重叠解释」的差异在这里显式化：

* ``naive_sum``   把各工序工时直接相加——含同技师并行重复计入；
* ``union_manual`` 同技师并行按时间并集去重后的人工口径；
* ``line_actual`` 停线→量产重启的真实占线——含设备等待与重检；
* ``overlap_prep`` 停线前预准备——真实可重叠，不计入占线偏差。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from .parsing import ChangeoverFact
from .scheduling import ScheduledOrder
from .timeutils import Interval, overlap, union_len


@dataclass
class StageDeviation:
    stage: str
    planned_min: int | None
    actual_min: int | None
    delta_min: int | None
    note: str


@dataclass
class ReconciliationReport:
    switch_id: str
    order_id: str | None
    line: str
    pair: tuple[str, str]
    planned_line_minutes: int | None
    actual_line_minutes: int | None
    line_delta_min: int | None
    planned_manual_minutes: int | None
    actual_manual_minutes: int
    naive_stage_sum_min: int
    double_counted_min: int
    overlap_prep_actual_min: int
    equipment_wait_actual_min: int
    reinspect_actual_min: int
    first_article_failed: bool
    stage_deviations: list[StageDeviation]
    attributions: list[str]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pair"] = list(self.pair)
        return d


def reconcile(fact: ChangeoverFact,
              scheduled: ScheduledOrder | None = None) -> ReconciliationReport:
    """对齐计划与实际。scheduled 为 None 时只出实际口径分析。"""
    planned_line = scheduled.changeover.line_minutes if scheduled else None
    planned_manual = scheduled.changeover.manual_minutes if scheduled else None

    actual_line = fact.line_occupied_min
    line_delta = (actual_line - planned_line
                  if actual_line is not None and planned_line is not None
                  else None)

    # 人工口径：同技师并行并集
    worker_intervals: dict[str, list[Interval]] = {}
    naive_sum = 0
    stage_actuals: dict[str, int] = {}
    for stg in fact.stages:
        if stg.stage == "设备等待" or stg.is_open or stg.worker is None:
            continue
        dur = stg.end_min - stg.start_min
        naive_sum += dur
        stage_actuals[stg.stage] = stage_actuals.get(stg.stage, 0) + dur
        worker_intervals.setdefault(stg.worker, []).append(
            (stg.start_min, stg.end_min))
    actual_manual = sum(union_len(v) for v in worker_intervals.values())

    # 工序级偏差
    planned_map = {}
    if scheduled:
        co = scheduled.changeover
        planned_map = {
            "预准备": co.post_stop_prep_min + co.overlap_prep_min,
            "清洗": co.cleaning_min,
            "首件": co.first_article_min,
            "重检": co.reinspect_allowance_min,
        }
    deviations: list[StageDeviation] = []
    for stage in ("预准备", "清洗", "首件", "重检", "设备等待"):
        actual = stage_actuals.get(stage)
        if stage == "设备等待":
            actual = fact.equipment_wait_min or None
        planned = planned_map.get(stage)
        note = ""
        if stage == "重检" and actual and not planned:
            note = "计划未预留重检，首件失败导致的纯增量"
        if stage == "设备等待" and actual:
            note = "设备故障等待，不属于人工工时"
        delta = (actual - planned) if actual is not None and planned is not None else None
        if actual is not None or planned is not None:
            deviations.append(StageDeviation(
                stage=stage, planned_min=planned, actual_min=actual,
                delta_min=delta, note=note))

    # 归因
    attributions: list[str] = []
    if fact.first_article_failed and fact.reinspect_min:
        attributions.append(
            f"首件失败重检 {fact.reinspect_min} 分钟实际计入占线"
            + ("，计划未预留" if not scheduled
               or scheduled.changeover.reinspect_allowance_min == 0
               else "（计划已预留）"))
    if fact.equipment_wait_on_line_min:
        attributions.append(
            f"设备故障等待压在占线窗口内 "
            f"{fact.equipment_wait_on_line_min} 分钟")
    prep_spill = fact.post_stop_prep_min
    if prep_spill and scheduled and scheduled.changeover.overlap_prep_min:
        attributions.append(
            f"预准备 {prep_spill} 分钟溢出到停线后（可重叠时间未充分前移）")
    if fact.double_counted_min:
        attributions.append(
            f"旧口径把同技师并行工序重复计入 {fact.double_counted_min} 分钟，"
            "并集口径下该偏差消失")
    if line_delta is not None and not attributions:
        attributions.append(
            f"占线偏差 {line_delta} 分钟落在工序自然波动内")

    return ReconciliationReport(
        switch_id=fact.switch_id,
        order_id=scheduled.order.id if scheduled else None,
        line=fact.line, pair=fact.pair,
        planned_line_minutes=planned_line,
        actual_line_minutes=actual_line,
        line_delta_min=line_delta,
        planned_manual_minutes=planned_manual,
        actual_manual_minutes=actual_manual,
        naive_stage_sum_min=naive_sum,
        double_counted_min=fact.double_counted_min,
        overlap_prep_actual_min=fact.overlap_prep_min,
        equipment_wait_actual_min=fact.equipment_wait_min,
        reinspect_actual_min=fact.reinspect_min,
        first_article_failed=fact.first_article_failed,
        stage_deviations=deviations,
        attributions=attributions)


def reconcile_shift(facts: dict[str, ChangeoverFact],
                    scheduled_map: dict[str, ScheduledOrder] | None = None,
                    shift_window: Interval | None = None
                    ) -> list[ReconciliationReport]:
        """对一个班次窗口内已完成的换型批量复核。"""
        scheduled_map = scheduled_map or {}
        reports = []
        for switch_id, fact in facts.items():
            if fact.censored:
                continue
            if shift_window is not None:
                if fact.restart_min is None or not overlap(
                        (fact.stop_min, fact.restart_min), shift_window):
                    continue
            reports.append(
                reconcile(fact, scheduled_map.get(switch_id)))
        return sorted(reports, key=lambda r: r.switch_id)
