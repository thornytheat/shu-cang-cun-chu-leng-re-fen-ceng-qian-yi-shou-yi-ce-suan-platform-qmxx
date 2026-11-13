"""分批排期纯函数单测（容量约束 / 排序键 / 单表不可拆 / 当年净省折算）。

不连库：输入全部是就地构造的 dict，字段与 MigrationPlanItem 同名属性对齐。
"""
from __future__ import annotations

import pytest

from src.services import scheduling_core
from src.utils.numeric import money


def _row(name, size, save_month, migration_cost, net_year, payback=None) -> dict:
    return {
        "table_name": name,
        "full_name": name,
        "size_gb": float(size),
        "save_month": float(save_month),
        "migration_cost": float(migration_cost),
        "net_save_year": float(net_year),
        "payback_months": payback,
    }


def test_validate_capacity_accepts_bounds() -> None:
    assert scheduling_core.validate_capacity_gb(1) == 1.0
    assert scheduling_core.validate_capacity_gb("4000") == 4000.0
    assert scheduling_core.validate_capacity_gb(1_000_000) == 1_000_000.0


@pytest.mark.parametrize("value", [0, -10, 1_000_000.1, "abc", None])
def test_validate_capacity_rejects_bad_values(value) -> None:
    with pytest.raises(ValueError):
        scheduling_core.validate_capacity_gb(value)


@pytest.mark.parametrize("value", [1, 6, 12, "12"])
def test_validate_start_month_accepts_1_to_12(value) -> None:
    assert 1 <= scheduling_core.validate_start_month(value) <= 12


@pytest.mark.parametrize("value", [0, 13, -1, "x", None])
def test_validate_start_month_rejects_out_of_range(value) -> None:
    with pytest.raises(ValueError):
        scheduling_core.validate_start_month(value)


def test_month_label_rolls_over_year() -> None:
    assert scheduling_core.month_label(2026, 11, 0) == "2026-11"
    assert scheduling_core.month_label(2026, 11, 1) == "2026-12"
    assert scheduling_core.month_label(2026, 11, 2) == "2027-01"
    assert scheduling_core.month_label(2026, 12, 13) == "2028-01"


def test_current_year_net_save_prorates_by_start_month() -> None:
    # 开工月 offset=0 -> 迁完次月起 12 个节省月：100×12 - 300 = 900
    assert scheduling_core.current_year_net_save(100, 300, 0) == 900.0
    # offset=1 -> 11 个节省月：100×11 - 300 = 800
    assert scheduling_core.current_year_net_save(100, 300, 1) == 800.0
    # offset=11 -> 1 个节省月：100 - 300 = -200（成本全额计入，可为负）
    assert scheduling_core.current_year_net_save(100, 300, 11) == -200.0
    # offset=12 -> 当年没有节省月份：-300
    assert scheduling_core.current_year_net_save(100, 300, 12) == -300.0


def test_net_per_gb_is_primary_sort_key_desc() -> None:
    # A 每 GB 净收益 5 元，B 每 GB 净收益 4 元：A 先排，即使 B 总净省更高
    rows = [
        _row("B-big", size=1000, save_month=400, migration_cost=0, net_year=4000),
        _row("A-dense", size=100, save_month=50, migration_cost=0, net_year=500),
    ]
    result = scheduling_core.schedule_batches(rows, monthly_capacity_gb=10_000, base_year=2026, base_month=1)
    assert [row["ref"]["table_name"] for row in result["scheduled"]] == ["A-dense", "B-big"]


def test_equal_net_per_gb_shorter_payback_goes_first() -> None:
    # 两张表每 GB 净收益相同（net/size 都 = 3），回收期短的先排
    rows = [
        _row("slow", size=100, save_month=10, migration_cost=200, net_year=300, payback=20.0),
        _row("fast", size=100, save_month=20, migration_cost=100, net_year=300, payback=5.0),
    ]
    result = scheduling_core.schedule_batches(rows, monthly_capacity_gb=10_000, base_year=2026, base_month=1)
    assert [row["ref"]["table_name"] for row in result["scheduled"]] == ["fast", "slow"]


def test_equal_net_per_gb_none_payback_sorts_last() -> None:
    rows = [
        _row("no-payback", size=100, save_month=0, migration_cost=100, net_year=300, payback=None),
        _row("normal", size=100, save_month=20, migration_cost=100, net_year=300, payback=5.0),
    ]
    result = scheduling_core.schedule_batches(rows, monthly_capacity_gb=10_000, base_year=2026, base_month=1)
    assert [row["ref"]["table_name"] for row in result["scheduled"]] == ["normal", "no-payback"]


