"""分批排期纯函数层（容量约束下的搬迁计划）。

背景：旧口径把选中表视为「一次迁完、按全年算节省」。搬迁窗口有月度容量上限时，
该口径会虚高账面收益，也排不出可执行工单。本模块按容量上限做 first-fit 分批：

  排序    每 GB 首年净收益 = net_save_year / size_gb，从高到低；
          同值时回收期（payback_months）短的优先（None 排最后）。
  装箱    单表不可拆分：当前批次剩余容量装不下就开下一批，每批体积 <= 月度容量。
  排期    批次 b（0 起）的开工月 = start_month + b，即每月一批；
          单表体积超过月度容量时任何批次都装不下 -> 排不下（unscheduled）。
  节省    每张表迁完后次月开始产生节省；当年净省按开工月折算，
          开工月 12 的表当年没有节省月份，当年净省为 0（迁移成本仍全额计入）。

只吃 dict 或带同名属性的对象，不 import Session，可被 pytest 直接单测。
"""
from __future__ import annotations

from src.constants.limits import (
    MAX_MONTHLY_CAPACITY_GB,
    MAX_START_MONTH,
    MIN_MONTHLY_CAPACITY_GB,
    MIN_START_MONTH,
)
from src.utils.numeric import money

SCHEDULED = "scheduled"
UNSCHEDULED = "unscheduled"


def _field(item, name):
    if isinstance(item, dict):
        return item[name]
    return getattr(item, name)


def validate_capacity_gb(value) -> float:
    """月度搬迁容量（GB）必须为正数且落在常量边界内；非法值抛 ValueError。"""
    try:
        capacity = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"月度容量必须是数字，收到 {value!r}") from exc
    if capacity < MIN_MONTHLY_CAPACITY_GB or capacity > MAX_MONTHLY_CAPACITY_GB:
        raise ValueError(
            f"月度容量必须落在 {MIN_MONTHLY_CAPACITY_GB:g} 到 {MAX_MONTHLY_CAPACITY_GB:g} GB 之间，收到 {value!r}"
        )
    return capacity


def validate_start_month(value) -> int:
    """开工月份必须是 1..12 的整数（年份取计划创建当年）。"""
    try:
        month = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"开工月份必须是整数，收到 {value!r}") from exc
    if month < MIN_START_MONTH or month > MAX_START_MONTH:
        raise ValueError(f"开工月份必须落在 {MIN_START_MONTH} 到 {MAX_START_MONTH} 之间，收到 {value!r}")
    return month


def net_per_gb(item) -> float:
    """每 GB 首年净收益（排序第一键，降序）。零体积表返回 0，不抛异常。"""
    size = float(_field(item, "size_gb"))
    if size <= 0:
        return 0.0
    return float(_field(item, "net_save_year")) / size


def _payback_key(item) -> tuple[int, float]:
    """回收期排序键（同值时短的优先）：None 视为无穷大，排最后。"""
    payback = _field(item, "payback_months")
    if payback is None:
        return (1, 0.0)
    return (0, float(payback))


def schedule_sort_key(item) -> tuple:
    """全局装箱排序键：每 GB 首年净收益降序，同值时回收期短者优先。

    service 层从 DB 重建列表时复用同一键，保证页面/工单顺序与装箱顺序逐行一致。
    """
    return (-net_per_gb(item), *_payback_key(item))


def month_label(base_year: int, base_month: int, offset: int) -> str:
    """开工月标签：base 年/月 + offset 个月（offset 从 0 起），格式 YYYY-MM。"""
    index = (int(base_year) * 12 + int(base_month) - 1) + int(offset)
    year, month = divmod(index, 12)
    return f"{year:04d}-{month + 1:02d}"


def current_year_net_save(
    save_month: float,
    migration_cost: float,
    start_offset: int,
    *,
    months_in_year: int = 12,
) -> float:
    """单表当年净省：迁完次月起计节省，开工月 offset 当年剩 (12 - offset) 个节省月。

    offset >= 12 时当年没有节省月份，节省为 0，但一次性迁移成本仍全额计入，
    因此当年净省可能为负（与「不计收益」的排不下表不同，排不下表不进任何汇总）。
    """
    saving_months = max(0, int(months_in_year) - int(start_offset))
    return money(float(save_month) * saving_months - float(migration_cost))


