<script setup lang="ts">
import { computed } from 'vue'
import type { Plan, PlanItem } from '../../types/domain'
import { PLAN_SCHEDULE_LABELS, PLAN_STATUS_LABELS } from '../../constants/enums'
import { gb, money, months, shortTime } from '../../utils/format'
import RiskBadge from '../common/RiskBadge.vue'

const props = defineProps<{ plan: Plan; busy: boolean; canEdit: boolean; canExport: boolean }>()
const emit = defineEmits<{
  (event: 'remove', itemId: string): void
  (event: 'recheck'): void
  (event: 'submit'): void
  (event: 'export'): void
}>()

const isDraft = computed(() => props.plan.status === 'draft')
const blockers = computed(() =>
  props.plan.items.filter((item) => item.risk === 'block' && item.schedule_status === 'scheduled'),
)
const warns = computed(() => props.plan.items.filter((item) => item.risk === 'warn'))
const submitted = computed(() => props.plan.status === 'submitted')

// 排期表按（批次号、每 GB 净收益降序、回收期短者优先）展示，与后端装箱顺序一致
const scheduledRows = computed(() =>
  [...props.plan.items]
    .filter((item) => item.schedule_status === 'scheduled')
    .sort(
      (a, b) =>
        (a.batch_no ?? 0) - (b.batch_no ?? 0) ||
        b.net_per_gb - a.net_per_gb ||
        (a.payback_months ?? Number.POSITIVE_INFINITY) - (b.payback_months ?? Number.POSITIVE_INFINITY),
    ),
)
const unscheduledRows = computed(() =>
  [...props.plan.items]
    .filter((item) => item.schedule_status === 'unscheduled')
    .sort((a, b) => b.size_gb - a.size_gb),
)
const exportDisabled = computed(() => props.busy || !props.canExport || !scheduledRows.value.length)

function rowKey(item: PlanItem): string {
  return item.id
}
</script>