def test_first_fit_splits_into_capacity_batches_without_splitting_tables() -> None:
    # 按每 GB 净收益降序：t1(60) → t3(20) → t2(30)；容量 100
    # 批1 装下 60+20=80 后，30 装不下（110 > 100）开新批，单表始终不拆
    rows = [
        _row("t1", size=60, save_month=60, migration_cost=10, net_year=710),   # 11.83/GB
        _row("t2", size=30, save_month=30, migration_cost=10, net_year=120),   # 4.0/GB
        _row("t3", size=20, save_month=20, migration_cost=10, net_year=230),   # 11.5/GB
    ]
    result = scheduling_core.schedule_batches(rows, monthly_capacity_gb=100, base_year=2026, base_month=3)
    order = [row["ref"]["table_name"] for row in result["scheduled"]]
    assert order == ["t1", "t3", "t2"]
    assert result["summary"]["batch_count"] == 2
    first, second = result["batches"]
    assert first["size_gb"] == 80.0
    assert second["size_gb"] == 30.0
    # 开工月逐批后推一个月
    assert first["start_month"] == "2026-03"
    assert second["start_month"] == "2026-04"
    assert all(batch["size_gb"] <= 100.0 for batch in result["batches"])


def test_table_larger_than_capacity_is_unscheduled_and_excluded() -> None:
    rows = [
        _row("huge", size=5000, save_month=2000, migration_cost=4000, net_year=20_000),
        _row("ok", size=100, save_month=50, migration_cost=100, net_year=500),
    ]
    result = scheduling_core.schedule_batches(rows, monthly_capacity_gb=4000, base_year=2026, base_month=11)
    assert result["summary"]["scheduled_count"] == 1
    assert result["summary"]["unscheduled_count"] == 1
    unscheduled = result["unscheduled"][0]
    assert unscheduled["ref"]["table_name"] == "huge"
    assert unscheduled["batch_no"] is None
    assert unscheduled["start_month"] is None
    assert unscheduled["current_year_net_save"] == 0.0
    # 排不下的表不计入任何汇总
    assert result["summary"]["scheduled_size_gb"] == 100.0
    assert result["summary"]["scheduled_migration_cost"] == 100.0


def test_empty_input_degrades_to_zero() -> None:
    result = scheduling_core.schedule_batches([], 4000, 2026, 1)
    assert result["scheduled"] == []
    assert result["unscheduled"] == []
    assert result["batches"] == []
    assert result["summary"] == {
        "scheduled_count": 0,
        "unscheduled_count": 0,
        "batch_count": 0,
        "scheduled_size_gb": 0.0,
        "scheduled_migration_cost": 0.0,
        "current_year_net_save": 0.0,
    }


def test_batch_current_year_net_save_matches_item_sum() -> None:
    # 容量 200：a、b 同批（offset 0，12 个节省月）；c 开新批（offset 1，11 个节省月）
    rows = [
        _row("a", size=100, save_month=100, migration_cost=200, net_year=1000),
        _row("b", size=100, save_month=100, migration_cost=200, net_year=1000),
        _row("c", size=100, save_month=100, migration_cost=200, net_year=1000),
    ]
    result = scheduling_core.schedule_batches(rows, monthly_capacity_gb=200, base_year=2026, base_month=1)
    assert result["batches"][0]["current_year_net_save"] == money((1200 - 200) * 2)
    assert result["batches"][1]["current_year_net_save"] == 900.0
    assert result["summary"]["current_year_net_save"] == money(2000 + 900)
    # 计划项上的开工月/批次号与批次视图一致
    by_name = {row["ref"]["table_name"]: row for row in result["scheduled"]}
    assert by_name["a"]["batch_no"] == 1 and by_name["a"]["start_month"] == "2026-01"
    assert by_name["c"]["batch_no"] == 2 and by_name["c"]["start_month"] == "2026-02"


def test_capacity_fits_all_in_single_batch() -> None:
    rows = [_row(f"t{i}", size=10, save_month=10, migration_cost=5, net_year=115) for i in range(5)]
    result = scheduling_core.schedule_batches(rows, monthly_capacity_gb=1000, base_year=2026, base_month=1)
    assert result["summary"]["batch_count"] == 1
    assert result["batches"][0]["table_count"] == 5
    assert result["summary"]["unscheduled_count"] == 0
