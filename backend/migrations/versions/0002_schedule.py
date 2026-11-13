"""分批排期字段：计划头月度容量/开工年月，计划项批次号/开工月/当年净省/排期状态

Revision ID: 0002_schedule
Revises: 0001_initial
Create Date: 2026-11-13
"""
from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
from sqlalchemy import Column, Integer, Numeric, String, text

from src.config.settings import settings

revision = "0002_schedule"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

SCHEMA = settings.db_schema


def _label(base_year: int, base_month: int, offset: int) -> str:
    index = base_year * 12 + base_month - 1 + offset
    year, month = divmod(index, 12)
    return f"{year:04d}-{month + 1:02d}"


def upgrade() -> None:
    bind = op.get_bind()
    plan_table = f'"{SCHEMA}"."migration_plans"'
    item_table = f'"{SCHEMA}"."migration_plan_items"'

    op.add_column(
        "migration_plans",
        Column("monthly_capacity_gb", Numeric(14, 2), nullable=False, server_default="4000"),
        schema=SCHEMA,
    )
    op.add_column(
        "migration_plans",
        Column("start_year", Integer, nullable=False, server_default="2026"),
        schema=SCHEMA,
    )
    op.add_column(
        "migration_plans",
        Column("start_month", Integer, nullable=False, server_default="1"),
        schema=SCHEMA,
    )
    op.add_column(
        "migration_plan_items",
        Column("schedule_status", String(12), nullable=False, server_default="unscheduled"),
        schema=SCHEMA,
    )
    op.add_column("migration_plan_items", Column("batch_no", Integer, nullable=True), schema=SCHEMA)
    op.add_column("migration_plan_items", Column("start_offset", Integer, nullable=True), schema=SCHEMA)
    op.add_column("migration_plan_items", Column("start_month", String(7), nullable=True), schema=SCHEMA)
    op.add_column(
        "migration_plan_items",
        Column("current_year_net_save", Numeric(12, 2), nullable=False, server_default="0"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_migration_plan_items_schedule_status",
        "migration_plan_items",
        ["schedule_status"],
        schema=SCHEMA,
    )

    # 存量计划按当前月开工、默认容量重排一次，避免新字段全是默认值。
    # 全部取值来自 DB 行或内部计算（整数/浮点/YYYY-MM 串），无外部输入，内联字面量安全，
    # 同时保证 alembic --sql 离线生成的脚本不出现 NULL 绑定占位。
    today = datetime.now(timezone.utc)
    bind.execute(
        text(
            f"UPDATE {plan_table} SET start_year = {int(today.year)}, "
            f"start_month = {int(today.month)} WHERE start_year = 2026 AND start_month = 1"
        )
    )

    rows = bind.execute(
        text(
            f"SELECT id, monthly_capacity_gb, start_year, start_month FROM {plan_table} ORDER BY created_at"
        )
    ).all()
    for plan_id, capacity, base_year, base_month in rows:
        items = bind.execute(
            text(
                f"SELECT id, size_gb, save_month, migration_cost, net_save_year, payback_months "
                f"FROM {item_table} WHERE plan_id = '{plan_id}' AND status != 'removed' ORDER BY created_at"
            )
        ).all()
        ordered = sorted(
            items,
            key=lambda row: (
                -(float(row.net_save_year) / float(row.size_gb) if float(row.size_gb) > 0 else 0.0),
                1 if row.payback_months is None else 0,
                float(row.payback_months) if row.payback_months is not None else 0.0,
            ),
        )
        batch_no = 0
        used = 0.0
        capacity_value = float(capacity)
        for item_id, size_gb, save_month, migration_cost, _net, _payback in ordered:
            size = float(size_gb)
            if size > capacity_value:
                bind.execute(
                    text(
                        f"UPDATE {item_table} SET schedule_status = 'unscheduled', batch_no = NULL, "
                        f"start_offset = NULL, start_month = NULL, current_year_net_save = 0 "
                        f"WHERE id = '{item_id}'"
                    )
                )
                continue
            if batch_no == 0 or used + size > capacity_value:
                batch_no += 1
                used = 0.0
            offset = batch_no - 1
            saving_months = max(0, 12 - offset)
            current_net = float(save_month) * saving_months - float(migration_cost)
            month = _label(int(base_year), int(base_month), offset)
            bind.execute(
                text(
                    f"UPDATE {item_table} SET schedule_status = 'scheduled', batch_no = {batch_no}, "
                    f"start_offset = {offset}, start_month = '{month}', "
                    f"current_year_net_save = {current_net:.2f} WHERE id = '{item_id}'"
                )
            )
            used += size


def downgrade() -> None:
    op.drop_index(
        "ix_migration_plan_items_schedule_status",
        table_name="migration_plan_items",
        schema=SCHEMA,
    )
    for name in (
        "current_year_net_save",
        "start_month",
        "start_offset",
        "batch_no",
        "schedule_status",
    ):
        op.drop_column("migration_plan_items", name, schema=SCHEMA)
    for name in ("start_month", "start_year", "monthly_capacity_gb"):
        op.drop_column("migration_plans", name, schema=SCHEMA)
