<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import { judgeApi, judgeSettingsApi } from '../api'
import type { JudgeRun, JudgeRunSummary, JudgeSettings } from '../api'
import { errorMessage, formatDate } from '../utils'

const props = defineProps<{ projectId: number; requirementId: number; generationId: string; refreshKey: number; disabled: boolean }>()
const emit = defineEmits<{ submitting: [value: boolean] }>()
const runs = ref<JudgeRunSummary[]>([])
const selected = ref<JudgeRun>()
const config = ref<JudgeSettings>()
const loading = ref(false)
const submitting = ref(false)
const loadError = ref('')
const actionError = ref('')
let sequence = 0
let disposed = false
let timer: ReturnType<typeof setTimeout> | undefined
let pending: { id: string; retryOf: string | null } | undefined
const labels = { queued: '排队中', running: '评估中', succeeded: '评估完成', failed: '评估失败', interrupted: '已中断' }
const active = computed(() => runs.value.find(run => run.status === 'queued' || run.status === 'running'))
const canSubmit = computed(() => !submitting.value && !props.disabled && !active.value && !loadError.value && !!config.value && config.value.effective_source !== 'unconfigured')
const latestFailure = computed(() => runs.value[0] && ['failed','interrupted'].includes(runs.value[0].status) ? runs.value[0] : undefined)
function schedule() {
  if (timer) clearTimeout(timer)
  if (!disposed) timer = setTimeout(() => { void refresh() }, active.value ? 1200 : 10_000)
}
async function refresh(pickId?: string) {
  if (timer) clearTimeout(timer)
  const token = ++sequence
  loading.value = true
  try {
    const [rows, settings] = await Promise.all([judgeApi.list(props.projectId,props.requirementId,props.generationId), judgeSettingsApi.get()])
    if (disposed || token !== sequence) return
    const previousDone = new Set(runs.value.filter(run => run.status === 'succeeded').map(run => run.id))
    const completed = rows.find(run => run.status === 'succeeded' && !previousDone.has(run.id))
    runs.value = rows
    config.value = settings
    loadError.value = ''
    const id = pickId || completed?.id || selected.value?.id || rows.find(run => run.status === 'succeeded')?.id || rows[0]?.id
    const detail = id ? await judgeApi.get(props.projectId,props.requirementId,props.generationId,id) : undefined
    if (!disposed && token === sequence) selected.value = detail
  } catch (cause) { if (!disposed && token === sequence) loadError.value = errorMessage(cause) }
  finally { if (!disposed && token === sequence) { loading.value = false; schedule() } }
}
async function submit(retryOf: string | null = null) {
  if (!canSubmit.value) return
  submitting.value = true
  emit('submitting', true)
  actionError.value = ''
  try {
    if (retryOf) {
      try { await ElMessageBox.confirm('将读取当前已保存的人工版本和当前 Judge 配置，创建新的评估记录。先前调用可能已计费；重试可能重复计费。', '重试 Judge 评估', { confirmButtonText: '重新评估', cancelButtonText: '取消' }) }
      catch { return }
    }
    if (!pending || pending.retryOf !== retryOf) pending = { id: crypto.randomUUID(), retryOf }
    const response = await judgeApi.submit(props.projectId,props.requirementId,props.generationId,pending.id,pending.retryOf)
    if (disposed) return
    pending = undefined
    runs.value = [response, ...runs.value.filter(run => run.id !== response.id)]
    if (!selected.value) selected.value = response
    await refresh(response.status === 'succeeded' ? response.id : undefined)
  } catch (cause) {
    if (!disposed) { actionError.value = `${errorMessage(cause)} 如提交超时，请刷新记录核对；核实提交会沿用同一请求标识。`; await refresh() }
  } finally { if (!disposed) { submitting.value = false; emit('submitting', false) } }
}
function caseTitle(id: string | null) { return selected.value?.input_snapshot.cases.find(item => item.case_id === id)?.fields.title || (id || '生成依据') }
watch(() => props.refreshKey, () => { void refresh() })
onMounted(() => { void refresh() })
onBeforeUnmount(() => { disposed = true; sequence += 1; if (timer) clearTimeout(timer); emit('submitting', false) })
</script>

