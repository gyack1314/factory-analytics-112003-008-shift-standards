"""工时标准台账：版本、审批、缺口补采与拒用条件。

台账状态持久化到 JSON（默认 state/standards_state.json），输入文件
``standards_ledger.json`` 作为初始台账导入，之后的批准/退回动作都在
本模块留痕，不回写原始资料。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

from .estimator import SUFFICIENT_N, StandardEstimate, relative_uncertainty

ACTION_APPROVE = "批准"
ACTION_REJECT = "退回补采"


@dataclass
class StandardVersion:
    id: str
    previous: str
    next: str
    spec: str
    effective_min: int
    action: str
    factory: str
    approver: str | None = None
    decided_min: int | None = None
    note: str = ""

    def matches(self, pair, factory, spec) -> bool:
        return (self.previous, self.next) == pair and self.factory == factory \
            and self.spec == spec


@dataclass
class GapRequest:
    """工艺负责人要求补采的样本缺口。"""
    id: str
    previous: str
    next: str
    factory: str
    spec: str
    reason: str
    requested_min: int
    requested_by: str
    status: str = "开放"          # 开放 / 已补采 / 关闭
    filled_switch_ids: list[str] = field(default_factory=list)


@dataclass
class RejectionCondition:
    """标准暂时拒用的条件（命中任一即不得用于排产）。"""
    id: str
    previous: str
    next: str
    factory: str
    spec: str
    condition: str
    raised_min: int
    raised_by: str
    active: bool = True


class StandardsLibrary:
    def __init__(self, versions: list[StandardVersion],
                 gaps: list[GapRequest],
                 rejections: list[RejectionCondition],
                 change_log: list[dict]):
        self.versions = versions
        self.gaps = gaps
        self.rejections = rejections
        self.change_log = change_log

    # ---------- 持久化 ----------
    def save(self, path: str | Path) -> None:
        payload = {
            "versions": [asdict(v) for v in self.versions],
            "gaps": [asdict(g) for g in self.gaps],
            "rejections": [asdict(r) for r in self.rejections],
            "change_log": self.change_log,
        }
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "StandardsLibrary":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            versions=[StandardVersion(**v) for v in data["versions"]],
            gaps=[GapRequest(**g) for g in data["gaps"]],
            rejections=[RejectionCondition(**r)
                        for r in data.get("rejections", [])],
            change_log=data.get("change_log", []),
        )

    @classmethod
    def from_input_ledger(cls, rows: list[dict]) -> "StandardsLibrary":
        versions = [
            StandardVersion(
                id=r["id"], previous=r["previous"], next=r["next"],
                spec=r["spec"], effective_min=r["effective_min"],
                action=r["action"], factory=r["factory"],
                note="由输入台账导入")
            for r in rows
        ]
        return cls(versions, [], [], [])

    def _log(self, event: str, payload: dict) -> None:
        self.change_log.append({
            "event": event,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **payload,
        })

    # ---------- 查询 ----------
    def versions_for(self, pair, factory) -> list[StandardVersion]:
        return sorted(
            (v for v in self.versions
             if (v.previous, v.next) == pair and v.factory == factory),
            key=lambda v: v.effective_min)

    def active_version(self, pair, factory, at_min: int) -> StandardVersion | None:
        """时刻 at_min 生效的版本：已到生效点且最新。"""
        candidates = [v for v in self.versions_for(pair, factory)
                      if v.effective_min <= at_min]
        return candidates[-1] if candidates else None

    def is_approved(self, pair, factory, spec) -> bool:
        return any(v.action == ACTION_APPROVE
                   and v.matches(pair, factory, spec)
                   for v in self.versions)

    def active_rejections(self, pair, factory, spec) -> list[RejectionCondition]:
        return [r for r in self.rejections if r.active
                and (r.previous, r.next) == pair
                and r.factory == factory and r.spec == spec]

    def usable_for_scheduling(self, pair, factory, spec,
                              at_min: int) -> tuple[bool, list[str]]:
        """综合生效、审批、拒用条件给出可否排产。"""
        reasons = []
        active = self.active_version(pair, factory, at_min)
        if active is None:
            reasons.append("无已生效版本")
        elif active.spec != spec:
            reasons.append(
                f"请求版本 {spec} 与当前生效版本 {active.spec} 不一致")
        if not self.is_approved(pair, factory, spec):
            reasons.append(f"版本 {spec} 未获工艺负责人批准")
        reasons.extend(r.condition
                       for r in self.active_rejections(pair, factory, spec))
        return (not reasons), reasons

    # ---------- 工艺负责人动作 ----------
    def approve(self, pair, factory, spec, effective_min, approver,
                estimate: StandardEstimate | None, note="") -> StandardVersion:
        """批准一项标准给排产使用；证据不足时要求在备注中明示。"""
        if estimate is not None and not estimate.sufficient:
            note = (note + "；" if note else "") + (
                f"证据不足（直接完整样本 {estimate.direct_complete_n}"
                f"/{SUFFICIENT_N}，截尾 {estimate.censored_n}），"
                "批准仅限观察使用")
        new_id = f"STD-{pair[0]}{pair[1]}-{spec}-{len(self.versions) + 1}"
        version = StandardVersion(
            id=new_id, previous=pair[0], next=pair[1], spec=spec,
            effective_min=effective_min, action=ACTION_APPROVE,
            factory=factory, approver=approver,
            decided_min=effective_min, note=note)
        self.versions.append(version)
        self._log("approve", {"id": new_id, "pair": list(pair),
                              "factory": factory, "spec": spec,
                              "approver": approver, "note": note})
        return version

    def request_gap(self, pair, factory, spec, reason, requested_min,
                    requested_by) -> GapRequest:
        gap_id = f"GAP-{pair[0]}{pair[1]}-{len(self.gaps) + 1}"
        gap = GapRequest(
            id=gap_id, previous=pair[0], next=pair[1], factory=factory,
            spec=spec, reason=reason, requested_min=requested_min,
            requested_by=requested_by)
        self.gaps.append(gap)
        self._log("request_gap", asdict(gap))
        return gap

    def raise_rejection(self, pair, factory, spec, condition, raised_min,
                        raised_by) -> RejectionCondition:
        rid = f"REJ-{pair[0]}{pair[1]}-{len(self.rejections) + 1}"
        rec = RejectionCondition(
            id=rid, previous=pair[0], next=pair[1], factory=factory,
            spec=spec, condition=condition, raised_min=raised_min,
            raised_by=raised_by)
        self.rejections.append(rec)
        self._log("raise_rejection", asdict(rec))
        return rec

    def resolve_rejection(self, rid: str) -> None:
        for rec in self.rejections:
            if rec.id == rid:
                rec.active = False
                self._log("resolve_rejection", {"id": rid})

    # ---------- 改版原因追溯 ----------
    def revision_reasons(self, pair, factory) -> list[dict]:
        """某项工时标准改版后安排改变的直接原因链。"""
        out = []
        for v in self.versions_for(pair, factory):
            related_gaps = [g for g in self.gaps
                            if (g.previous, g.next) == pair
                            and g.factory == factory
                            and g.requested_min <= v.effective_min]
            out.append({
                "version_id": v.id,
                "spec": v.spec,
                "effective_min": v.effective_min,
                "action": v.action,
                "approver": v.approver,
                "note": v.note,
                "triggered_gaps": [g.id for g in related_gaps],
                "active_rejection_conditions": [
                    r.condition for r in self.rejections
                    if (r.previous, r.next) == pair and r.factory == factory
                    and r.spec == v.spec and r.active],
            })
        return out

    def evidence_summary(self, estimate: StandardEstimate) -> dict:
        """计划员查询用：不确定性、适用版本、样本充分性。"""
        return {
            "pair": [estimate.previous, estimate.next],
            "factory": estimate.factory,
            "spec": estimate.spec,
            "sufficient": estimate.sufficient,
            "sufficiency_note": estimate.sufficiency_note,
            "direct_complete_n": estimate.direct_complete_n,
            "censored_n": estimate.censored_n,
            "fallback_used": estimate.fallback_used,
            "relative_uncertainty": {
                m: relative_uncertainty(estimate, m)
                for m in estimate.metrics},
            "approved": self.is_approved(
                (estimate.previous, estimate.next),
                estimate.factory, estimate.spec),
            "rejections": [r.condition for r in self.active_rejections(
                (estimate.previous, estimate.next),
                estimate.factory, estimate.spec)],
        }
