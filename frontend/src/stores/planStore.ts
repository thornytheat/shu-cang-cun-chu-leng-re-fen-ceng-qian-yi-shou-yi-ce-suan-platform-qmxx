import { defineStore } from 'pinia'
import {
  addPlanItems,
  createPlan,
  downloadTickets,
  fetchPlan,
  fetchPlans,
  recheckPlan,
  removePlanItem,
  submitPlan,
} from '../api/plans'
import type { Plan } from '../types/domain'

interface PlanState {
  list: Plan[]
  current: Plan | null
  loading: boolean
  busy: boolean
  error: string
  notice: string
}

export const usePlanStore = defineStore('plan', {
  state: (): PlanState => ({
    list: [],
    current: null,
    loading: false,
    busy: false,
    error: '',
    notice: '',
  }),
  actions: {
    syncList() {
      if (!this.current) return
      this.list = this.list.map((plan) => (plan.id === this.current?.id ? { ...this.current } : plan))
    },
    async loadList() {
      this.loading = true
      this.error = ''
      try {
        const result = await fetchPlans()
        this.list = result.items
        if (!this.current && this.list.length) {
          await this.open(this.list[0].id)
        }
      } catch (error) {
        this.error = (error as { message?: string }).message ?? '加载计划失败'
      } finally {
        this.loading = false
      }
    },
    async open(id: string) {
      this.busy = true
      this.error = ''
      try {
        this.current = await fetchPlan(id)
      } catch (error) {
        this.error = (error as { message?: string }).message ?? '加载计划失败'
      } finally {
        this.busy = false
      }
    },
    async create(
      title: string,
      threshold: number,
      tableIds: string[],
      capacityGb: number,
      startMonth: number,
    ) {
      this.busy = true
      this.error = ''
      this.notice = ''
      try {
        const plan = await createPlan({
          title,
          threshold,
          monthly_capacity_gb: capacityGb,
          start_month: startMonth,
          table_ids: tableIds,
        })
        this.current = plan
        this.list = [plan, ...this.list]
        this.notice =
          `已创建 ${plan.code}：排入 ${plan.scheduled_count} 张 / ${plan.batch_count} 批` +
          (plan.unscheduled_count ? `，${plan.unscheduled_count} 张超月度容量排不下（不计收益）` : '')
        return plan
      } catch (error) {
        this.error = (error as { message?: string }).message ?? '建计划失败'
        return null
      } finally {
        this.busy = false
      }
    },
    async addItems(tableIds: string[]) {
      if (!this.current) return
      this.busy = true
      try {
        this.current = await addPlanItems(this.current.id, tableIds)
        this.notice = `已追加 ${tableIds.length} 张表`
        this.syncList()
      } catch (error) {
        this.error = (error as { message?: string }).message ?? '追加失败'
      } finally {
        this.busy = false
      }
    },
    async remove(itemId: string) {
      if (!this.current) return
      this.busy = true
      try {
        this.current = await removePlanItem(this.current.id, itemId)
        this.notice = '已从计划中移除该表'
        this.syncList()
      } catch (error) {
        this.error = (error as { message?: string }).message ?? '移除失败'
      } finally {
        this.busy = false
      }
    },
    async recheck() {
      if (!this.current) return
      this.busy = true
      this.error = ''
      try {
        this.current = await recheckPlan(this.current.id)
        const plan = this.current
        this.notice = `风险复检完成：${plan.risk_block_count} 张阻断 / ${plan.risk_warn_count} 张需确认`
        this.syncList()
      } catch (error) {
        this.error = (error as { message?: string }).message ?? '复检失败'
      } finally {
        this.busy = false
      }
    },
    async submit() {
      if (!this.current) return
      this.busy = true
      this.error = ''
      try {
        this.current = await submitPlan(this.current.id)
        const plan = this.current
        this.notice =
          `已提交 ${plan.scheduled_count} 张 / ${plan.batch_count} 批，当年净省 ¥` +
          `${plan.current_year_net_save.toLocaleString('zh-CN')}` +
          (plan.unscheduled_count ? `；${plan.unscheduled_count} 张排不下未开工单` : '')
        this.syncList()
      } catch (error) {
        this.error = (error as { message?: string }).message ?? '提交失败'
      } finally {
        this.busy = false
      }
    },
    async exportCsv() {
      if (!this.current) return null
      this.busy = true
      this.error = ''
      try {
        const result = await downloadTickets(this.current.id)
        this.notice = `已导出工单 CSV：${result.rows} 行`
        return result
      } catch (error) {
        this.error = (error as { message?: string }).message ?? '导出失败'
        return null
      } finally {
        this.busy = false
      }
    },
  },
})
