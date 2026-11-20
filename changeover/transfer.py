"""历史经验迁移判定。

计划口径：

* 同厂家族同方向的历史样本是「直接样本」；
* 跨厂借用要求两厂在 ``factory_caps`` 中互为可替代厂，且产线工具/资质
  口径不冲突，得到「跨厂样本」；
* 跨厂家族（不同产品族之间）只能在 **材料 chemistry、洁净等级
  cleanliness、模具方向 die_direction 三者全部一致** 时借用；
* 任一属性变化都要给出阻断原因，不能静默迁移。
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import DataPack, Product

# 三个迁移门槛属性，顺序即报告中的展示顺序
GATE_ATTRIBUTES = ("chemistry", "cleanliness", "die_direction")
GATE_LABELS = {
    "chemistry": "材料",
    "cleanliness": "洁净等级",
    "die_direction": "模具方向",
}


@dataclass(frozen=True)
class TransferVerdict:
    usable: bool
    relation: str          # direct / cross_factory / cross_family / blocked
    reasons: tuple[str, ...]
    uncertainty_factor: float  # 迁移带来的不确定性放大系数

    def to_dict(self) -> dict:
        return {
            "usable": self.usable,
            "relation": self.relation,
            "reasons": list(self.reasons),
            "uncertainty_factor": self.uncertainty_factor,
        }


def _factories_interchangeable(pack: DataPack, source_factory: str,
                               target_factory: str) -> bool:
    if source_factory == target_factory:
        return True
    for cap in pack.capabilities:
        if (cap.factory == source_factory
                and cap.interchangeable_factory == target_factory):
            return True
        if (cap.factory == target_factory
                and cap.interchangeable_factory == source_factory):
            return True
    return False


def _line_compatible(pack: DataPack, source: tuple[str, str],
                     target: tuple[str, str]) -> tuple[bool, tuple[str, ...]]:
    """跨厂借用时检查工具与技师资质口径。"""
    reasons: list[str] = []
    src_cap = pack.capability_index.get(source)
    dst_cap = pack.capability_index.get(target)
    if src_cap is None or dst_cap is None:
        return False, ("目标或来源产线无产能档案",)
    if not set(src_cap.tools) & set(dst_cap.tools):
        reasons.append(
            f"共用工具不相交：来源 {src_cap.tools} vs 目标 {dst_cap.tools}")
    if not set(src_cap.qualified_workers) & set(dst_cap.qualified_workers):
        reasons.append(
            "合格技师不相交：跨厂换型无共同资质技师")
    return (not reasons), tuple(reasons)


def judge_transfer(pack: DataPack,
                   target_pair: tuple[str, str],
                   target_site: tuple[str, str],
                   source_pair: tuple[str, str],
                   source_site: tuple[str, str]) -> TransferVerdict:
    """判断一条来源经验能否迁移到目标换型。

    ``target_pair`` / ``source_pair`` 为 (previous_family, next_family)，
    ``*_site`` 为 (factory, line)。
    """
    target_prev, target_next = target_pair
    source_prev, source_next = source_pair
    target_factory, target_line = target_site
    source_factory, source_line = source_site

    same_pair = source_pair == target_pair
    same_factory = source_factory == target_factory

    # —— 直接样本 ——
    if same_pair and same_factory:
        return TransferVerdict(True, "direct", (), 1.0)

    # —— 跨厂同方向 ——
    if same_pair:
        if not _factories_interchangeable(pack, source_factory, target_factory):
            return TransferVerdict(
                False, "blocked",
                (f"{source_factory} 与 {target_factory} 未建立可替代厂关系",),
                1.0)
        ok, reasons = _line_compatible(pack, source_site, target_site)
        if not ok:
            return TransferVerdict(False, "blocked", reasons, 1.0)
        return TransferVerdict(
            True, "cross_factory",
            (f"跨厂借用：{source_factory}{source_line} → "
             f"{target_factory}{target_line}，厂际可替代且工具/资质相容",),
            1.15)

    # —— 跨厂家族（含同厂不同族）：三道属性门槛 ——
    products = pack.product_index
    missing = [f for f in (target_prev, target_next, source_prev, source_next)
               if f not in products]
    if missing:
        return TransferVerdict(
            False, "blocked",
            (f"产品族缺工艺档案：{sorted(set(missing))}",), 1.0)

    # 方向必须逐一对应（prev 对 prev、next 对 next）
    reasons: list[str] = []
    for role, src_family, dst_family in (
        ("前批", source_prev, target_prev),
        ("后批", source_next, target_next),
    ):
        src_p: Product = products[src_family]
        dst_p: Product = products[dst_family]
        for attr in GATE_ATTRIBUTES:
            if getattr(src_p, attr) != getattr(dst_p, attr):
                reasons.append(
                    f"{role}产品族 {src_family}→{dst_family} 的"
                    f"{GATE_LABELS[attr]}变化："
                    f"{getattr(src_p, attr)} → {getattr(dst_p, attr)}")

    if reasons:
        return TransferVerdict(False, "blocked", tuple(reasons), 1.0)

    # 属性全等但族不同：还要过厂际/产线相容性
    if not same_factory:
        if not _factories_interchangeable(pack, source_factory, target_factory):
            return TransferVerdict(
                False, "blocked",
                (f"{source_factory} 与 {target_factory} 未建立可替代厂关系",),
                1.0)
        ok, line_reasons = _line_compatible(pack, source_site, target_site)
        if not ok:
            return TransferVerdict(False, "blocked", line_reasons, 1.0)
        relation = "cross_family"
        note = (f"跨厂跨族借用：三属性一致，{source_factory} → {target_factory}",)
        factor = 1.3
    else:
        relation = "cross_family"
        note = ("同厂跨族借用：材料/洁净等级/模具方向三属性一致，"
                "仅产品族不同",)
        factor = 1.2

    return TransferVerdict(True, relation, note, factor)


def transferable_sources(pack: DataPack,
                         target_pair: tuple[str, str],
                         target_site: tuple[str, str],
                         facts) -> list[tuple[str, TransferVerdict]]:
    """列出事实库中每条换型对目标的迁移判定。"""
    out = []
    for switch_id, fact in facts.items():
        verdict = judge_transfer(
            pack, target_pair, target_site,
            fact.pair, (fact.factory, fact.line))
        if verdict.usable:
            out.append((switch_id, verdict))
    return out
