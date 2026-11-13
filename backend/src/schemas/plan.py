"""迁移计划入出参。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from src.constants.limits import (
    MAX_MONTHLY_CAPACITY_GB,
    MAX_START_MONTH,
    MIN_MONTHLY_CAPACITY_GB,
    MIN_START_MONTH,
)


class PlanBatchOut(BaseModel):
    batch_no: int
    start_month: str | None
    table_count: int
    size_gb: float
    save_month: float
    migration_cost: float
    current_year_net_save: float


class PlanItemOut(BaseModel):
    id: str
    table_id: str
    table_name: str
    from_tier: str
    suggested_tier: str
    size_gb: float
    hot_score: float
    save_month: float
    migration_cost: float
    payback_months: float | None
    net_save_year: float
    risk: str
    reasons: str
    status: str
    schedule_status: str
    batch_no: int | None
    start_offset: int | None
    start_month: str | None
    current_year_net_save: float
    net_per_gb: float


class PlanOut(BaseModel):
    id: str
    code: str
    title: str
    status: str
    status_label: str
    policy_version: int
    threshold: float
    monthly_capacity_gb: float
    start_year: int
    start_month: int
    total_size_gb: float
    save_month: float
    save_year: float
    migration_cost: float
    net_save_year: float
    current_year_net_save: float
    avg_payback: float | None
    risk_block_count: int
    risk_warn_count: int
    scheduled_count: int
    unscheduled_count: int
    batch_count: int
    batches: list[PlanBatchOut] = Field(default_factory=list)
    created_by: str
    submitted_by: str
    submitted_at: str | None
    rechecked_at: str | None
    note: str
    item_count: int
    items: list[PlanItemOut] = Field(default_factory=list)


class PlanCreateIn(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    threshold: float = Field(default=20, ge=0, le=100)
    monthly_capacity_gb: float = Field(
        default=4000, ge=MIN_MONTHLY_CAPACITY_GB, le=MAX_MONTHLY_CAPACITY_GB
    )
    start_month: int = Field(default=1, ge=MIN_START_MONTH, le=MAX_START_MONTH)
    table_ids: list[str] = Field(default_factory=list)
    note: str = ""


class PlanItemsIn(BaseModel):
    table_ids: list[str] = Field(min_length=1)


class PlanSummaryOut(BaseModel):
    plan_id: str
    status: str
    item_count: int
    scheduled_count: int
    unscheduled_count: int
    batch_count: int
    total_size_gb: float
    save_month: float
    save_year: float
    migration_cost: float
    net_save_year: float
    current_year_net_save: float
    avg_payback: float | None
    risk_block_count: int
    risk_warn_count: int
    rechecked_at: str | None
