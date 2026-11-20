"""把单条历史换型凭证重建为时间线事实。

输出的 :class:`ChangeoverFact` 是后续估计、复核与排产的统一口径：

* ``line_occupied_min``      产线真实占线时间（停线→量产重启）
* ``overlap_prep_min``       预准备落在停线之前、可与上一批运行重叠的时间
* ``manual_min``             技师人工投入（同时段多工序并行只算一次）
* ``double_counted_min``     若把各工序工时直接相加会被重复计入的部分
* ``equipment_wait_min``     设备故障等待（无技师、可与工序并行，需单列）
* ``first_article_failed``   首件是否失败、是否走过重检
* ``censored``               换型是否尚未结束
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from .models import DataPack, StageRecord, SwitchRecord, ToolLog
from .timeutils import Interval, merge, overlap, union_len

# 工序语义分组（与输入 result/stage 文案对应）
PREP_STAGE = "预准备"
CLEAN_STAGE = "清洗"
FIRST_ARTICLE_STAGE = "首件"
REINSPECT_STAGE = "重检"
EQUIPMENT_WAIT_STAGE = "设备等待"

WORK_STAGES = (PREP_STAGE, CLEAN_STAGE, FIRST_ARTICLE_STAGE, REINSPECT_STAGE)


@dataclass
class StageFact:
    stage: str
    start_min: int
    end_min: Optional[int]
    result: str
    worker: Optional[str]
    duration_min: Optional[int]          # 开放工序为 None
    is_open: bool


@dataclass
class ToolUse:
    tool: str
    worker: str
    line: str
    intervals: list[Interval]             # 去重后的实际占用区间
    raw_rows: int                         # 原始转抄行数（含重复）
    duplicate_rows: int


@dataclass
class ChangeoverFact:
    switch_id: str
    factory: str
    line: str
    previous: str
    next: str
    spec: str
    stop_min: int
    restart_min: Optional[int]
    signed_min: Optional[int]

    stages: list[StageFact]
    tool_uses: list[ToolUse]

    # —— 时间线口径（分钟）——
    line_occupied_min: Optional[int]      # 截尾时为 None
    overlap_prep_min: int                 # 停线前完成的预准备（真实可重叠）
    post_stop_prep_min: int               # 预准备拖到停线后的部分
    cleaning_min: int
    first_article_min: int
    reinspect_min: int                    # 0 表示未触发重检
    equipment_wait_min: int
    equipment_wait_on_line_min: int       # 设备等待中真正压在占线窗口内的部分
    manual_min: Optional[int]             # 技师并行去重后的人工投入
    double_counted_min: int               # 工序工时直接求和的虚增
    sum_stage_min: int                    # 各工序工时直接求和（旧口径对照）

    first_article_failed: bool
    reinspect_open: bool
    censored: bool
    open_stage: Optional[str]
    data_flags: list[str] = field(default_factory=list)

    @property
    def pair(self) -> tuple[str, str]:
        return self.previous, self.next

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _dedup_tool_logs(logs: list[ToolLog]) -> list[ToolUse]:
    """同一工具/技师/产线/换型的重复转抄按区间并集去重。"""
    grouped: dict[tuple, dict] = {}
    for log in logs:
        key = (log.tool, log.worker, log.line, log.switch)
        bucket = grouped.setdefault(
            key, {"intervals": [], "raw": 0, "exact_seen": set()})
        bucket["intervals"].append((log.start_min, log.end_min))
        bucket["raw"] += 1
        bucket["exact_seen"].add((log.start_min, log.end_min))
    uses: list[ToolUse] = []
    for (tool, worker, line, _switch), bucket in sorted(grouped.items()):
        merged = merge(bucket["intervals"])
        # 重复行 = 原始行数 − 不同区间数（相邻合并不算重复转抄）
        duplicate_rows = bucket["raw"] - len(bucket["exact_seen"])
        uses.append(ToolUse(
            tool=tool, worker=worker, line=line,
            intervals=merged,
            raw_rows=bucket["raw"],
            duplicate_rows=duplicate_rows,
        ))
    return uses


def parse_switch(switch: SwitchRecord,
                 stages: list[StageRecord],
                 tool_logs: list[ToolLog],
                 missing_stage_time_limit_min: int) -> ChangeoverFact:
    """重建一条换型的时间线事实。"""
    flags: list[str] = []
    stage_facts: list[StageFact] = []
    open_stage: Optional[str] = None

    for stg in stages:
        is_open = stg.is_open
        duration = None if is_open else stg.end_min - stg.start_min
        if is_open:
            open_stage = stg.stage
            flags.append(f"开放工序:{stg.stage}")
        stage_facts.append(StageFact(
            stage=stg.stage, start_min=stg.start_min, end_min=stg.end_min,
            result=stg.result, worker=stg.worker,
            duration_min=duration, is_open=is_open,
        ))

    # 截尾判定：没有量产重启证据，或存在仍在进行的工序
    censored = switch.is_censored or open_stage is not None
    if censored and not switch.is_censored:
        flags.append("有重启时间但存在开放工序，按未结束处理")

    def _closed(name: str) -> list[StageFact]:
        return [s for s in stage_facts if s.stage == name and not s.is_open]

    def _stage_total(name: str) -> int:
        return sum(s.duration_min for s in _closed(name))

    # —— 预准备的可重叠部分：停线之前的区间 ——
    overlap_prep = 0
    post_stop_prep = 0
    for s in stage_facts:
        if s.stage != PREP_STAGE:
            continue
        end = s.end_min if s.end_min is not None else switch.stop_min
        pre = overlap((s.start_min, end),
                      (-10**9, switch.stop_min))
        overlap_prep += pre
        post_stop_prep += max(0, end - max(s.start_min, switch.stop_min))

    cleaning = _stage_total(CLEAN_STAGE)
    first_article = _stage_total(FIRST_ARTICLE_STAGE)
    reinspect = _stage_total(REINSPECT_STAGE)

    # —— 设备故障等待：无技师工序，单列并与人工工序区分 ——
    equip = _closed(EQUIPMENT_WAIT_STAGE)
    equipment_wait = sum(s.duration_min for s in equip)
    if switch.restart_min is not None:
        window = (switch.stop_min, switch.restart_min)
        equipment_wait_on_line = sum(
            overlap((s.start_min, s.end_min), window) for s in equip)
    else:
        equipment_wait_on_line = equipment_wait  # 截尾时先全计，复核时再裁

    # —— 技师人工投入：同一技师并行工序按时间并集去重 ——
    worker_intervals: dict[str, list[Interval]] = {}
    sum_stage = 0
    for s in stage_facts:
        if s.stage not in WORK_STAGES or s.is_open or s.worker is None:
            continue
        sum_stage += s.duration_min
        worker_intervals.setdefault(s.worker, []).append(
            (s.start_min, s.end_min))
    manual = sum(union_len(ivs) for ivs in worker_intervals.values())
    double_counted = sum_stage - manual

    line_occupied = (switch.restart_min - switch.stop_min
                     if switch.restart_min is not None else None)

    fa_records = [s for s in stage_facts if s.stage == FIRST_ARTICLE_STAGE]
    first_failed = any(s.result == "失败" for s in fa_records)
    reinspect_open = any(s.stage == REINSPECT_STAGE and s.is_open
                         for s in stage_facts)

    uses = _dedup_tool_logs(tool_logs)
    if any(u.duplicate_rows for u in uses):
        flags.append("工具日志存在重复转抄（已按区间并集去重）")

    return ChangeoverFact(
        switch_id=switch.id, factory=switch.factory, line=switch.line,
        previous=switch.previous, next=switch.next, spec=switch.spec,
        stop_min=switch.stop_min, restart_min=switch.restart_min,
        signed_min=switch.signed_min,
        stages=stage_facts, tool_uses=uses,
        line_occupied_min=line_occupied,
        overlap_prep_min=overlap_prep,
        post_stop_prep_min=post_stop_prep,
        cleaning_min=cleaning,
        first_article_min=first_article,
        reinspect_min=reinspect,
        equipment_wait_min=equipment_wait,
        equipment_wait_on_line_min=equipment_wait_on_line,
        manual_min=manual,
        double_counted_min=double_counted,
        sum_stage_min=sum_stage,
        first_article_failed=first_failed,
        reinspect_open=reinspect_open,
        censored=censored,
        open_stage=open_stage,
        data_flags=flags,
    )


def parse_all(pack: DataPack) -> dict[str, ChangeoverFact]:
    """解析全部换型，返回 switch_id -> fact。"""
    logs_by_switch: dict[str, list[ToolLog]] = {}
    for log in pack.tool_logs:
        logs_by_switch.setdefault(log.switch, []).append(log)

    facts: dict[str, ChangeoverFact] = {}
    for switch in pack.switchovers:
        facts[switch.id] = parse_switch(
            switch,
            pack.stages_of(switch.id),
            logs_by_switch.get(switch.id, []),
            pack.planning.missing_stage_time_limit_min,
        )
    return facts