<template>
  <section class="judge-panel" aria-labelledby="judge-title">
    <div class="panel-toolbar"><div><h3 id="judge-title">AI Judge 质量评估</h3><p>评估当前已保存的人工版本 · 独立记录 · 不改写用例</p></div><el-button type="primary" :loading="submitting" :disabled="!canSubmit" @click="submit()">{{ pending ? '核实评估提交' : '评估当前草稿' }}</el-button></div>
    <div class="judge-body">
      <p class="field-help">实际评估模型：{{ config?.effective_source === 'generation' ? '复用生成模型' : config?.effective_source === 'independent' ? '独立 Judge' : '未配置' }} · {{ config?.effective_model || (config?.effective_mock_mode ? 'Mock' : '—') }} <router-link to="/settings/judge">Judge 配置 →</router-link></p>
      <p class="field-help">每次评估会发送当前已保存的用例及生成时需求、功能点、知识/专项快照，单独调用一次模型，可能计费。三项均 ≥3 且无疑似幻觉标记才达到本模块复核门槛；评分不是人工结论，也不代表实测通过。</p>
      <el-alert v-if="loadError" :title="loadError" type="error" :closable="false" /><el-alert v-if="actionError" :title="actionError" type="error" :closable="false" />
      <p v-if="active" role="status">{{ labels[active.status] }} · {{ active.mock_mode ? 'Mock' : active.model }} · 可离开或刷新页面后继续查看。</p>
      <el-alert v-if="latestFailure" :title="`最近记录：${labels[latestFailure.status]}：${latestFailure.error_message}`" type="warning" :closable="false" />
      <el-button :loading="loading" :disabled="submitting" @click="refresh()">刷新评估记录</el-button>
      <div v-if="selected" class="judge-result">
        <h4>{{ labels[selected.status] }} · {{ formatDate(selected.created_at) }}</h4>
        <p class="field-help">{{ selected.model_source === 'generation' ? '复用生成模型' : '独立 Judge' }} · {{ selected.model || 'Mock' }} · {{ selected.prompt_version }} · {{ selected.rubric_version }}<span v-if="selected.elapsed_ms !== null"> · {{ selected.elapsed_ms }} ms</span></p>
        <el-alert v-if="selected.mock_mode || selected.input_snapshot.generation_source === 'mock'" title="本次评估或被评草稿包含 Mock，分数与门槛统计只验证流程，不代表真实质量。" type="warning" :closable="false" />
        <el-alert v-if="selected.outdated" title="草稿人工版本、评审状态或生成依据已变化。下方评分仅对应保存的输入快照，请重新评估当前版本。" type="warning" :closable="false" />
        <el-alert v-if="selected.input_snapshot.generation_outdated" title="本次评估针对历史生成依据；不能当作当前需求或知识库的验证结果。" type="warning" :closable="false" />
        <p v-if="selected.error_message">{{ selected.error_message }}</p>
        <details v-if="selected.input_snapshot.rule_findings.length" class="judge-rules"><summary>本地规则检查：{{ selected.input_snapshot.rule_findings.length }} 项待核对</summary><ul><li v-for="(finding,index) in selected.input_snapshot.rule_findings" :key="index">{{ caseTitle(finding.case_id) }}：{{ finding.message }}<span v-if="finding.related_case_id"> 关联：{{ caseTitle(finding.related_case_id) }}</span></li></ul><p class="field-help">{{ selected.input_snapshot.rules_version }} · 本地提示不自动删除用例或改变 AI 分数。</p></details>
        <template v-if="selected.report">
          <p role="status">已评估 {{ selected.report.checked_count }} 条 · 达到复核门槛 {{ selected.report.usable_count }} 条 · 疑似幻觉 {{ selected.report.suspect_count }} 条 · 平均 {{ selected.report.average_score.toFixed(2) }} / 5</p>
          <article v-for="item in selected.report.items" :key="item.case_id" class="judge-item"><h4>{{ caseTitle(item.case_id) }} <el-tag :type="item.usable ? 'success' : 'warning'">{{ item.usable ? '达到复核门槛' : '需进一步核对' }}</el-tag></h4>
            <p v-for="dimension in (['relevance','executability','verifiability'] as const)" :key="dimension"><strong>{{ { relevance:'相关性',executability:'可执行性',verifiability:'可验证性' }[dimension] }} {{ item[dimension].score }} / 5：</strong>{{ item[dimension].reason }}</p>
            <p v-if="!item.hallucinations.length" class="field-help">未标记疑似幻觉；仍需人工复核。</p><ul v-else><li v-for="(issue,index) in item.hallucinations" :key="index">疑似幻觉：{{ issue }}</li></ul>
            <ul v-if="item.suggestions.length"><li v-for="(suggestion,index) in item.suggestions" :key="index">建议：{{ suggestion }}</li></ul>
          </article>
        </template>
        <details class="judge-snapshot"><summary>评估输入快照与 Prompt</summary><p class="field-help">输入摘要：{{ selected.input_hash }}</p><h4>{{ selected.input_snapshot.requirement.title }}</h4><details><summary>生成时的需求与全部评估数据</summary><pre>{{ JSON.stringify(selected.input_snapshot,null,2) }}</pre></details><details><summary>本次 Judge Prompt</summary><pre>{{ selected.prompt_snapshot }}</pre></details></details>
      </div>
      <p v-else-if="!loading" class="field-help">尚无评估记录，生成草稿后可独立发起。评估不会重新生成用例。</p>
      <el-table v-if="runs.length" :data="runs" row-key="id" size="small"><el-table-column label="时间" min-width="150"><template #default="{ row }">{{ formatDate(row.created_at) }}</template></el-table-column><el-table-column label="状态" min-width="100"><template #default="{ row }">{{ labels[row.status as keyof typeof labels] }}{{ row.outdated ? ' · 已过期' : '' }}</template></el-table-column><el-table-column label="模型来源" min-width="110"><template #default="{ row }">{{ row.model_source === 'generation' ? '复用生成' : '独立' }}{{ row.mock_mode ? ' · Mock' : '' }}</template></el-table-column><el-table-column label="复核门槛" width="100"><template #default="{ row }">{{ row.usable_count === null ? '—' : `${row.usable_count} / ${row.checked_count}` }}</template></el-table-column><el-table-column label="操作" min-width="140"><template #default="{ row }"><el-button link :disabled="submitting" @click="refresh(row.id)">查看</el-button><el-button v-if="row.status === 'failed' || row.status === 'interrupted'" link type="primary" :disabled="!canSubmit" @click="submit(row.id)">重试评估</el-button></template></el-table-column></el-table>
    </div>
  </section>
</template>

<style scoped>
.judge-panel { border: 1px solid #dbe4ed; border-radius: 10px; margin: 20px 0; overflow: hidden; }
.judge-panel h3 { margin: 0; }
.judge-body { padding: 18px; }
.judge-result { margin: 18px 0; }
.judge-item { border-top: 1px solid #dbe4ed; padding: 14px 0; }
.judge-item h4 { margin-top: 0; }
.judge-rules, .judge-snapshot { margin: 16px 0; }
.judge-panel summary { cursor: pointer; color: #315b8f; }
.judge-panel pre { white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
