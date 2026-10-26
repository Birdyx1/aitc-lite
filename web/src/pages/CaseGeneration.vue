<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { caseGenerationsApi, featuresApi, requirementsApi, settingsApi } from '../api'
import type { CaseGenerateInput, CaseGeneration, CaseGenerationSummary, CaseStrategy, FeatureSheet, Requirement } from '../api'
import { errorMessage, formatDate } from '../utils'

const route = useRoute()
const projectId = Number(route.params.projectId)
const requirementId = Number(route.params.requirementId)
const requirement = ref<Requirement>()
const sheet = ref<FeatureSheet>()
const history = ref<CaseGenerationSummary[]>([])
const selected = ref<CaseGeneration>()
const featureId = ref('')
const strategy = ref<CaseStrategy>('full')
const loading = ref(false)
const generating = ref(false)
const reading = ref(false)
const error = ref('')
const actionError = ref('')
const pending = ref<CaseGenerateInput>()
const busy = computed(() => loading.value || generating.value || reading.value)
const available = computed(() => sheet.value?.items.filter(item => item.included) ?? [])
const canGenerate = computed(() => !!sheet.value?.confirmed && !sheet.value.stale && !!featureId.value && !busy.value)
const typeNames = { functional: '功能', boundary: '边界', exception: '异常' }
const stageNames: Record<string, string> = { prepare: '准备输入', generate: '生成用例', validate: '结构与策略校验' }

async function loadRun(id: string) {
  if (generating.value || reading.value) return
  reading.value = true
  actionError.value = ''
  try {
    selected.value = await caseGenerationsApi.get(projectId, requirementId, id)
  } catch (cause) {
    actionError.value = errorMessage(cause)
  } finally {
    reading.value = false
  }
}

