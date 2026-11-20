"""执行中换型（如 LIVE）的剩余时间推算与标准版本衔接。

规则（对应需求）：

* 标准生效正逢旧版排出的跨班计划已部分开工时，**继续沿用开工时的版本**，
  不能按新标准从头估计；
* 剩余时间 = 估计总量 − 已完成阶段实际工时，且只对未开始/进行中的工序
  做估计，已签字/完成的工序以实际凭证为准；
* 首件失败、重检未放行时，产线带 hold，不是可释放资源；
* 新版本的影响只体现在「同方向尚未开工」的换型上。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from .estimator import StandardEstimate
from .parsing import ChangeoverFact, CLEAN_STAGE, FIRST_ARTICLE_STAGE, \
    PREP_STAGE, REINSPECT_STAGE
from .standards import StandardsLibrary


@dataclass
class LiveAssessment:
    switch_id: str
    line: str
    factory: str
    pair: tuple[str, str]
    spec_at_start: str               # 开工时实际版本，锁定
    current_effective_spec: str | None
    version_switch_pending: bool
    stop_min: int
    review_min: int
    completed_stages: list[dict]
    open_stage: str | None
    observed_line_minutes: int       # 停线至复核时刻
    remaining_minutes: int
    remaining_breakdown: dict
    first_article_failed: bool
    line_released: bool
    hold_until_min: int | None
    reasons: list[str]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pair"] = list(self.pair)
        return d


# 各工序在估计中的取数键
_STAGE_METRIC = {
    PREP_STAGE: "post_stop_prep",
    CLEAN_STAGE: "cleaning",
    FIRST_ARTICLE_STAGE: "first_article",
    REINSPECT_STAGE: "reinspect",
}


def assess_live(fact: ChangeoverFact, estimate: StandardEstimate | None,
                library: StandardsLibrary,
                review_min: int,
                missing_stage_time_limit_min: int = 180) -> LiveAssessment:
    """基于实际阶段评估执行中换型。"""
    reasons: list[str] = []
    pair = fact.pair
    active = library.active_version(pair, fact.factory, review_min)
    current_spec = active.spec if active else None
    version_pending = current_spec is not None and current_spec != fact.spec

    if version_pending:
        reasons.append(
            f"换型按 {fact.spec} 开工且已部分完成；生效中的 {current_spec} "
            "不回溯适用，剩余推算继续使用开工版本")
    if estimate is not None and estimate.spec != fact.spec:
        reasons.append(
            f"传入估计版本 {estimate.spec} 与开工版本 {fact.spec} 不一致，"
            "已拒绝按新版本重算")
        estimate = None

    completed = []
    done_minutes: dict[str, int] = {}
    open_stage = None
    for stg in fact.stages:
        if stg.stage == "设备等待":
            continue
        if stg.is_open:
            open_stage = stg.stage
            observed = review_min - stg.start_min
            if observed > missing_stage_time_limit_min:
                reasons.append(
                    f"开放工序 {stg.stage} 已持续 {observed} 分钟，"
                    f"超过缺工时限 {missing_stage_time_limit_min} 分钟，"
                    "需补录结束凭证后才能收口")
            completed.append({
                "stage": stg.stage, "status": stg.result,
                "observed_min": observed,
                "over_time_limit": observed > missing_stage_time_limit_min,
                "end_min": None})
        else:
            dur = stg.end_min - stg.start_min
            done_minutes[stg.stage] = done_minutes.get(stg.stage, 0) + dur
            completed.append({
                "stage": stg.stage, "status": stg.result,
                "actual_min": dur, "end_min": stg.end_min})

    # —— 剩余分解：未完成工序按开工版本估计，已完成按实际 ——
    remaining_breakdown: dict[str, int] = {}
    if estimate is None:
        # 无同版本估计：用该换型自身已观察节奏给保守剩余
        reasons.append("无开工版本估计，剩余时间按已观察工序时长保守推算")
        for stage_name in (CLEAN_STAGE, FIRST_ARTICLE_STAGE, REINSPECT_STAGE):
            present = any(s.stage == stage_name for s in fact.stages)
            if not present:
                remaining_breakdown[stage_name] = 15
        if open_stage:
            remaining_breakdown[f"{open_stage}(进行中)"] = 10
    else:
        for stage_name, metric in _STAGE_METRIC.items():
            block = estimate.metrics.get(metric)
            expected = round(block.dist.mean) if block and block.dist.mean else 0
            if stage_name == REINSPECT_STAGE:
                # 仅首件失败后才需要重检
                if not fact.first_article_failed:
                    continue
                if block and block.dist.n == 0:
                    expected = 12
            already = done_minutes.get(stage_name, 0)
            stage_present = any(s.stage == stage_name for s in fact.stages)
            if already and stage_present and stage_name != open_stage:
                continue  # 已完成，以实际为准
            if stage_present and stage_name == open_stage:
                # 进行中：剩余 = 期望总量 − 已观察
                observed = review_min - next(
                    s.start_min for s in fact.stages
                    if s.stage == stage_name and s.is_open)
                remaining_breakdown[f"{stage_name}(进行中)"] = max(
                    0, expected - observed)
            elif not stage_present:
                remaining_breakdown[stage_name] = expected

    remaining = sum(remaining_breakdown.values())
    observed_line = review_min - fact.stop_min

    line_released = fact.restart_min is not None and not fact.reinspect_open
    hold_until = None
    if fact.reinspect_open or (fact.first_article_failed and not line_released):
        hold_until = review_min + remaining
        reasons.append(
            "首件失败且重检未放行：产线保持 hold，不得释放给下张订单")

    return LiveAssessment(
        switch_id=fact.switch_id, line=fact.line, factory=fact.factory,
        pair=pair, spec_at_start=fact.spec,
        current_effective_spec=current_spec,
        version_switch_pending=version_pending,
        stop_min=fact.stop_min, review_min=review_min,
        completed_stages=completed, open_stage=open_stage,
        observed_line_minutes=observed_line,
        remaining_minutes=remaining,
        remaining_breakdown=remaining_breakdown,
        first_article_failed=fact.first_article_failed,
        line_released=line_released,
        hold_until_min=hold_until, reasons=reasons)
