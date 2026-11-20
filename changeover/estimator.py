"""换型工时标准估计。

对每个 **有向** 产品族切换 (previous → next) × 工厂 × 工艺口径版本，
从历史换型事实中构建分布，并显式区分：

* 真实可重叠时间（停线前的预准备）
* 技师工时重复计入（同技师并行工序的虚增）
* 尚未结束的换型（截尾样本，不进点估计，只报告截尾数量与已观察时长）
* 设备故障等待（从占线时间中单列，不并入人工口径）

样本不足时按 transfer.py 的规则沿「同厂跨族 → 可替代厂」降级，
并把迁移放大系数作用到不确定性上。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import DataPack
from .parsing import ChangeoverFact
from .timeutils import EmpiricalDistribution, distribution
from .transfer import TransferVerdict, judge_transfer

# 样本充分性门槛：直接完整样本达到该数才视为「足够近似样本」
SUFFICIENT_N = 5
# 降级链允许的最大放大系数
MAX_FACTOR = 1.3


@dataclass
class MetricBlock:
    """一个口径维度的分布结果。"""
    metric: str
    label: str
    dist: EmpiricalDistribution
    censored_observed: tuple[int, ...]   # 截尾样本已观察到的时长
    source_switch_ids: tuple[str, ...]
    transfer: TransferVerdict | None     # 非直接样本时的迁移判定

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "label": self.label,
            "distribution": self.dist.as_dict(),
            "censored_observed_min": list(self.censored_observed),
            "source_switch_ids": list(self.source_switch_ids),
            "transfer": self.transfer.to_dict() if self.transfer else None,
        }


@dataclass
class StandardEstimate:
    previous: str
    next: str
    factory: str
    spec: str
    metrics: dict[str, MetricBlock]
    direct_complete_n: int
    censored_n: int
    sufficient: bool
    sufficiency_note: str
    fallback_used: bool
    first_article_failure_rate: float
    blocked_reasons: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str, str]:
        return self.previous, self.next, self.factory, self.spec

    def point_estimate(self, metric: str) -> float | None:
        block = self.metrics.get(metric)
        if block is None or block.dist.mean is None:
            return None
        return round(block.dist.mean, 2)

    def uncertainty_band(self, metric: str) -> dict | None:
        """返回 p10/p90 经验带，并按迁移系数放大离散度。"""
        block = self.metrics.get(metric)
        if block is None or block.dist.p10 is None:
            return None
        factor = block.transfer.uncertainty_factor if block.transfer else 1.0
        mean = block.dist.mean
        return {
            "p10": round(mean - (mean - block.dist.p10) * factor, 2),
            "p90": round(mean + (block.dist.p90 - mean) * factor, 2),
            "inflated_by": factor,
            "n": block.dist.n,
        }

    def to_dict(self) -> dict:
        return {
            "previous": self.previous,
            "next": self.next,
            "factory": self.factory,
            "spec": self.spec,
            "direct_complete_n": self.direct_complete_n,
            "censored_n": self.censored_n,
            "sufficient": self.sufficient,
            "sufficiency_note": self.sufficiency_note,
            "fallback_used": self.fallback_used,
            "blocked_reasons": self.blocked_reasons,
            "metrics": {k: v.to_dict() for k, v in self.metrics.items()},
            "uncertainty": {k: self.uncertainty_band(k)
                            for k in self.metrics},
        }


# 输出口径：metric 键 -> 展示名、fact 属性、是否只统计首件失败样本
METRIC_SPECS = (
    ("line_occupied", "占线时间（停线→量产重启）", "line_occupied_min", False),
    ("overlap_prep", "可重叠预准备（停线前）", "overlap_prep_min", False),
    ("post_stop_prep", "停线后预准备", "post_stop_prep_min", False),
    ("cleaning", "清洗签字", "cleaning_min", False),
    ("first_article", "首件检验", "first_article_min", False),
    ("reinspect", "重检放行（仅失败样本）", "reinspect_min", True),
    ("equipment_wait", "设备故障等待", "equipment_wait_min", False),
    ("manual", "技师人工（并行去重后）", "manual_min", False),
    ("double_counted", "技师工时重复计入", "double_counted_min", False),
)


def _metric_value(fact: ChangeoverFact, attr: str,
                  failure_only: bool) -> float | None:
    if failure_only and not fact.first_article_failed:
        return None
    return getattr(fact, attr)


def _collect_direct(facts: list[ChangeoverFact], pair, factory, spec):
    """同方向、同厂、同口径的样本，拆完整/截尾。"""
    complete, censored = [], []
    for fact in facts:
        if fact.pair != pair or fact.factory != factory or fact.spec != spec:
            continue
        if fact.censored:
            censored.append(fact)
        else:
            complete.append(fact)
    return complete, censored


def _fallback_pool(pack: DataPack, facts: list[ChangeoverFact],
                   pair, site, spec):
    """直接样本不足时，收集可迁移的完整样本及其判定。

    优先同口径版本；迁移关系优先级 direct > cross_factory > cross_family。
    """
    ranked: list[tuple[int, TransferVerdict, ChangeoverFact]] = []
    priority = {"direct": 0, "cross_factory": 1, "cross_family": 2}
    for fact in facts:
        if fact.censored or fact.spec != spec:
            continue
        verdict = judge_transfer(pack, pair, site, fact.pair,
                                 (fact.factory, fact.line))
        if verdict.usable:
            ranked.append((priority[verdict.relation], verdict, fact))
    ranked.sort(key=lambda x: (x[0], x[2].switch_id))
    return ranked


def build_estimate(pack: DataPack, facts: dict[str, ChangeoverFact],
                   pair: tuple[str, str], factory: str,
                   spec: str,
                   site: tuple[str, str] | None = None) -> StandardEstimate:
    """构建单个有向切换的标准估计。"""
    if site is None:
        site = next(((c.factory, c.line) for c in pack.capabilities
                     if c.factory == factory), (factory, ""))
    all_facts = list(facts.values())
    complete, censored = _collect_direct(all_facts, pair, factory, spec)

    fallback_used = False
    blocked: list[str] = []
    # 迁移池只在直接完整样本不足时启用
    pool: list[tuple[int, TransferVerdict, ChangeoverFact]] = []
    if len(complete) < SUFFICIENT_N:
        pool = _fallback_pool(pack, all_facts, pair, site, spec)
        if not complete and not pool:
            blocked.append(
                f"无 {pair[0]}→{pair[1]} 直接样本，且三属性门槛下无可用迁移来源")

    metrics: dict[str, MetricBlock] = {}
    for metric, label, attr, failure_only in METRIC_SPECS:
        direct_vals = [v for f in complete
                       if (v := _metric_value(f, attr, failure_only))
                       is not None]
        cens_vals = []
        for f in censored:
            val = _metric_value(f, attr, failure_only)
            if val is not None:
                cens_vals.append(val)
        source_ids = tuple(f.switch_id for f in complete
                           if _metric_value(f, attr, failure_only)
                           is not None)
        transfer = None
        if len(direct_vals) < SUFFICIENT_N and pool:
            vals = list(direct_vals)
            ids = list(source_ids)
            seen = set(ids)
            # 用迁移样本补足到门槛，记录判定（取池中主导关系）
            for _prio, verdict, fact in pool:
                if len(vals) >= SUFFICIENT_N:
                    break
                if fact.switch_id in seen:
                    continue
                val = _metric_value(fact, attr, failure_only)
                if val is None:
                    continue
                vals.append(val)
                ids.append(fact.switch_id)
                seen.add(fact.switch_id)
                if transfer is None or (
                        verdict.uncertainty_factor
                        > transfer.uncertainty_factor):
                    transfer = verdict
            if vals:
                fallback_used = True
                dist = distribution(vals)
            else:
                dist = distribution([])
            source_ids = tuple(ids)
        else:
            dist = distribution(direct_vals)

        metrics[metric] = MetricBlock(
            metric=metric, label=label, dist=dist,
            censored_observed=tuple(cens_vals),
            source_switch_ids=source_ids, transfer=transfer,
        )

    sufficient = len(complete) >= SUFFICIENT_N
    # 首件失败率：直接完整样本 + 实际进入分布的迁移样本
    rate_pool = list(complete)
    if not sufficient:
        seen = {f.switch_id for f in rate_pool}
        for _prio, _verdict, fact in pool:
            if fact.switch_id not in seen:
                rate_pool.append(fact)
                seen.add(fact.switch_id)
    failure_rate = (
        round(sum(1 for f in rate_pool if f.first_article_failed)
              / len(rate_pool), 3)
        if rate_pool else 0.0)
    if sufficient:
        note = f"直接完整样本 {len(complete)} 条，达到充分门槛 {SUFFICIENT_N}"
    elif complete:
        note = (f"直接完整样本仅 {len(complete)} 条（门槛 {SUFFICIENT_N}），"
                f"另有 {len(censored)} 条截尾；"
                + ("已用可迁移样本补充分布" if pool else
                   "且无可用迁移来源，估计仅供参考"))
    else:
        note = (f"无直接完整样本，{len(censored)} 条截尾；"
                + ("分布完全来自迁移样本" if pool else "无法给出可靠估计"))

    return StandardEstimate(
        previous=pair[0], next=pair[1], factory=factory, spec=spec,
        metrics=metrics,
        direct_complete_n=len(complete),
        censored_n=len(censored),
        sufficient=sufficient,
        sufficiency_note=note,
        fallback_used=fallback_used,
        first_article_failure_rate=failure_rate,
        blocked_reasons=blocked,
    )


def build_all_estimates(pack: DataPack,
                        facts: dict[str, ChangeoverFact]) -> list[StandardEstimate]:
    """为事实库中出现的每个 有向对 × 工厂 × 版本 构建估计。"""
    keys = sorted({(f.pair, f.factory, f.spec) for f in facts.values()})
    return [build_estimate(pack, facts, pair, factory, spec)
            for pair, factory, spec in keys]


def relative_uncertainty(est: StandardEstimate, metric: str) -> float | None:
    """相对不确定性（p90-p10)/(2*均值)，供台账与审批展示。"""
    band = est.uncertainty_band(metric)
    mean = est.point_estimate(metric)
    if band is None or not mean:
        return None
    return round((band["p90"] - band["p10"]) / (2 * mean), 3)