<template>
  <section class="wb">
    <header class="head">
      <div class="title-block">
        <div class="meta">
          <span class="tag">{{ plan.code }}</span>
          <span class="status mono" :class="`st-${plan.status}`">{{ PLAN_STATUS_LABELS[plan.status] }}</span>
          <span class="mono small">策略 v{{ plan.policy_version }} · 阈值 {{ plan.threshold }}</span>
          <span class="mono small">月容量 {{ gb(plan.monthly_capacity_gb) }} · {{ plan.start_year }} 年 {{ plan.start_month }} 月开工</span>
        </div>
        <h2 class="title">{{ plan.title }}</h2>
        <p class="sub mono">
          {{ plan.created_by }}
          <template v-if="plan.rechecked_at"> · 复检 {{ shortTime(plan.rechecked_at) }}</template>
          <template v-if="plan.submitted_at"> · 提交 {{ shortTime(plan.submitted_at) }}（{{ plan.submitted_by }}）</template>
        </p>
      </div>
      <div class="actions">
        <button class="btn btn-sm" type="button" :disabled="busy || !canEdit" @click="emit('recheck')">
          风险复检
        </button>
        <button
          class="btn btn-primary btn-sm"
          type="button"
          :disabled="busy || !canEdit || !isDraft || blockers.length > 0 || !scheduledRows.length"
          :title="blockers.length ? '先移除被阻断的表' : !scheduledRows.length ? '没有已排期的表可提交' : ''"
          @click="emit('submit')"
        >
          提交计划
        </button>
        <button
          class="btn btn-sm"
          type="button"
          :disabled="exportDisabled"
          :title="scheduledRows.length ? '' : '没有已排期的表可导出工单'"
          @click="emit('export')"
        >
          导出工单 CSV
        </button>
      </div>
    </header>

    <div class="summary">
      <div class="cell">
        <span class="label">排期 / 选中</span>
        <strong class="mono">{{ plan.scheduled_count }} / {{ plan.item_count }} 张</strong>
      </div>
      <div class="cell">
        <span class="label">批次数（月容量 {{ gb(plan.monthly_capacity_gb) }}）</span>
        <strong class="mono">{{ plan.batch_count }} 批</strong>
      </div>
      <div class="cell">
        <span class="label">排期总体积</span>
        <strong class="mono">{{ gb(plan.total_size_gb) }}</strong>
      </div>
      <div class="cell">
        <span class="label">满月月省</span>
        <strong class="mono">¥{{ money(plan.save_month) }}</strong>
      </div>
      <div class="cell">
        <span class="label">迁移成本</span>
        <strong class="mono">¥{{ money(plan.migration_cost) }}</strong>
      </div>
      <div class="cell emph">
        <span class="label">当年净省（按开工月折算）</span>
        <strong class="mono">¥{{ money(plan.current_year_net_save) }}</strong>
      </div>
      <div class="cell">
        <span class="label">首年净省（满年口径）</span>
        <strong class="mono">¥{{ money(plan.net_save_year) }}</strong>
      </div>
      <div class="cell">
        <span class="label">平均回收期</span>
        <strong class="mono">{{ months(plan.avg_payback) }}</strong>
      </div>
      <div class="cell">
        <span class="label">风险 / 排不下</span>
        <strong class="mono">
          <span class="blk">{{ plan.risk_block_count }} 阻断</span> ·
          <span class="wn">{{ plan.risk_warn_count }} 需确认</span> ·
          <span class="unsched">{{ plan.unscheduled_count }} 排不下</span>
        </strong>
      </div>
    </div>

    <p v-if="blockers.length" class="banner block mono">
      ■ {{ blockers.length }} 张被阻断：{{ blockers.map((item) => item.table_name).join('、') }} —— 冷存要求近 30
      天零访问，逐张移除后才能提交
    </p>
    <p v-else-if="warns.length" class="banner warn mono">
      ▲ {{ warns.length }} 张需报表 owner 书面确认：{{ warns.map((item) => item.table_name).join('、') }}
    </p>
    <p v-else-if="scheduledRows.length" class="banner ok mono">○ 风险闸门全部通过，可提交</p>

    <div v-if="plan.batches.length" class="batch-board">
      <div class="batch-head mono">
        <span class="label">分批排期</span>
        <span class="small">单表不可拆分 · 每批体积 ≤ 月容量 {{ gb(plan.monthly_capacity_gb) }} · 迁完次月起计节省</span>
      </div>
      <div class="batch-cards">
        <article v-for="batch in plan.batches" :key="batch.batch_no" class="batch-card">
          <header class="bc-top">
            <span class="bc-no mono">第 {{ batch.batch_no }} 批</span>
            <span class="bc-month mono">{{ batch.start_month }} 开工</span>
          </header>
          <p class="bc-fact mono"><span>表数</span><strong>{{ batch.table_count }} 张</strong></p>
          <p class="bc-fact mono"><span>批次体积</span><strong>{{ gb(batch.size_gb) }}</strong></p>
          <p class="bc-fact mono"><span>迁移成本</span><strong>¥{{ money(batch.migration_cost) }}</strong></p>
          <p class="bc-fact mono"><span>满月月省</span><strong>¥{{ money(batch.save_month) }}</strong></p>
          <p class="bc-fact bc-emph mono"><span>当年净省</span><strong>¥{{ money(batch.current_year_net_save) }}</strong></p>
        </article>
      </div>
    </div>

    <div class="table-wrap">
      <table class="scheduled-table">
        <thead>
          <tr>
            <th class="w-i">#</th>
            <th class="w-batch">批次 / 开工月</th>
            <th>库表</th>
            <th>温区</th>
            <th class="num">体积</th>
            <th class="num">每GB净收益</th>
            <th class="num">月省</th>
            <th class="num">回收期</th>
            <th class="num">首年净省</th>
            <th class="num">当年净省</th>
            <th>风险 / 原因</th>
            <th class="w-act"></th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="(item, index) in scheduledRows"
            :key="rowKey(item)"
            :class="{ blocked: item.risk === 'block' }"
          >
            <td class="mono idx">{{ String(index + 1).padStart(2, '0') }}</td>
            <td class="mono batchcell">
              <span class="bc-tag">第 {{ item.batch_no }} 批</span>
              <span class="bc-month-label">{{ item.start_month }}</span>
            </td>
            <td class="mono name">
              {{ item.table_name }}
              <span class="iname-cost mono">迁移成本 ¥{{ money(item.migration_cost) }}</span>
            </td>
            <td class="mono tiercell">
              {{ item.from_tier === 'hot' ? 'H' : item.from_tier === 'warm' ? 'W' : 'C' }} →
              {{ item.suggested_tier === 'hot' ? 'H' : item.suggested_tier === 'warm' ? 'W' : 'C' }}
            </td>
            <td class="num mono">{{ gb(item.size_gb) }}</td>
            <td class="num mono">¥{{ money(item.net_per_gb, 2) }}</td>
            <td class="num mono">¥{{ money(item.save_month) }}</td>
            <td class="num mono">{{ months(item.payback_months) }}</td>
            <td class="num mono">¥{{ money(item.net_save_year) }}</td>
            <td class="num mono emph-num">¥{{ money(item.current_year_net_save) }}</td>
            <td class="riskcell">
              <RiskBadge :risk="item.risk" :reason="item.reasons" />
              <span class="reason">{{ item.reasons }}</span>
            </td>
            <td class="w-act">
              <button
                v-if="isDraft && canEdit"
                class="btn btn-danger btn-sm"
                type="button"
                :disabled="busy"
                @click="emit('remove', item.id)"
              >
                移除
              </button>
              <span v-else-if="submitted" class="mono done">已提交</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="!scheduledRows.length" class="none mono">
        月度容量装不下任何表 —— 调大上方容量或移除超大表后再排期。
      </p>
    </div>

    <div v-if="unscheduledRows.length" class="unscheduled-block">
      <header class="uns-head mono">
        <span class="uns-tag">{{ PLAN_SCHEDULE_LABELS.unscheduled }}（{{ unscheduledRows.length }} 张，不计收益、不出工单）</span>
        <span class="small">单表体积超过月度容量 {{ gb(plan.monthly_capacity_gb) }}，需扩容或拆分区后另立计划</span>
      </header>
      <table class="unscheduled-table">
        <thead>
          <tr>
            <th>库表</th>
            <th class="num">体积 GB</th>
            <th class="num">满月月省</th>
            <th class="num">首年净省（参考）</th>
            <th>风险</th>
            <th class="w-act"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in unscheduledRows" :key="rowKey(item)" class="uns-row">
            <td class="mono name">{{ item.table_name }}</td>
            <td class="num mono">{{ gb(item.size_gb) }}</td>
            <td class="num mono">¥{{ money(item.save_month) }}</td>
            <td class="num mono muted">¥{{ money(item.net_save_year) }}</td>
            <td class="riskcell">
              <RiskBadge :risk="item.risk" :reason="item.reasons" />
            </td>
            <td class="w-act">
              <button
                v-if="isDraft && canEdit"
                class="btn btn-danger btn-sm"
                type="button"
                :disabled="busy"
                @click="emit('remove', item.id)"
              >
                移除
              </button>
              <span v-else class="mono unsched-status">{{ PLAN_SCHEDULE_LABELS.unscheduled }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-if="!plan.items.length" class="none mono">
      计划里还没有表 —— 回到「策略与试算」勾选候选表后再建一次计划。
    </p>
    <p v-if="plan.note" class="note mono">备注：{{ plan.note }}</p>
  </section>
</template>

<style scoped>
.wb {
  border: 1px solid var(--line);
  background: var(--frost-1);
}
.head {
  display: flex;
  justify-content: space-between;
  gap: 14px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line-strong);
  background: var(--frost-2);
  flex-wrap: wrap;
}
.meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.status {
  font-size: 10.5px;
  padding: 1px 7px;
  border: 1px solid currentColor;
  letter-spacing: 0.06em;
}
.st-draft {
  color: var(--warm);
}
.st-submitted {
  color: var(--ok);
}
.small {
  font-size: 10px;
  color: var(--slate-faint);
}
.title {
  font-size: var(--step-2);
  font-weight: 700;
  margin-top: 4px;
}
.sub {
  font-size: 10.5px;
  color: var(--slate-faint);
  margin-top: 2px;
}
.actions {
  display: flex;
  gap: 6px;
  align-items: flex-start;
  flex-wrap: wrap;
}
.summary {
  display: grid;
  grid-template-columns: repeat(9, minmax(0, 1fr));
  border-bottom: var(--hair);
}
.cell {
  padding: 9px 12px;
  border-right: var(--hair);
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.cell .label {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.cell:last-child {
  border-right: 0;
}
.cell strong {
  font-size: 15px;
  font-weight: 700;
  letter-spacing: -0.01em;
}
.cell.emph {
  background: var(--frost-0);
}
.cell.emph strong {
  color: var(--wood-deep);
}
.blk {
  color: var(--block);
}
.wn {
  color: var(--warn);
}
.unsched {
  color: var(--slate-faint);
}
.batch-board {
  border-bottom: var(--hair);
  background: var(--frost-0);
}
.batch-head {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 14px 6px;
  align-items: baseline;
  flex-wrap: wrap;
}
.batch-head .label {
  font-size: 11px;
  color: var(--slate);
  letter-spacing: 0.08em;
}
.batch-head .small {
  font-size: 9.8px;
  color: var(--slate-faint);
}
.batch-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(196px, 1fr));
  gap: 10px;
  padding: 0 14px 12px;
}
.batch-card {
  border: 1px solid var(--line);
  border-top: 3px solid var(--wood-pale, var(--wood));
  background: var(--frost-1);
  padding: 9px 11px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.bc-top {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 3px;
}
.bc-no {
  font-size: 12px;
  font-weight: 700;
  color: var(--wood-deep);
}
.bc-month {
  font-size: 10px;
  color: var(--slate-faint);
}
.bc-fact {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 10.5px;
  color: var(--slate-soft);
  margin: 0;
}
.bc-fact strong {
  color: var(--slate);
  font-weight: 600;
}
.bc-emph strong {
  color: var(--wood-deep);
}
.w-batch {
  width: 104px;
}
.batchcell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  white-space: nowrap;
}
.bc-tag {
  font-size: 10.5px;
  color: var(--wood-deep);
  font-weight: 600;
}
.bc-month-label {
  font-size: 9.8px;
  color: var(--slate-faint);
}
.emph-num {
  color: var(--wood-deep);
  font-weight: 600;
}
.unscheduled-block {
  border-top: var(--hair);
  background: var(--frost-0);
}
.uns-head {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  padding: 9px 14px 6px;
  align-items: baseline;
  flex-wrap: wrap;
}
.uns-tag {
  font-size: 11px;
  color: var(--block);
  letter-spacing: 0.06em;
  font-weight: 600;
}
.uns-head .small {
  font-size: 9.8px;
  color: var(--slate-faint);
}
.unscheduled-table {
  width: 100%;
}
.uns-row td {
  background: var(--hot-wash);
}
.muted {
  color: var(--slate-faint);
}
.unsched-status {
  font-size: 10px;
  color: var(--block);
}
.banner {
  margin: 0;
  padding: 8px 14px;
  font-size: 10.8px;
  border-bottom: var(--hair);
  line-height: 1.5;
}
.banner.block {
  background: var(--hot-wash);
  color: var(--block);
}
.banner.warn {
  background: var(--warm-wash);
  color: var(--warm);
}
.banner.ok {
  background: var(--ok-wash);
  color: var(--ok);
}
.table-wrap {
  overflow-x: auto;
}
table {
  min-width: 860px;
}
thead th {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--slate-faint);
  text-align: left;
  padding: 7px 9px;
  border-bottom: var(--hair);
  font-weight: 500;
  white-space: nowrap;
}
tbody td {
  padding: 6px 9px;
  border-bottom: var(--hair);
  font-size: 12px;
  vertical-align: top;
}
tbody tr.blocked {
  background: var(--hot-wash);
}
.idx {
  color: var(--slate-faint);
  font-size: 10.5px;
}
.name {
  min-width: 216px;
}
.iname-cost {
  display: block;
  font-size: 9.5px;
  color: var(--slate-faint);
  margin-top: 1px;
}
.tiercell {
  font-size: 10.5px;
  color: var(--slate-soft);
  white-space: nowrap;
}
.num {
  text-align: right;
  font-size: 11.5px;
  white-space: nowrap;
}
.riskcell {
  min-width: 210px;
}
.reason {
  display: block;
  font-size: 10px;
  color: var(--slate-soft);
  margin-top: 2px;
  line-height: 1.45;
}
.done {
  font-size: 10px;
  color: var(--ok);
}
.none {
  padding: 16px;
  font-size: 11.5px;
  color: var(--slate-faint);
}
.note {
  padding: 7px 14px;
  font-size: 10.5px;
  color: var(--slate-faint);
  border-top: var(--hair);
}
.w-i { width: 34px; }
.w-act { width: 74px; }
@media (max-width: 1200px) {
  .summary {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
@media (max-width: 700px) {
  .summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
