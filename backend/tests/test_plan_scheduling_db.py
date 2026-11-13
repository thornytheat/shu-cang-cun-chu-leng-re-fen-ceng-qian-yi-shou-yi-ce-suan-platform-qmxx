"""plan_service 排期链路的 SQLite 集成测试（不依赖 Postgres / Redis）。

PG 专有类型（JSONB / UUID）在 SQLite 上编译为通用 JSON / CHAR(32)，
业务逻辑（容量分批、汇总口径、提交与工单导出口径）与方言无关。
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import sessionmaker
from sqlalchemy.types import CHAR, JSON, TypeDecorator

from src.constants.enums import PlanItemStatus
from src.models import Base
from src.models.migration_plan import MigrationPlan
from src.models.migration_plan_item import MigrationPlanItem
from src.services import export_service, plan_service, scheduling_core


class GUID(TypeDecorator):
    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(CHAR(32))

    def process_bind_param(self, value, dialect):
        return str(value) if value is not None else None

    def process_result_value(self, value, dialect):
        import uuid

        return uuid.UUID(value) if value is not None else None


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    # PG 类型/schema 在 sqlite 方言下的替身（仅 DDL/绑定用，不改业务模型语义）
    original_schemas = {table.name: table.schema for table in Base.metadata.tables.values()}
    original_types = {
        (table.name, column.name): column.type
        for table in Base.metadata.tables.values()
        for column in table.columns
    }
    for table in Base.metadata.tables.values():
        table.schema = None
        for column in table.columns:
            if isinstance(column.type, UUID):
                column.type = GUID()
            elif isinstance(column.type, JSONB):
                column.type = JSON()
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        # 还原全局 metadata，避免污染同进程内其它测试
        for table in Base.metadata.tables.values():
            table.schema = original_schemas[table.name]
            for column in table.columns:
                column.type = original_types[(table.name, column.name)]


def _make_plan(db, capacity=4000.0, start_year=2026, start_month=11) -> MigrationPlan:
    plan = MigrationPlan(
        code="MP-TEST-Q1-001",
        title="排期集成测试计划",
        status="draft",
        policy_version=3,
        threshold=20.0,
        monthly_capacity_gb=capacity,
        start_year=start_year,
        start_month=start_month,
    )
    db.add(plan)
    db.flush()
    return plan


def _make_item(plan, name, size, save_month, migration_cost, net_year, payback=None, risk="ok") -> MigrationPlanItem:
    return MigrationPlanItem(
        plan_id=plan.id,
        table_id=uuid4(),
        table_name=name,
        from_tier="hot",
        suggested_tier="cold",
        size_gb=size,
        hot_score=5.0,
        save_month=save_month,
        migration_cost=migration_cost,
        payback_months=payback,
        net_save_year=net_year,
        risk=risk,
        reasons="测试",
        status=PlanItemStatus.PENDING,
        schedule_status=PlanItemStatus.UNSCHEDULED,
    )


def _seed_demo_plan(db):
    """4 张表：一张超容量（4800），其余按每 GB 净收益排序装箱。"""
    plan = _make_plan(db)
    items = [
        # name, size, save_month, migration_cost, net_year
        _make_item(plan, "huge.trade", 4800, 2400, 3870, 24930, payback=1.6),       # 每GB 5.19 -> 排不下
        _make_item(plan, "dense.click", 1680, 840, 1530, 8550, payback=1.8),        # 5.09
        _make_item(plan, "mid.inv", 1260, 630, 1215, 6345, payback=1.9),            # 5.04
        _make_item(plan, "small.pay", 960, 480, 990, 4770, payback=2.1),            # 4.97
    ]
    db.add_all(items)
    db.flush()
    plan_service.apply_schedule(db, plan, plan_service.active_items(db, plan.id))
    plan_service.recount(db, plan)
    return plan, items


def test_schedule_marks_oversize_table_unscheduled(db_session) -> None:
    plan, items = _seed_demo_plan(db_session)
    payload = plan_service.serialize_plan(db_session, plan)

    assert payload["scheduled_count"] == 3
    assert payload["unscheduled_count"] == 1
    huge = next(item for item in payload["items"] if item["table_name"] == "huge.trade")
    assert huge["schedule_status"] == scheduling_core.UNSCHEDULED
    assert huge["batch_no"] is None and huge["start_month"] is None
    # 排不下的表不计入计划头金额/体积
    assert payload["total_size_gb"] == pytest.approx(1680 + 1260 + 960)
    assert payload["migration_cost"] == pytest.approx(1530 + 1215 + 990)


def test_batches_respect_capacity_and_carry_start_months(db_session) -> None:
    plan, _ = _seed_demo_plan(db_session)
    payload = plan_service.serialize_plan(db_session, plan)

    # 批1：1680+1260=2940；再加 960 会到 3900 <= 4000，实际三表同批
    assert payload["batch_count"] == 1
    batch = payload["batches"][0]
    assert batch["size_gb"] == pytest.approx(3900.0)
    assert batch["start_month"] == "2026-11"
    assert batch["table_count"] == 3
    for item in payload["items"]:
        if item["schedule_status"] == scheduling_core.SCHEDULED:
            assert item["batch_no"] == 1
            assert item["start_month"] == "2026-11"


def test_tight_capacity_opens_second_batch_with_next_month(db_session) -> None:
    plan = _make_plan(db_session, capacity=2000.0)
    db_session.add_all(
        [
            _make_item(plan, "a", 1200, 600, 1170, 6030, payback=2.0),   # 5.025/GB
            _make_item(plan, "b", 900, 450, 945, 4455, payback=2.1),    # 4.95/GB
            _make_item(plan, "c", 800, 400, 870, 3930, payback=2.2),    # 4.9125/GB
        ]
    )
    db_session.flush()
    plan_service.apply_schedule(db_session, plan, plan_service.active_items(db_session, plan.id))
    plan_service.recount(db_session, plan)
    payload = plan_service.serialize_plan(db_session, plan)

    # 排序 a → b → c；容量 2000：批1 装 a(1200) 后 b 放不下（2100>2000），b、c 进批2
    assert payload["batch_count"] == 2
    assert [b["start_month"] for b in payload["batches"]] == ["2026-11", "2026-12"]
    assert payload["batches"][0]["size_gb"] == pytest.approx(1200.0)
    assert payload["batches"][1]["size_gb"] == pytest.approx(1700.0)
    # 当年净省：批1 offset0 有 12 个节省月，批2 offset1 有 11 个节省月
    first, second = payload["batches"]
    assert first["current_year_net_save"] == pytest.approx(600 * 12 - 1170)
    assert second["current_year_net_save"] == pytest.approx((450 + 400) * 11 - (945 + 870))


def test_submit_skips_unscheduled_and_export_matches_page(db_session) -> None:
    plan, _ = _seed_demo_plan(db_session)
    actor = SimpleNamespace(id=uuid4(), email="analyst@coldtier.io", role="analyst")
    submitted = plan_service.submit(db_session, str(plan.id), actor)

    assert submitted["status"] == "submitted"
    assert submitted["scheduled_count"] == 3
    # 排不下的表状态为 unscheduled，不进工单
    statuses = {item["table_name"]: item["status"] for item in submitted["items"]}
    assert statuses["huge.trade"] == PlanItemStatus.UNSCHEDULED

    csv_text, rows = export_service.build_ticket_csv(db_session, plan)
    assert rows == 3
    assert "huge.trade" not in csv_text
    header = csv_text.splitlines()[0]
    assert "批次" in header and "开工月" in header and "当年净省元" in header
    # 每行都带批次与开工月，且与页面批次卡一致
    for line in csv_text.splitlines()[1:]:
        assert "第 1 批" in line and "2026-11" in line


def test_reschedule_after_remove_refreshes_batches(db_session) -> None:
    plan, items = _seed_demo_plan(db_session)
    # 移除超容量表后，剩余三表仍为一批；移除其中一张后排期不变批次数
    huge = next(item for item in items if item.table_name == "huge.trade")
    huge.status = PlanItemStatus.REMOVED
    db_session.flush()
    plan_service.apply_schedule(db_session, plan, plan_service.active_items(db_session, plan.id))
    plan_service.recount(db_session, plan)
    payload = plan_service.serialize_plan(db_session, plan)
    assert payload["unscheduled_count"] == 0
    assert payload["scheduled_count"] == 3
    assert payload["item_count"] == 3


def test_submit_rejects_when_everything_unscheduled(db_session) -> None:
    plan = _make_plan(db_session, capacity=100.0)
    db_session.add(_make_item(plan, "too-big", 500, 250, 645, 2355, payback=2.6))
    db_session.flush()
    plan_service.apply_schedule(db_session, plan, plan_service.active_items(db_session, plan.id))
    plan_service.recount(db_session, plan)
    actor = SimpleNamespace(id=uuid4(), email="analyst@coldtier.io", role="analyst")

    with pytest.raises(Exception) as exc:
        plan_service.submit(db_session, str(plan.id), actor)
    assert "容量" in str(exc.value)
