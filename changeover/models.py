"""换型工时重算应用的领域模型与数据装载。

所有时间均为相对分钟（整数），区间右端不包含，与输入资料口径一致。
本模块只做结构校验与空值处理，不做业务推断——推断放在 analysis / estimator。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 原始记录（字段与 data/ 下 JSON 一一对应，保留 null）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SwitchRecord:
    id: str
    factory: str
    line: str
    previous: str
    next: str
    spec: str
    stop_min: int
    restart_min: Optional[int]
    signed_min: Optional[int]

    @property
    def is_censored(self) -> bool:
        """换型尚未结束：没有量产重启证据。"""
        return self.restart_min is None

    @property
    def pair(self) -> tuple[str, str]:
        return self.previous, self.next


@dataclass(frozen=True)
class StageRecord:
    switch: str
    stage: str
    start_min: int
    end_min: Optional[int]
    result: str
    worker: Optional[str]

    @property
    def is_open(self) -> bool:
        """工序仍在进行（end_min 缺失）。"""
        return self.end_min is None


@dataclass(frozen=True)
class ToolLog:
    tool: str
    worker: str
    line: str
    start_min: int
    end_min: int
    switch: str


@dataclass(frozen=True)
class Product:
    family: str
    chemistry: str
    cleanliness: str
    die_direction: str


@dataclass(frozen=True)
class LineCapability:
    factory: str
    line: str
    interchangeable_factory: Optional[str]
    tools: tuple[str, ...]
    qualified_workers: tuple[str, ...]
    rate_per_hour: int


@dataclass(frozen=True)
class Order:
    id: str
    product: str
    qty: int
    release_min: int
    promise_min: int
    locked: bool
    original_line: str
    original_start_min: int


@dataclass(frozen=True)
class LedgerEntry:
    id: str
    previous: str
    next: str
    spec: str
    effective_min: int
    action: str  # 批准 / 退回补采
    factory: str


@dataclass(frozen=True)
class PlanningConfig:
    review_min: tuple[int, ...]
    shift_boundaries_min: tuple[int, ...]
    reopen_margin_min: int
    missing_stage_time_limit_min: int


# ---------------------------------------------------------------------------
# 装载
# ---------------------------------------------------------------------------

REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "switchovers": ("id", "factory", "line", "previous", "next", "spec",
                    "stop_min", "restart_min", "signed_min"),
    "stages": ("switch", "stage", "start_min", "end_min", "result", "worker"),
    "tool_logs": ("tool", "worker", "line", "start_min", "end_min", "switch"),
    "products": ("family", "chemistry", "cleanliness", "die_direction"),
    "factory_caps": ("factory", "line", "interchangeable_factory", "tools",
                     "qualified_workers", "rate_per_hour"),
    "orders": ("id", "product", "qty", "release_min", "promise_min", "locked",
               "original_line", "original_start_min"),
    "standards_ledger": ("id", "previous", "next", "spec", "effective_min",
                         "action", "factory"),
    "planning": ("review_min", "shift_boundaries_min", "reopen_margin_min",
                 "missing_stage_time_limit_min"),
}


class DataValidationError(ValueError):
    pass


def _check_fields(table: str, rows: list[dict[str, Any]]) -> None:
    expected = set(REQUIRED_FIELDS[table])
    for i, row in enumerate(rows):
        if set(row) != expected:
            raise DataValidationError(
                f"{table} 第 {i} 行字段不符：期望 {sorted(expected)}，"
                f"实际 {sorted(row)}")


@dataclass
class DataPack:
    switchovers: list[SwitchRecord]
    stages: list[StageRecord]
    tool_logs: list[ToolLog]
    products: list[Product]
    capabilities: list[LineCapability]
    orders: list[Order]
    ledger: list[LedgerEntry]
    planning: PlanningConfig
    switch_index: dict[str, SwitchRecord] = field(default_factory=dict)
    stages_by_switch: dict[str, list[StageRecord]] = field(default_factory=dict)
    product_index: dict[str, Product] = field(default_factory=dict)
    capability_index: dict[tuple[str, str], LineCapability] = field(
        default_factory=dict)

    def stages_of(self, switch_id: str) -> list[StageRecord]:
        return self.stages_by_switch.get(switch_id, [])

    def capability(self, factory: str, line: str) -> LineCapability:
        return self.capability_index[(factory, line)]


def load_pack(data_dir: str | Path) -> DataPack:
    """从数据目录读取并校验全部输入表。"""
    data_dir = Path(data_dir)
    raw: dict[str, Any] = {}
    for path in sorted(data_dir.glob("*.json")):
        raw[path.stem] = json.loads(path.read_text(encoding="utf-8"))

    missing = set(REQUIRED_FIELDS) - set(raw)
    if missing:
        raise DataValidationError(f"缺少数据表：{sorted(missing)}")

    for table in REQUIRED_FIELDS:
        rows = raw[table]
        if table == "planning":
            continue
        _check_fields(table, rows)

    switchovers = [SwitchRecord(**r) for r in raw["switchovers"]]
    stages = [StageRecord(**r) for r in raw["stages"]]
    tool_logs = [ToolLog(**r) for r in raw["tool_logs"]]
    products = [Product(**r) for r in raw["products"]]
    capabilities = [
        LineCapability(
            factory=r["factory"], line=r["line"],
            interchangeable_factory=r["interchangeable_factory"],
            tools=tuple(r["tools"]),
            qualified_workers=tuple(r["qualified_workers"]),
            rate_per_hour=r["rate_per_hour"],
        )
        for r in raw["factory_caps"]
    ]
    orders = [Order(**r) for r in raw["orders"]]
    ledger = [LedgerEntry(**r) for r in raw["standards_ledger"]]
    p = raw["planning"]
    _check_fields("planning", [p])
    planning = PlanningConfig(
        review_min=tuple(p["review_min"]),
        shift_boundaries_min=tuple(p["shift_boundaries_min"]),
        reopen_margin_min=p["reopen_margin_min"],
        missing_stage_time_limit_min=p["missing_stage_time_limit_min"],
    )

    # 跨表引用与区间顺序校验
    switch_index = {s.id: s for s in switchovers}
    if len(switch_index) != len(switchovers):
        raise DataValidationError("switchovers 存在重复 id")
    for stg in stages:
        if stg.switch not in switch_index:
            raise DataValidationError(f"stages 引用未知换型 {stg.switch}")
        if stg.end_min is not None and stg.end_min < stg.start_min:
            raise DataValidationError(f"工序区间倒挂：{stg}")
    for log in tool_logs:
        if log.end_min < log.start_min:
            raise DataValidationError(f"工具借用区间倒挂：{log}")

    stages_by_switch: dict[str, list[StageRecord]] = {}
    for stg in stages:
        stages_by_switch.setdefault(stg.switch, []).append(stg)
    for rows in stages_by_switch.values():
        rows.sort(key=lambda r: (r.start_min, r.end_min or 10**12))

    product_index = {pr.family: pr for pr in products}
    capability_index = {(c.factory, c.line): c for c in capabilities}

    return DataPack(
        switchovers=switchovers,
        stages=stages,
        tool_logs=tool_logs,
        products=products,
        capabilities=capabilities,
        orders=orders,
        ledger=ledger,
        planning=planning,
        switch_index=switch_index,
        stages_by_switch=stages_by_switch,
        product_index=product_index,
        capability_index=capability_index,
    )
