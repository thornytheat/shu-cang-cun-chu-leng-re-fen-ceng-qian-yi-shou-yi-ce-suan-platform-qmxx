"""迁移计划：建计划 → 风险复检 → 容量分批排期 → 移除阻断 → 提交 → 导出。

收益口径分两层，避免「选中即按全年算」导致的账面虚高：

1. 试算口径（savings_core.aggregate，计划项上的 save_month / net_save_year 等字段）
   描述每张表「假如迁了」的首年经济性，与试算页完全一致，不随排期变化。
2. 排期口径（scheduling_core.schedule_batches）
   按计划记录的月度容量上限与开工月份分批：单表不可拆分，按每 GB 首年净收益
   从高到低、同值时回收期短者优先装箱；装不下的表标记 unscheduled，单列且不计收益。
   每张表迁完后次月开始产生节省，计划头与批次上的「当年净省」按开工月折算。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants import error_codes as codes
from src.constants.enums import PLAN_STATUS_LABELS, PlanItemStatus, PlanStatus, StorageTier
from src.constants.limits import MAX_PLAN_ITEMS
from src.constants.log_templates import (
    ACTION_PLAN_CREATE,
    ACTION_PLAN_EXPORT,
    ACTION_PLAN_ITEM_ADD,
    ACTION_PLAN_ITEM_REMOVE,
    ACTION_PLAN_RECHECK,
    ACTION_PLAN_SUBMIT,
)
from src.models.migration_plan import MigrationPlan
from src.models.migration_plan_item import MigrationPlanItem
from src.models.table_profile import TableProfile
from src.models.user import User
from src.services import audit_service, cost_service, heat_service, savings_core, scheduling_core
from src.utils.errors import AppError, not_found
from src.utils.ids import parse_uuid
from src.utils.numeric import money


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _next_code(db: Session, moment: datetime) -> str:
    quarter = (moment.month - 1) // 3 + 1
    prefix = f"MP-{moment.year}Q{quarter}-"
    existing = db.execute(
        select(MigrationPlan.code).where(MigrationPlan.code.like(f"{prefix}%"))
    ).scalars().all()
    seq = 1
    for code in existing:
        try:
            seq = max(seq, int(code.rsplit("-", 1)[1]) + 1)
        except (IndexError, ValueError):
            continue
    return f"{prefix}{seq:03d}"


def active_items(db: Session, plan_id) -> list[MigrationPlanItem]:
    """计划内未移除的表（含排不下的 unscheduled 表，它们仍是计划的一部分）。"""
    return list(
        db.execute(
            select(MigrationPlanItem)
            .where(
                MigrationPlanItem.plan_id == plan_id,
                MigrationPlanItem.status != PlanItemStatus.REMOVED,
            )
            .order_by(MigrationPlanItem.save_month.desc())
        ).scalars().all()
    )


def scheduled_items(db: Session, plan_id) -> list[MigrationPlanItem]:
    """已排入批次的计划项，按批次号 + 批次内装箱顺序（每 GB 净收益降序）输出。

    排序在 Python 侧完成，与 scheduling_core 的装箱键完全一致，
    不依赖各数据库 UUID 生成顺序，保证页面与工单 CSV 逐行对齐。
    """
    rows = list(
        db.execute(
            select(MigrationPlanItem).where(
                MigrationPlanItem.plan_id == plan_id,
                MigrationPlanItem.status != PlanItemStatus.REMOVED,
                MigrationPlanItem.schedule_status == scheduling_core.SCHEDULED,
            )
        ).scalars().all()
    )
    rows.sort(key=lambda item: (item.batch_no, scheduling_core.schedule_sort_key(item)))
    return rows


def apply_schedule(db: Session, plan: MigrationPlan, items: list[MigrationPlanItem]) -> dict:
    """按计划头的容量/开工月重算每张表的批次与开工月，并写回计划项。"""
    result = scheduling_core.schedule_batches(
        items,
        float(plan.monthly_capacity_gb),
        int(plan.start_year),
        int(plan.start_month),
    )
    by_id = {item.id: item for item in items}
    for row in result["scheduled"] + result["unscheduled"]:
        item = by_id.get(row["ref"].id) if hasattr(row["ref"], "id") else None
        if item is None:  # 纯函数层回传的是 ORM 对象本身
            item = row["ref"]
        item.schedule_status = row["schedule_status"]
        item.batch_no = row["batch_no"]
        item.start_offset = row["start_offset"]
        item.start_month = row["start_month"]
        item.current_year_net_save = row["current_year_net_save"]
    return result


def recount(db: Session, plan: MigrationPlan) -> dict:
    """汇总写回计划头。

    风险计数覆盖全部未移除表（含排不下的）；金额/体积只统计已排入批次的表，
    排不下的表不计任何收益与成本。
    """
    items = active_items(db, plan.id)
    scheduled = [item for item in items if item.schedule_status == scheduling_core.SCHEDULED]
    summary = savings_core.aggregate(scheduled)

    plan.total_size_gb = summary["size_total_gb"]
    plan.save_month = summary["save_month"]
    plan.save_year = summary["save_year"]
    plan.migration_cost = summary["migration_cost"]
    plan.net_save_year = summary["net_save_year"]
    plan.avg_payback = summary["avg_payback"]
    plan.risk_block_count = sum(1 for item in items if item.risk == "block")
    plan.risk_warn_count = sum(1 for item in items if item.risk == "warn")
    db.flush()
    return {
        "item_count": len(items),
        "scheduled_count": len(scheduled),
        "unscheduled_count": len(items) - len(scheduled),
        "batch_count": len({item.batch_no for item in scheduled}),
        "total_size_gb": plan.total_size_gb,
        "save_month": plan.save_month,
        "save_year": plan.save_year,
        "migration_cost": plan.migration_cost,
        "net_save_year": plan.net_save_year,
        "current_year_net_save": money(
            sum(float(item.current_year_net_save) for item in scheduled)
        ),
        "avg_payback": float(plan.avg_payback) if plan.avg_payback is not None else None,
        "risk_block_count": plan.risk_block_count,
        "risk_warn_count": plan.risk_warn_count,
    }


def batch_view(db: Session, plan: MigrationPlan) -> list[dict]:
    """计划页批次卡：每批体积、成本、开工月、当年净省（与工单 CSV 同源）。"""
    rows = scheduled_items(db, plan.id)
    batches: dict[int, dict] = {}
    for item in rows:
        batch = batches.setdefault(
            item.batch_no,
            {
                "batch_no": item.batch_no,
                "start_month": item.start_month,
                "table_count": 0,
                "size_gb": 0.0,
                "migration_cost": 0.0,
                "save_month": 0.0,
                "current_year_net_save": 0.0,
            },
        )
        batch["table_count"] += 1
        batch["size_gb"] += float(item.size_gb)
        batch["migration_cost"] += float(item.migration_cost)
        batch["save_month"] += float(item.save_month)
        batch["current_year_net_save"] += float(item.current_year_net_save)
    view = []
    for batch_no in sorted(batches):
        batch = batches[batch_no]
        view.append(
            {
                "batch_no": batch_no,
                "start_month": batch["start_month"],
                "table_count": batch["table_count"],
                "size_gb": money(batch["size_gb"]),
                "migration_cost": money(batch["migration_cost"]),
                "save_month": money(batch["save_month"]),
                "current_year_net_save": money(batch["current_year_net_save"]),
            }
        )
    return view


def serialize_item(item: MigrationPlanItem) -> dict:
    return {
        "id": str(item.id),
        "table_id": str(item.table_id),
        "table_name": item.table_name,
        "from_tier": item.from_tier,
        "suggested_tier": item.suggested_tier,
        "size_gb": float(item.size_gb),
        "hot_score": float(item.hot_score),
        "save_month": float(item.save_month),
        "migration_cost": float(item.migration_cost),
        "payback_months": float(item.payback_months) if item.payback_months is not None else None,
        "net_save_year": float(item.net_save_year),
        "risk": item.risk,
        "reasons": item.reasons,
        "status": item.status,
        "schedule_status": item.schedule_status,
        "batch_no": item.batch_no,
        "start_offset": item.start_offset,
        "start_month": item.start_month,
        "current_year_net_save": float(item.current_year_net_save),
        "net_per_gb": scheduling_core.net_per_gb(item),
    }


def serialize_plan(db: Session, plan: MigrationPlan, with_items: bool = True) -> dict:
    items = active_items(db, plan.id)
    scheduled = [item for item in items if item.schedule_status == scheduling_core.SCHEDULED]
    batches = batch_view(db, plan)
    payload = {
        "id": str(plan.id),
        "code": plan.code,
        "title": plan.title,
        "status": plan.status,
        "status_label": PLAN_STATUS_LABELS.get(plan.status, plan.status),
        "policy_version": int(plan.policy_version),
        "threshold": float(plan.threshold),
        "monthly_capacity_gb": float(plan.monthly_capacity_gb),
        "start_year": int(plan.start_year),
        "start_month": int(plan.start_month),
        "total_size_gb": float(plan.total_size_gb),
        "save_month": float(plan.save_month),
        "save_year": float(plan.save_year),
        "migration_cost": float(plan.migration_cost),
        "net_save_year": float(plan.net_save_year),
        "current_year_net_save": money(
            sum(float(item.current_year_net_save) for item in scheduled)
        ),
        "avg_payback": float(plan.avg_payback) if plan.avg_payback is not None else None,
        "risk_block_count": int(plan.risk_block_count),
        "risk_warn_count": int(plan.risk_warn_count),
        "scheduled_count": len(scheduled),
        "unscheduled_count": len(items) - len(scheduled),
        "batch_count": len(batches),
        "batches": batches,
        "created_by": plan.created_by,
        "submitted_by": plan.submitted_by,
        "submitted_at": plan.submitted_at.isoformat() if plan.submitted_at else None,
        "rechecked_at": plan.rechecked_at.isoformat() if plan.rechecked_at else None,
        "note": plan.note,
        "item_count": len(items),
        "items": [serialize_item(item) for item in items] if with_items else [],
    }
    return payload


def list_plans(db: Session) -> dict:
    rows = db.execute(select(MigrationPlan).order_by(MigrationPlan.created_at.desc())).scalars().all()
    items = [serialize_plan(db, plan, with_items=False) for plan in rows]
    return {"items": items, "meta": {"total": len(items), "returned": len(items), "filters": {}}}


def get_plan(db: Session, plan_id: str) -> dict:
    return serialize_plan(db, load_plan(db, plan_id))


def _plan_or_404(db: Session, plan_id) -> MigrationPlan:
    """非法 UUID 与不存在 UUID 统一 404（避免驱动层 500）。"""
    key = parse_uuid(plan_id)
    plan = db.get(MigrationPlan, key) if key is not None else None
    if plan is None:
        raise not_found("MigrationPlan", plan_id)
    return plan


def build_item(db: Session, plan: MigrationPlan, profile: TableProfile, hot, group) -> MigrationPlanItem:
    econ = cost_service.economics(float(profile.size_gb), profile.tier, group)
    verdict = cost_service.assess_risk(profile, hot, group)
    return MigrationPlanItem(
        plan_id=plan.id,
        table_id=profile.id,
        table_name=profile.full_name,
        from_tier=profile.tier,
        suggested_tier=hot.suggested_tier,
        size_gb=float(profile.size_gb),
        hot_score=hot.hot_score,
        save_month=econ.save_month,
        migration_cost=econ.migration_cost,
        payback_months=econ.payback_months,
        net_save_year=econ.net_save_year,
        risk=verdict.risk,
        reasons=verdict.reasons,
        status=PlanItemStatus.PENDING,
        schedule_status=PlanItemStatus.UNSCHEDULED,
    )


def _parse_schedule_inputs(payload) -> tuple[float, int]:
    try:
        capacity = scheduling_core.validate_capacity_gb(
            getattr(payload, "monthly_capacity_gb", None)
        )
    except ValueError as exc:
        raise AppError(codes.PLAN_CAPACITY_INVALID, str(exc), f"value={getattr(payload, 'monthly_capacity_gb', None)}") from None
    try:
        start_month = scheduling_core.validate_start_month(getattr(payload, "start_month", None))
    except ValueError as exc:
        raise AppError(codes.PLAN_START_MONTH_INVALID, str(exc), f"value={getattr(payload, 'start_month', None)}") from None
    return capacity, start_month


def create_plan(
    db: Session,
    group,
    payload,
    actor: User | None = None,
    trace_id: str = "",
    code: str | None = None,
) -> dict:
    table_ids = [str(tid) for tid in payload.table_ids]
    try:
        savings_core.check_plan_size(table_ids)
    except ValueError:
        raise AppError(
            codes.VALIDATION_ERROR, f"单次最多 {MAX_PLAN_ITEMS} 张表", f"count={len(table_ids)}"
        ) from None
    capacity, start_month = _parse_schedule_inputs(payload)
    now = _now()
    plan = MigrationPlan(
        code=code or _next_code(db, now),
        title=payload.title,
        status=PlanStatus.DRAFT,
        policy_version=group.version,
        threshold=payload.threshold,
        monthly_capacity_gb=capacity,
        start_year=now.year,
        start_month=start_month,
        created_by=actor.email if actor else "system",
        note=payload.note,
    )
    db.add(plan)
    db.flush()

    scores = heat_service.compute_scores(db, group)
    added = 0
    for table_id in table_ids:
        profile_key = parse_uuid(table_id)
        profile = db.get(TableProfile, profile_key) if profile_key is not None else None
        if profile is None:
            raise not_found("TableProfile", table_id)
        hot = scores.get(str(profile.id))
        if hot is None:
            continue
        if profile.tier == StorageTier.COLD:
            continue
        db.add(build_item(db, plan, profile, hot, group))
        added += 1
    db.flush()
    apply_schedule(db, plan, active_items(db, plan.id))
    summary = recount(db, plan)
    if actor is not None:
        audit_service.record(
            db,
            action=ACTION_PLAN_CREATE,
            actor=actor,
            entity_type="MigrationPlan",
            entity_id=plan.code,
            detail={
                "items": added,
                "threshold": float(payload.threshold),
                "monthly_capacity_gb": capacity,
                "start_month": start_month,
                **summary,
            },
            trace_id=trace_id,
        )
    return serialize_plan(db, plan)


def add_items(db: Session, group, plan_id: str, table_ids: list[str], actor: User, trace_id: str = "") -> dict:
    plan = _plan_or_404(db, plan_id)
    if plan.status != PlanStatus.DRAFT:
        raise AppError(codes.PLAN_NOT_DRAFT, "只有草稿状态的计划可以添加表", f"status={plan.status}")

    existing = {str(item.table_id) for item in active_items(db, plan.id)}
    scores = heat_service.compute_scores(db, group)
    added = 0
    for table_id in table_ids:
        table_key = str(table_id)
        if table_key in existing:
            continue
        profile_key = parse_uuid(table_id)
        profile = db.get(TableProfile, profile_key) if profile_key is not None else None
        if profile is None:
            raise not_found("TableProfile", table_id)
        hot = scores.get(table_key)
        if hot is None or profile.tier == StorageTier.COLD:
            continue
        db.add(build_item(db, plan, profile, hot, group))
        existing.add(table_key)
        added += 1
    db.flush()
    apply_schedule(db, plan, active_items(db, plan.id))
    summary = recount(db, plan)
    audit_service.record(
        db,
        action=ACTION_PLAN_ITEM_ADD,
        actor=actor,
        entity_type="MigrationPlan",
        entity_id=plan.code,
        detail={"added": added, **summary},
        trace_id=trace_id,
    )
    return serialize_plan(db, plan)


def remove_item(db: Session, plan_id: str, item_id: str, actor: User, trace_id: str = "") -> dict:
    plan = _plan_or_404(db, plan_id)
    if plan.status != PlanStatus.DRAFT:
        raise AppError(codes.PLAN_NOT_DRAFT, "只有草稿状态的计划可以移除表", f"status={plan.status}")
    item_key = parse_uuid(item_id)
    item = db.get(MigrationPlanItem, item_key) if item_key is not None else None
    if item is None or str(item.plan_id) != str(plan.id):
        raise not_found("MigrationPlanItem", item_id)
    item.status = PlanItemStatus.REMOVED
    db.flush()
    apply_schedule(db, plan, active_items(db, plan.id))
    summary = recount(db, plan)
    audit_service.record(
        db,
        action=ACTION_PLAN_ITEM_REMOVE,
        actor=actor,
        entity_type="MigrationPlan",
        entity_id=plan.code,
        detail={"removed": item.table_name, "risk": item.risk, **summary},
        trace_id=trace_id,
    )
    return serialize_plan(db, plan)


def recheck(db: Session, group, plan_id: str, actor: User, trace_id: str = "") -> dict:
    """风险复检：按最新访问数据重算每张表的风险与收益，再按容量重新排期、刷新汇总。"""
    plan = _plan_or_404(db, plan_id)
    scores = heat_service.compute_scores(db, group)
    for item in active_items(db, plan.id):
        profile = db.get(TableProfile, item.table_id)
        hot = scores.get(str(item.table_id))
        if profile is None or hot is None:
            continue
        econ = cost_service.economics(float(profile.size_gb), profile.tier, group)
        verdict = cost_service.assess_risk(profile, hot, group)
        item.size_gb = float(profile.size_gb)
        item.hot_score = hot.hot_score
        item.suggested_tier = hot.suggested_tier
        item.save_month = econ.save_month
        item.migration_cost = econ.migration_cost
        item.payback_months = econ.payback_months
        item.net_save_year = econ.net_save_year
        item.risk = verdict.risk
        item.reasons = verdict.reasons
    plan.rechecked_at = _now()
    db.flush()
    apply_schedule(db, plan, active_items(db, plan.id))
    summary = recount(db, plan)
    audit_service.record(
        db,
        action=ACTION_PLAN_RECHECK,
        actor=actor,
        entity_type="MigrationPlan",
        entity_id=plan.code,
        detail={**summary},
        trace_id=trace_id,
    )
    return serialize_plan(db, plan)


def submit(db: Session, plan_id: str, actor: User, trace_id: str = "") -> dict:
    plan = _plan_or_404(db, plan_id)
    if plan.status != PlanStatus.DRAFT:
        raise AppError(codes.PLAN_NOT_DRAFT, "该计划已提交，不能重复提交", f"status={plan.status}")
    items = active_items(db, plan.id)
    if not items:
        raise AppError(codes.PLAN_EMPTY, "计划中没有可提交的表", f"plan={plan.code}")
    # 排不下的表不计收益也不进工单，不构成提交阻断；阻断闸门只看能排上批次的表
    blockers = savings_core.blockers(
        [item for item in items if item.schedule_status == scheduling_core.SCHEDULED]
    )
    if blockers:
        raise AppError(
            codes.PLAN_HAS_BLOCKERS,
            f"仍有 {len(blockers)} 张表被风险闸门阻断，请先移除",
            ",".join(blockers),
            extra={"blockers": blockers},
        )
    scheduled = scheduled_items(db, plan.id)
    if not scheduled:
        raise AppError(
            codes.PLAN_EMPTY,
            "月度容量装不下任何表，调大容量或移除超大表后再提交",
            f"plan={plan.code}",
        )
    now = _now()
    for item in items:
        if item.schedule_status == scheduling_core.SCHEDULED:
            item.status = PlanItemStatus.SUBMITTED
        else:
            # 排不下的表保留待处理状态，随计划留档但不生成工单
            item.status = PlanItemStatus.UNSCHEDULED
    plan.status = PlanStatus.SUBMITTED
    plan.submitted_at = now
    plan.submitted_by = actor.email
    db.flush()
    summary = recount(db, plan)
    audit_service.record(
        db,
        action=ACTION_PLAN_SUBMIT,
        actor=actor,
        entity_type="MigrationPlan",
        entity_id=plan.code,
        detail={**summary},
        trace_id=trace_id,
    )
    return serialize_plan(db, plan)


def record_export(db: Session, plan: MigrationPlan, actor: User, row_count: int, trace_id: str = "") -> None:
    audit_service.record(
        db,
        action=ACTION_PLAN_EXPORT,
        actor=actor,
        entity_type="MigrationPlan",
        entity_id=plan.code,
        detail={"rows": row_count},
        trace_id=trace_id,
    )


def load_plan(db: Session, plan_id: str) -> MigrationPlan:
    return _plan_or_404(db, plan_id)


def summary_of(db: Session, plan_id: str) -> dict:
    plan = load_plan(db, plan_id)
    counts = recount(db, plan)
    return {
        "plan_id": str(plan.id),
        "status": plan.status,
        "rechecked_at": plan.rechecked_at.isoformat() if plan.rechecked_at else None,
        **counts,
    }


def plan_item_rows(db: Session, plan: MigrationPlan) -> list[MigrationPlanItem]:
    """工单导出口径：只含已排入批次的表，按批次号、批次内顺序输出。"""
    return scheduled_items(db, plan.id)


def stats(db: Session) -> dict:
    total = db.query(func.count(MigrationPlan.id)).scalar() or 0
    return {"plan_total": int(total)}
