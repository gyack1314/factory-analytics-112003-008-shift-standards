"""命令行入口：计划员/工艺负责人的查询与操作界面。

所有结果默认打印 JSON（UTF-8、不转义中文），可重定向到 output/ 留存。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .app import Application
from .models import DataPack
from .reconciliation import reconcile_shift
from .resources import worker_utilization
from .scheduling import Scheduler
from .transfer import GATE_LABELS, judge_transfer
from .versions import PlanVersionStore, assess_replan

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "data"
DEFAULT_STATE = ROOT / "state" / "standards_state.json"
DEFAULT_PLAN_STORE = ROOT / "state" / "plan_versions.json"


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=list))


def _pair(text: str) -> tuple[str, str]:
    parts = text.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("方向格式应为 前批,后批，例如 A,B")
    return parts[0].strip(), parts[1].strip()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="changeover",
        description="多品种小线换型工时标准与班次承诺重算")
    p.add_argument("--data", default=str(DEFAULT_DATA))
    p.add_argument("--state", default=str(DEFAULT_STATE))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("quality", help="输入资料体检（重复/矛盾/截尾）")

    e = sub.add_parser("estimate", help="查询某有向切换的工时分布估计")
    e.add_argument("--pair", required=True, type=_pair)
    e.add_argument("--factory", required=True)
    e.add_argument("--spec", default=None)

    sub.add_parser("estimates", help="全部方向的标准估计概览")

    t = sub.add_parser("transfer", help="历史经验迁移判定")
    t.add_argument("--target-pair", required=True, type=_pair)
    t.add_argument("--target-site", required=True,
                   help="目标 厂,线，例如 一厂,L0")
    t.add_argument("--source-pair", required=True, type=_pair)
    t.add_argument("--source-site", required=True, type=lambda s: tuple(s.split(",")))

    sub.add_parser("gaps", help="样本充分性与补采缺口清单")

    a = sub.add_parser("approve", help="工艺负责人批准标准")
    a.add_argument("--pair", required=True, type=_pair)
    a.add_argument("--factory", required=True)
    a.add_argument("--spec", required=True)
    a.add_argument("--effective-min", required=True, type=int)
    a.add_argument("--by", required=True)
    a.add_argument("--note", default="")

    g = sub.add_parser("request-gap", help="工艺负责人要求补采缺口")
    g.add_argument("--pair", required=True, type=_pair)
    g.add_argument("--factory", required=True)
    g.add_argument("--spec", required=True)
    g.add_argument("--reason", required=True)
    g.add_argument("--at-min", required=True, type=int)
    g.add_argument("--by", required=True)

    rj = sub.add_parser("reject", help="登记标准暂时拒用条件")
    rj.add_argument("--pair", required=True, type=_pair)
    rj.add_argument("--factory", required=True)
    rj.add_argument("--spec", required=True)
    rj.add_argument("--condition", required=True)
    rj.add_argument("--at-min", required=True, type=int)
    rj.add_argument("--by", required=True)

    lv = sub.add_parser("revisions", help="标准改版原因与拒用条件追溯")
    lv.add_argument("--pair", required=True, type=_pair)
    lv.add_argument("--factory", required=True)

    live = sub.add_parser("live", help="执行中换型剩余时间评估")
    live.add_argument("--switch", default="LIVE")
    live.add_argument("--review-min", required=True, type=int)

    cal = sub.add_parser("calendar", help="复核时点资源占用与争抢")
    cal.add_argument("--review-min", required=True, type=int)

    pl = sub.add_parser("plan", help="构建基线排产方案")
    pl.add_argument("--review-min", required=True, type=int)
    pl.add_argument("--name", default="baseline")

    cmp_ = sub.add_parser("compare", help="对比顺序/提前准备/共用工具方案")
    cmp_.add_argument("--review-min", required=True, type=int)

    rp = sub.add_parser("replan", help="锁定交期订单重排评估")
    rp.add_argument("--review-min", required=True, type=int)
    rp.add_argument("--confirm", action="store_true",
                    help="确认后保留新版本（原版本不动）")
    rp.add_argument("--by", default="计划员")

    rc = sub.add_parser("reconcile", help="历史班次计划偏差复核")
    rc.add_argument("--from-min", type=int, default=None)
    rc.add_argument("--to-min", type=int, default=None)

    sub.add_parser("workers", help="技师人工与重复计入统计")
    return p


# ---------------------------------------------------------------------------
def cmd_quality(app: Application, args) -> None:
    _print(app.data_quality_report())


def cmd_estimate(app: Application, args) -> None:
    est = app.estimate_for(args.pair, args.factory, args.spec)
    out = est.to_dict()
    out["ledger_evidence"] = app.library.evidence_summary(est)
    _print(out)


def cmd_estimates(app: Application, args) -> None:
    rows = []
    for est in app.estimates_list:
        rows.append({
            "pair": [est.previous, est.next],
            "factory": est.factory, "spec": est.spec,
            "sufficient": est.sufficient,
            "direct_complete_n": est.direct_complete_n,
            "censored_n": est.censored_n,
            "fallback_used": est.fallback_used,
            "line_occupied_mean": est.point_estimate("line_occupied"),
            "line_band": est.uncertainty_band("line_occupied"),
            "failure_rate": est.first_article_failure_rate,
            "approved": app.library.is_approved(
                (est.previous, est.next), est.factory, est.spec),
        })
    _print(rows)


def cmd_transfer(app: Application, args) -> None:
    target_site = tuple(args.target_site.split(","))
    verdict = judge_transfer(
        app.pack, args.target_pair, target_site,
        args.source_pair, args.source_site)
    _print({
        "target_pair": list(args.target_pair),
        "source_pair": list(args.source_pair),
        "gate_attributes": list(GATE_LABELS.values()),
        "verdict": verdict.to_dict(),
    })


def cmd_gaps(app: Application, args) -> None:
    out = []
    for est in app.estimates_list:
        if not est.sufficient or est.fallback_used or est.blocked_reasons:
            out.append({
                "pair": [est.previous, est.next],
                "factory": est.factory, "spec": est.spec,
                "direct_complete_n": est.direct_complete_n,
                "censored_n": est.censored_n,
                "note": est.sufficiency_note,
                "blocked_reasons": est.blocked_reasons,
                "ledger_gap_requests": [
                    vars(g)
                    for g in app.library.gaps
                    if (g.previous, g.next) == (est.previous, est.next)
                    and g.factory == est.factory],
            })
    # 订单需要、但历史完全无方向样本且迁移被门槛阻断的方向
    known_pairs = {(e.previous, e.next) for e in app.estimates_list}
    order_directions = set()
    last_by_line: dict[str, str] = {}
    for fact in app.facts.values():
        if fact.censored:
            last_by_line[fact.line] = fact.previous
    for o in sorted(app.pack.orders, key=lambda x: x.original_start_min):
        prev = last_by_line.get(o.original_line)
        if prev is not None:
            order_directions.add((prev, o.product))
            last_by_line[o.original_line] = o.product
    uncovered = []
    for pair in sorted(order_directions - known_pairs):
        blocked = []
        for fact in app.facts.values():
            if fact.censored:
                continue
            v = judge_transfer(
                app.pack, pair,
                (fact.factory, fact.line), fact.pair,
                (fact.factory, fact.line))
            if not v.usable:
                blocked.append(v.reasons)
        sample_reasons = sorted({r for rs in blocked for r in rs})[:4]
        uncovered.append({
            "pair": list(pair),
            "status": "无直接样本且迁移被阻断，需要补采",
            "blocking_attributes": sample_reasons,
        })
    _print({"insufficient_estimates": out,
            "uncovered_directions": uncovered})


def _persist(app: Application) -> None:
    Path(DEFAULT_STATE).parent.mkdir(parents=True, exist_ok=True)
    app.save_library()


def cmd_approve(app: Application, args) -> None:
    est = app.estimate_for(args.pair, args.factory, args.spec)
    v = app.library.approve(args.pair, args.factory, args.spec,
                            args.effective_min, args.by, est, args.note)
    _persist(app)
    _print({"approved": vars(v), "evidence": app.library.evidence_summary(est)})


def cmd_request_gap(app: Application, args) -> None:
    gap = app.library.request_gap(
        args.pair, args.factory, args.spec, args.reason,
        args.at_min, args.by)
    _persist(app)
    _print(vars(gap))


def cmd_reject(app: Application, args) -> None:
    rec = app.library.raise_rejection(
        args.pair, args.factory, args.spec, args.condition,
        args.at_min, args.by)
    _persist(app)
    _print(vars(rec))


def cmd_revisions(app: Application, args) -> None:
    _print({
        "pair": list(args.pair), "factory": args.factory,
        "revision_chain": app.library.revision_reasons(
            args.pair, args.factory)})


def cmd_live(app: Application, args) -> None:
    _print(app.live_switch(args.switch, args.review_min).to_dict())


def cmd_calendar(app: Application, args) -> None:
    cal = app.calendar_at(args.review_min)
    # tool_logs 中还包含针对待开工订单（PLAN*）的预占用记录，一并入历
    for log in app.pack.tool_logs:
        if log.switch.startswith("PLAN") and log.end_min > args.review_min:
            cal.add(log.tool, "tool",
                    (max(log.start_min, args.review_min), log.end_min),
                    log.switch, f"计划预占用 @ {log.line}")
    _print({
        "review_min": args.review_min,
        "contention": cal.contention_report(args.review_min, app.pack),
        "all_occupancies": [
            {"resource": o.resource, "kind": o.kind,
             "interval": list(o.interval), "owner": o.owner,
             "note": o.note, "hold": o.hold}
            for o in cal.occupancies],
    })


def _baseline_assignments(pack: DataPack):
    orders = sorted(pack.orders, key=lambda o: o.original_start_min)
    assignments = []
    for o in orders:
        caps = [c for c in pack.capabilities if c.line == o.original_line]
        factory = caps[0].factory if caps else ""
        assignments.append((o, o.original_line, factory))
    return assignments


def _live_context(app: Application, review_min: int) -> dict:
    """从执行中换型推导排产起点上下文。"""
    initial_products: dict[str, str] = {}
    start_after: dict[str, int] = {}
    for fact in app.facts.values():
        if not fact.censored:
            continue
        # 该线在制的是前批产品；下一张订单的换型方向 previous=前批
        initial_products[fact.line] = fact.previous
        assessment = app.live_switch(fact.switch_id, review_min)
        if assessment.hold_until_min:
            start_after[fact.line] = max(
                start_after.get(fact.line, review_min),
                assessment.hold_until_min)
    return {"initial_products": initial_products,
            "start_after": start_after}


def _seed_plan_tool_holds(app: Application, scheduler: Scheduler,
                          review_min: int) -> None:
    """tool_logs 中针对待开工订单的预占用进入排产日历。"""
    for log in app.pack.tool_logs:
        if log.switch.startswith("PLAN") and log.end_min > review_min:
            scheduler.calendar.add(
                log.tool, "tool",
                (max(log.start_min, review_min), log.end_min),
                log.switch, f"计划预占用 @ {log.line}")


def cmd_plan(app: Application, args) -> None:
    scheduler = app.scheduler(args.review_min)
    _seed_plan_tool_holds(app, scheduler, args.review_min)
    ctx = _live_context(app, args.review_min)
    plan = scheduler.build_scenario(
        args.name, "按原始产线与原始开始顺序的基线方案",
        _baseline_assignments(app.pack),
        start_after=ctx["start_after"],
        initial_products=ctx["initial_products"])
    _print(plan.to_dict())


def cmd_compare(app: Application, args) -> None:
    pack = app.pack
    scheduler = app.scheduler(args.review_min)
    _seed_plan_tool_holds(app, scheduler, args.review_min)
    ctx = _live_context(app, args.review_min)
    common = dict(start_after=ctx["start_after"],
                  initial_products=ctx["initial_products"])
    base_assign = _baseline_assignments(pack)

    baseline = scheduler.build_scenario(
        "baseline", "基线：原始顺序、允许提前准备", base_assign, **common)
    no_prep = scheduler.build_scenario(
        "no_early_prep", "禁止提前准备：预准备全部压到停线后",
        base_assign, prep_early=False, **common)

    # 顺序枚举，挑违约最少、再挑占线最短的
    perms = scheduler.enumerate_orders(
        pack.orders, start_after=ctx["start_after"],
        initial_products=ctx["initial_products"])
    best_line = min(perms, key=lambda p: (
        len(p.promise_breaches), p.total_line_minutes))
    best_line.name = "best_sequence"
    best_line.description = (
        f"顺序优化（24 种排列中最优）：{best_line.description}")

    # 增加共用工具：给 L1 增配 M1（数据中 L1 仅 M0，争抢源在 M0）
    extra_tools = scheduler.build_scenario(
        "extra_shared_tool", "给 L1 增配共用工具 M1",
        base_assign, extra_tools={"L1": ["M1"]}, **common)

    def summary(plan):
        m = plan.metrics()
        m["delta_line_vs_baseline"] = (
            plan.total_line_minutes - baseline.total_line_minutes)
        m["delta_overlap_prep_vs_baseline"] = (
            plan.total_overlap_prep_min
            - baseline.total_overlap_prep_min)
        return m

    _print({
        "review_min": args.review_min,
        "scenarios": [summary(baseline), summary(no_prep),
                      summary(best_line), summary(extra_tools)],
        "interpretation": (
            "delta_line 为真正减少的占线时间；overlap_prep 只是前移到"
            "前一班的人工时间，不改变产线占用"),
        "best_sequence_detail": best_line.to_dict(),
    })


def cmd_replan(app: Application, args) -> None:
    pack = app.pack
    review = args.review_min
    scheduler = app.scheduler(review)
    _seed_plan_tool_holds(app, scheduler, review)
    ctx = _live_context(app, review)
    current = scheduler.build_scenario(
        "current", "当前在执行版本的计划", _baseline_assignments(pack),
        start_after=ctx["start_after"],
        initial_products=ctx["initial_products"])

    # 已开工订单：开始时刻不晚于复核时点；同线后续未开工订单仍可重排，
    # 但会通过 start_after 的 hold 时刻受到约束
    started = {o.id for o in pack.orders
               if o.original_start_min <= review}
    # 重排思路：把未开工订单按允诺紧迫度排序，保留原线归属
    reopen = [o for o in pack.orders if o.id not in started]
    reopen.sort(key=lambda o: o.promise_min)
    new_assign = []
    for o in reopen:
        caps = [c for c in pack.capabilities if c.line == o.original_line]
        new_assign.append((o, o.original_line,
                           caps[0].factory if caps else ""))
    result = assess_replan(
        pack, scheduler, current, new_assign, review, started,
        "replan-candidate", "未开工锁定订单重排评估",
        build_kwargs={"start_after": ctx["start_after"],
                      "initial_products": ctx["initial_products"]})

    store = PlanVersionStore(DEFAULT_PLAN_STORE)
    plan_obj = result.pop("_plan")
    if args.confirm and result["feasible"]:
        parent = store.latest()
        v = store.commit(
            plan_obj, created_min=review, created_by=args.by,
            basis_spec="当前生效标准",
            responsible_shift=result["responsible_shift"],
            parent=parent, note=result["note"], confirmed=True)
        result["committed_version"] = v.to_dict()
        result["parent_retained"] = parent.version_id if parent else None
    _print(result)


def cmd_reconcile(app: Application, args) -> None:
    window = None
    if args.from_min is not None and args.to_min is not None:
        window = (args.from_min, args.to_min)
    reports = reconcile_shift(app.facts, shift_window=window)
    totals = {
        "reconciled": len(reports),
        "line_delta_sum": sum(r.line_delta_min or 0 for r in reports),
        "double_counted_sum": sum(r.double_counted_min for r in reports),
        "overlap_prep_sum": sum(r.overlap_prep_actual_min for r in reports),
        "equipment_wait_sum": sum(r.equipment_wait_actual_min
                                  for r in reports),
        "reinspect_sum": sum(r.reinspect_actual_min for r in reports),
    }
    _print({"totals": totals,
            "reports": [r.to_dict() for r in reports]})


def cmd_workers(app: Application, args) -> None:
    _print(worker_utilization(app.pack, app.facts))


HANDLERS = {
    "quality": cmd_quality,
    "estimate": cmd_estimate,
    "estimates": cmd_estimates,
    "transfer": cmd_transfer,
    "gaps": cmd_gaps,
    "approve": cmd_approve,
    "request-gap": cmd_request_gap,
    "reject": cmd_reject,
    "revisions": cmd_revisions,
    "live": cmd_live,
    "calendar": cmd_calendar,
    "plan": cmd_plan,
    "compare": cmd_compare,
    "replan": cmd_replan,
    "reconcile": cmd_reconcile,
    "workers": cmd_workers,
}


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    app = Application(args.data, args.state)
    HANDLERS[args.cmd](app, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