async function loadPage() {
  if (busy.value) return
  loading.value = true
  error.value = ''
  actionError.value = ''
  try {
    if (![projectId, requirementId].every(id => Number.isSafeInteger(id) && id > 0)) throw new Error('项目或需求地址无效。')
    const [document, features, runs] = await Promise.all([
      requirementsApi.get(projectId, requirementId), featuresApi.get(projectId, requirementId),
      caseGenerationsApi.list(projectId, requirementId),
    ])
    requirement.value = document
    sheet.value = features
    history.value = runs
    if (!features.items.some(item => item.id === featureId.value && item.included)) featureId.value = available.value[0]?.id ?? ''
    selected.value = runs.length ? await caseGenerationsApi.get(projectId, requirementId, runs[0]!.id) : undefined
    pending.value = undefined
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

async function generate() {
  if (!canGenerate.value || !sheet.value) return
  generating.value = true
  actionError.value = ''
  try {
    const config = await settingsApi.get()
    if (!config.configured) throw new Error('请先在模型设置中保存生成模型配置。')
    pending.value ??= {
      request_id: crypto.randomUUID(), feature_id: featureId.value, expected_revision: sheet.value.revision,
      input_hash: sheet.value.current_hash, strategy: strategy.value,
    }
    const result = await caseGenerationsApi.generate(projectId, requirementId, pending.value, config.timeout_seconds)
    selected.value = result
    history.value = [result, ...history.value.filter(item => item.id !== result.id)]
    pending.value = undefined
    ElMessage.success(`已保存 ${result.case_count} 条用例草稿`)
  } catch (cause) {
    actionError.value = errorMessage(cause)
  } finally {
    generating.value = false
  }
}

watch([featureId, strategy], () => { pending.value = undefined })
onBeforeRouteLeave(() => !generating.value)
onBeforeRouteUpdate(() => !generating.value)
function warnBeforeUnload(event: BeforeUnloadEvent) {
  if (generating.value) { event.preventDefault(); event.returnValue = '' }
}
onMounted(() => { loadPage(); window.addEventListener('beforeunload', warnBeforeUnload) })
onBeforeUnmount(() => window.removeEventListener('beforeunload', warnBeforeUnload))
</script>

<template>
  <main>
    <router-link :to="`/projects/${projectId}/requirements`" class="back-link">← 返回需求列表</router-link>
    <div class="page-heading"><div><div class="eyebrow">TEST CASE DRAFTS</div><h1>{{ requirement?.title || '用例草稿' }}</h1><p>从已确认的功能点开始，逐个生成可核对的测试用例。</p></div><el-button :loading="loading" :disabled="generating || reading" @click="loadPage">重新加载</el-button></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <div v-else-if="loading" class="loading-state"><el-skeleton :rows="6" animated /></div>
    <template v-else-if="requirement">
      <el-alert v-if="!sheet?.confirmed || sheet.stale" title="请先核对并确认功能点范围，再生成新用例。历史草稿仍可查看。" type="warning" :closable="false" show-icon class="feature-notice" />
      <section class="project-panel case-generator">
        <div class="panel-toolbar"><div><h2>生成用例草稿</h2><p>每次生成一个功能点，校验通过后自动保存</p></div><router-link :to="`/projects/${projectId}/requirements/${requirementId}/features`" class="row-link">核对功能点 →</router-link></div>
        <div class="case-generator-body">
          <el-form label-position="top" :disabled="busy" class="case-generate-form">
            <el-form-item label="已纳入范围的功能点"><el-select v-model="featureId" placeholder="请选择功能点" aria-label="已纳入范围的功能点"><el-option v-for="item in available" :key="item.id" :label="`${item.module} / ${item.feature}`" :value="item.id" /></el-select></el-form-item>
            <el-form-item label="生成策略"><el-select v-model="strategy" aria-label="生成策略"><el-option label="完整用例 · 3–8 条" value="full" /><el-option label="快速冒烟 · 1–3 条" value="quick" /></el-select></el-form-item>
            <el-button type="primary" :loading="generating" :disabled="!canGenerate" @click="generate">{{ pending ? '重试本次生成' : '生成用例草稿' }}</el-button>
          </el-form>
          <p class="field-help">使用已保存的模型配置，发送当前需求与所选功能点。生成期间请等待；失败可重试，已有草稿保留。<router-link to="/settings">模型设置</router-link></p>
          <el-alert v-if="actionError" :title="actionError" type="error" :closable="false" show-icon class="test-result" />
        </div>
      </section>

      <section v-if="selected" class="project-panel draft-panel" aria-labelledby="draft-title">
        <div class="panel-toolbar"><div><h2 id="draft-title">{{ selected.feature_name }} <span>{{ selected.case_count }} 条草稿</span></h2><p>{{ formatDate(selected.created_at) }} · {{ selected.strategy === 'quick' ? '快速冒烟' : '完整用例' }} · {{ selected.source === 'mock' ? 'Mock 示例（未联网）' : selected.model }}</p></div><el-tag type="warning">待人工评审</el-tag></div>
        <div class="draft-panel-body">
          <el-alert v-if="selected.outdated" title="该草稿来自历史输入，当前需求或功能点已变化。可展开生成依据核对。" type="warning" :closable="false" show-icon class="feature-notice" />
          <article v-for="(draft, index) in selected.cases" :key="draft.id" class="case-card">
            <div class="case-card-title"><h3>{{ index + 1 }}. {{ draft.title }}</h3><span>{{ draft.priority }} · {{ typeNames[draft.case_type] }}</span><el-tag v-if="draft.is_smoke" size="small">冒烟</el-tag></div>
            <dl><dt>前置条件</dt><dd>{{ draft.precondition || '无' }}</dd><dt>操作步骤</dt><dd><ol><li v-for="(step, stepIndex) in draft.steps" :key="stepIndex">{{ step }}</li></ol></dd><dt>预期结果</dt><dd>{{ draft.expected_result }}</dd></dl>
          </article>
          <details class="generation-evidence"><summary>生成依据与过程</summary><p>{{ selected.graph_nodes.map(node => stageNames[node] || node).join(' → ') }}</p><p>提示词版本：{{ selected.prompt_version }} · 功能点清单版本：{{ selected.sheet_revision }}</p><h4>{{ selected.feature_snapshot.module }} / {{ selected.feature_snapshot.feature }}</h4><pre>{{ selected.feature_snapshot.description }}
验收标准：{{ selected.feature_snapshot.acceptance_criteria || '未提供' }}
约束：{{ selected.feature_snapshot.constraints || '未提供' }}</pre><details><summary>生成时的需求原文</summary><pre>{{ selected.requirement_snapshot.content }}</pre></details></details>
        </div>
      </section>
      <div v-else class="project-panel empty-state draft-panel"><h3>还没有用例草稿</h3><p>确认功能点后，选择一个功能点生成第一批草稿。</p></div>

      <section class="project-panel draft-panel">
        <div class="panel-toolbar"><div><h2>历史生成记录 <span>{{ history.length }}</span></h2><p>每次成功生成独立保存，新的结果不覆盖旧草稿</p></div></div>
        <el-table :data="history" row-key="id" class="project-table">
          <el-table-column label="时间" width="160"><template #default="{ row }">{{ formatDate(row.created_at) }}</template></el-table-column>
          <el-table-column prop="feature_name" label="功能点" min-width="220" />
          <el-table-column label="策略" width="100"><template #default="{ row }">{{ row.strategy === 'quick' ? '冒烟' : '完整' }}</template></el-table-column>
          <el-table-column prop="case_count" label="条数" width="80" />
          <el-table-column label="来源" width="100"><template #default="{ row }">{{ row.source === 'mock' ? 'Mock' : 'AI' }}</template></el-table-column>
          <el-table-column label="操作" width="95" align="right"><template #default="{ row }"><el-button link type="primary" :disabled="busy" @click="loadRun(row.id)">查看</el-button></template></el-table-column>
        </el-table>
      </section>
    </template>
  </main>
</template>
