"""计划版本管理与锁定交期重排。

需求口径：

* 尚未开工、客户交期已锁定（``locked=true``）的订单允许重排，但必须指出
  重排是否引发其他交期冲突；
* 计划员确认新的可行承诺后，**保留原版本和责任班次**，另建新版本，
  不覆盖旧计划；
* 已开工（含执行中换型 LIVE 占用的产线）的订单不参与重排。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path

from .models import DataPack, Order
from .scheduling import ScenarioPlan, Scheduler


@dataclass
class PlanVersion:
    version_id: str
    created_min: int
    created_by: str
    basis_spec: str                      # 依据的标准版本
    scenario_name: str
    plan_payload: dict
    parent_version_id: str | None = None
    locked_orders: list[str] = field(default_factory=list)
    responsible_shift: int | None = None
    note: str = ""
    confirmed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class PlanVersionStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.versions: list[PlanVersion] = []
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.versions = [PlanVersion(**v) for v in data.get("versions", [])]

    def save(self) -> None:
        self.path.write_text(
            json.dumps({"versions": [v.to_dict() for v in self.versions]},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")

    def latest(self) -> PlanVersion | None:
        return self.versions[-1] if self.versions else None

    def commit(self, plan: ScenarioPlan, created_min: int, created_by: str,
               basis_spec: str, responsible_shift: int | None,
               parent: PlanVersion | None = None, note="",
               confirmed: bool = False) -> PlanVersion:
        vid = f"PLAN-VER-{len(self.versions) + 1:03d}"
        version = PlanVersion(
            version_id=vid, created_min=created_min, created_by=created_by,
            basis_spec=basis_spec, scenario_name=plan.name,
            plan_payload=plan.to_dict(),
            parent_version_id=parent.version_id if parent else None,
            locked_orders=[s.order.id for s in plan.scheduled
                           if s.order.locked],
            responsible_shift=responsible_shift, note=note,
            confirmed=confirmed)
        self.versions.append(version)
        self.save()
        return version


def _shift_of(t: int, boundaries: tuple[int, ...]) -> int:
    for k in range(len(boundaries) - 1):
        if boundaries[k] <= t < boundaries[k + 1]:
            return k
    return len(boundaries) - 1


def assess_replan(pack: DataPack, scheduler: Scheduler,
                  current_plan: ScenarioPlan,
                  new_assignments: list[tuple[Order, str, str]],
                  review_min: int,
                  started_order_ids: set[str],
                  name: str, description: str,
                  build_kwargs: dict | None = None) -> dict:
    """评估重排方案：交期冲突、锁定订单影响、与原计划的差异。"""
    locked = {o.id: o for o in pack.orders if o.locked}

    # 已开工订单不得重排
    violations = [oid for oid, _line, _f in
                  ((a[0].id, a[1], a[2]) for a in new_assignments)
                  if oid in started_order_ids]
    if violations:
        return {"feasible": False,
                "reason": f"订单已开工，禁止重排：{sorted(violations)}"}

    new_plan = scheduler.build_scenario(
        name, description, new_assignments, **(build_kwargs or {}))

    original_by_order = {s.order.id: s for s in current_plan.scheduled}
    impacts = []
    for s in new_plan.scheduled:
        old = original_by_order.get(s.order.id)
        impact = {
            "order_id": s.order.id,
            "locked": s.order.locked,
            "old_line": old.line if old else None,
            "new_line": s.line,
            "old_run_start": old.run_start_min if old else None,
            "new_run_start": s.run_start_min,
            "old_run_end": old.run_end_min if old else None,
            "new_run_end": s.run_end_min,
            "promise_min": s.order.promise_min,
            "promise_kept": s.promise_kept,
            "knock_on_conflict": False,
            "issues": s.issues,
        }
        if s.order.locked and old and not s.promise_kept:
            impact["knock_on_conflict"] = True
        # 重排挤压到其他锁定订单的允诺
        if old and s.run_end_min > s.order.promise_min:
            impact["knock_on_conflict"] = True
        impacts.append(impact)

    knock_on = [i for i in impacts if i["knock_on_conflict"]]
    feasible = not new_plan.promise_breaches and not knock_on
    responsible_shift = _shift_of(review_min,
                                  pack.planning.shift_boundaries_min)
    return {
        "feasible": feasible,
        "review_min": review_min,
        "responsible_shift": responsible_shift,
        "new_plan_metrics": new_plan.metrics(),
        "old_plan_metrics": current_plan.metrics(),
        "locked_orders": sorted(locked),
        "started_orders_excluded": sorted(started_order_ids),
        "order_impacts": impacts,
        "knock_on_conflicts": knock_on,
        "requires_planner_confirmation": True,
        "note": ("确认新版本前原计划保留；责任班次为复核时点所在班次 "
                 f"{responsible_shift}"),
        "_plan": new_plan,
    }