def schedule_batches(
    items: list,
    monthly_capacity_gb: float,
    base_year: int,
    base_month: int,
) -> dict:
    """按容量上限分批排期。

    返回：
      scheduled       排上的表（带 batch_no / start_offset / start_month / 当年净省）
      unscheduled     容量装不下的表（不计任何收益与成本汇总）
      batches         每批体积、成本、开工月、当年净省
      summary         排期口径合计（仅含 scheduled）
    """
    capacity = float(monthly_capacity_gb)
    ordered = sorted(items, key=schedule_sort_key)

    batches: list[dict] = []
    current: dict | None = None
    scheduled: list[dict] = []
    unscheduled: list[dict] = []

    def open_batch() -> dict:
        batch = {
            "batch_no": len(batches) + 1,
            "start_offset": len(batches),
            "start_month": month_label(base_year, base_month, len(batches)),
            "size_gb": 0.0,
            "migration_cost": 0.0,
            "save_month": 0.0,
            "current_year_net_save": 0.0,
            "table_count": 0,
            "items": [],
        }
        batches.append(batch)
        return batch

    for item in ordered:
        size = float(_field(item, "size_gb"))
        if size > capacity:
            unscheduled.append(_unscheduled_row(item, reason="single_table_over_capacity"))
            continue
        if current is None or current["size_gb"] + size > capacity:
            current = open_batch()
        offset = current["start_offset"]
        row = _scheduled_row(item, batch_no=current["batch_no"], offset=offset, base_year=base_year, base_month=base_month)
        scheduled.append(row)

        current["items"].append(row)
        current["size_gb"] += size
        current["migration_cost"] += float(_field(item, "migration_cost"))
        current["save_month"] += float(_field(item, "save_month"))
        current["current_year_net_save"] += row["current_year_net_save"]
        current["table_count"] += 1

    for batch in batches:
        batch["size_gb"] = money(batch["size_gb"])
        batch["migration_cost"] = money(batch["migration_cost"])
        batch["save_month"] = money(batch["save_month"])
        batch["current_year_net_save"] = money(batch["current_year_net_save"])

    summary = {
        "scheduled_count": len(scheduled),
        "unscheduled_count": len(unscheduled),
        "batch_count": len(batches),
        "scheduled_size_gb": money(sum(row["size_gb"] for row in scheduled)),
        "scheduled_migration_cost": money(sum(row["migration_cost"] for row in scheduled)),
        "current_year_net_save": money(sum(batch["current_year_net_save"] for batch in batches)),
    }
    return {
        "scheduled": scheduled,
        "unscheduled": unscheduled,
        "batches": [
            {key: value for key, value in batch.items() if key != "items"} for batch in batches
        ],
        "summary": summary,
    }


def _scheduled_row(item, *, batch_no: int, offset: int, base_year: int, base_month: int) -> dict:
    save_month = float(_field(item, "save_month"))
    migration_cost = float(_field(item, "migration_cost"))
    return {
        "ref": item,
        "schedule_status": SCHEDULED,
        "batch_no": batch_no,
        "start_offset": offset,
        "start_month": month_label(base_year, base_month, offset),
        "size_gb": float(_field(item, "size_gb")),
        "save_month": save_month,
        "migration_cost": migration_cost,
        "current_year_net_save": current_year_net_save(save_month, migration_cost, offset),
    }


def _unscheduled_row(item, *, reason: str) -> dict:
    return {
        "ref": item,
        "schedule_status": UNSCHEDULED,
        "batch_no": None,
        "start_offset": None,
        "start_month": None,
        "size_gb": float(_field(item, "size_gb")),
        "save_month": float(_field(item, "save_month")),
        "migration_cost": float(_field(item, "migration_cost")),
        "current_year_net_save": 0.0,
        "unscheduled_reason": reason,
    }
