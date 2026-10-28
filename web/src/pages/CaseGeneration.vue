<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { caseGenerationsApi, featuresApi, requirementsApi, generationTasksApi, skillsApi } from '../api'
import type { CaseGenerateInput, CaseGeneration, CaseGenerationSummary, CaseStrategy, FeatureSheet, Requirement, GenerationTask, SkillId, SkillDefinition } from '../api'
import { errorMessage, formatDate } from '../utils'
import CaseReviewPanel from '../components/CaseReviewPanel.vue'
import KnowledgeEvidence from '../components/KnowledgeEvidence.vue'
import SkillEvidence from '../components/SkillEvidence.vue'
import JudgePanel from '../components/JudgePanel.vue'

const route = useRoute()
const projectId = Number(route.params.projectId)
const requirementId = Number(route.params.requirementId)
const requirement = ref<Requirement>()
const sheet = ref<FeatureSheet>()
const history = ref<CaseGenerationSummary[]>([])
const selected = ref<CaseGeneration>()
const featureId = ref('')
const strategy = ref<CaseStrategy>('full')
const skillIds = ref<SkillId[]>([])
const skillCatalog = ref<SkillDefinition[]>([])
const skillsLoading = ref(false)
const skillsError = ref('')
const useRag = ref(false)
const ragQuery = ref('')
const ragTopK = ref(3)
const ragRerank = ref(false)
const ragAllowEmpty = ref(false)
const loading = ref(false)
const generating = ref(false)
const reading = ref(false)
const reviewBusy = ref(false)
const judgeSubmitting = ref(false)
const reviewRevision = ref(0)
const error = ref('')
const actionError = ref('')
const pending = ref<CaseGenerateInput>()
const tasks = ref<GenerationTask[]>([])
const taskError = ref('')
const polling = ref(false)
const controlling = ref('')
const controlError = ref('')
let controlRevision = 0
let disposed = false
let pollTimer: ReturnType<typeof setTimeout> | undefined
const activeTask = computed(() => tasks.value.find(task => task.status === 'queued' || task.status === 'running'))
const taskNames = { queued: '排队中', running: '运行中', paused: '已暂停', succeeded: '已完成', failed: '失败', interrupted: '已中断' }
const taskStages: Record<string, string> = { queued: '等待执行', prepare: '准备输入与可选知识检索', generate: '模型生成', validate: '结构与策略校验', save: '保存草稿', completed: '完成' }

function schedulePoll(force = false) {
  clearTimeout(pollTimer)
  if (!disposed && (force || activeTask.value || taskError.value)) pollTimer = setTimeout(refreshTasks, 1500)
}
async function refreshTasks() {
  if (disposed || polling.value || controlling.value) return
  const revision = controlRevision
  polling.value = true
  try {
    const previous = new Set(tasks.value.filter(task => task.status === 'succeeded').map(task => task.id))
    const loaded = await generationTasksApi.list(projectId, requirementId)
    if (disposed || revision !== controlRevision) return
    tasks.value = loaded
    taskError.value = ''
    const completed = loaded.find(task => task.status === 'succeeded' && !previous.has(task.id))
    if (completed) {
      history.value = await caseGenerationsApi.list(projectId, requirementId)
      if (!disposed && !reviewBusy.value && !reading.value && !loading.value) {
        const result = await caseGenerationsApi.get(projectId, requirementId, completed.id)
        if (!disposed && !reviewBusy.value && !reading.value && !loading.value) selected.value = result
      }
    }
  } catch (cause) {
    if (!disposed) taskError.value = errorMessage(cause)
  } finally {
    polling.value = false
    schedulePoll(revision !== controlRevision)
  }
}
const busy = computed(() => loading.value || generating.value || reading.value || reviewBusy.value || judgeSubmitting.value || !!controlling.value)
const available = computed(() => sheet.value?.items.filter(item => item.included) ?? [])
const canGenerate = computed(() => !!sheet.value?.confirmed && !sheet.value.stale && !!featureId.value && !busy.value && !activeTask.value && !taskError.value && (!useRag.value || Array.from(ragQuery.value).length <= 200) && (!skillIds.value.length || (!skillsLoading.value && !skillsError.value)))
const stageNames: Record<string, string> = { prepare: '准备输入与可选知识检索', generate: '生成用例', validate: '结构与策略校验' }

async function controlTask(task: GenerationTask, action: 'pause' | 'resume') {
  if (controlling.value || generating.value) return
  controlling.value = task.id
  controlRevision += 1
  controlError.value = ''
  try {
    let retryUncertain = false
    if (action === 'resume' && (task.requires_model_retry || task.requires_knowledge_retry)) {
      try {
        await ElMessageBox.confirm([task.requires_model_retry ? '上次生成模型调用没有可复用结果。' : '', task.requires_knowledge_retry ? '上次知识检索没有可复用快照。' : '', '先前调用可能已计费，恢复将重新调用对应服务，是否继续？'].join(''), '确认重复调用服务', {
          confirmButtonText: '允许重新调用并恢复', cancelButtonText: '取消', type: 'warning',
        })
      } catch { return }
      retryUncertain = true
    }
    const result = action === 'pause'
      ? await generationTasksApi.pause(projectId, requirementId, task.id)
      : await generationTasksApi.resume(projectId, requirementId, task.id, retryUncertain, retryUncertain)
    if (disposed) return
    tasks.value = tasks.value.map(item => item.id === result.id ? result : item)
    ElMessage.success(action === 'pause'
      ? (result.status === 'paused' ? '任务已暂停' : '已请求暂停，将在当前节点完成并保存检查点后停止')
      : '任务已恢复排队，将复用已保存的节点结果')
    schedulePoll()
  } catch (cause) {
    if (!disposed) controlError.value = errorMessage(cause)
    await refreshTasks()
  } finally {
    controlling.value = ''
    controlRevision += 1
    schedulePoll(true)
  }
}

async function loadRun(id: string) {
  if (busy.value) return
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
    const [document, features, runs, loadedTasks] = await Promise.all([
      requirementsApi.get(projectId, requirementId), featuresApi.get(projectId, requirementId),
      caseGenerationsApi.list(projectId, requirementId), generationTasksApi.list(projectId, requirementId),
    ])
    requirement.value = document
    sheet.value = features
    history.value = runs
    tasks.value = loadedTasks
    taskError.value = ''
    if (!features.items.some(item => item.id === featureId.value && item.included)) featureId.value = available.value[0]?.id ?? ''
    selected.value = runs.length ? await caseGenerationsApi.get(projectId, requirementId, runs[0]!.id) : undefined
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
    schedulePoll()
  }
}

async function loadSkills() {
  if (skillsLoading.value) return
  skillsLoading.value = true
  skillsError.value = ''
  try { const items = await skillsApi.list(); if (!disposed) skillCatalog.value = items }
  catch (cause) { if (!disposed) skillsError.value = errorMessage(cause) }
  finally { if (!disposed) skillsLoading.value = false }
}
function skillName(id: string) { return skillCatalog.value.find(item => item.id === id)?.name || id }

async function generate() {
  if (!canGenerate.value || !sheet.value) return
  generating.value = true
  actionError.value = ''
  try {
    pending.value ??= {
      request_id: crypto.randomUUID(), feature_id: featureId.value, expected_revision: sheet.value.revision,
      input_hash: sheet.value.current_hash, strategy: strategy.value, skill_ids: [...skillIds.value],
      use_rag: useRag.value, rag_query: ragQuery.value, rag_top_k: ragTopK.value, rag_rerank: ragRerank.value, rag_allow_empty: ragAllowEmpty.value,
    }
    const task = await generationTasksApi.submit(projectId, requirementId, pending.value)
    if (disposed) return
    tasks.value = [task, ...tasks.value.filter(item => item.id !== task.id)]
    pending.value = undefined
    if (task.status === 'failed' || task.status === 'interrupted') {
      ElMessage.info('原请求的任务已结束，可核对记录后重新发起')
    } else {
      ElMessage.success('任务已保存，可离开页面后返回查看')
    }
    schedulePoll()
    if (task.status === 'succeeded') {
      history.value = await caseGenerationsApi.list(projectId, requirementId)
      selected.value = await caseGenerationsApi.get(projectId, requirementId, task.id)
    }
  } catch (cause) {
    actionError.value = errorMessage(cause)
  } finally {
    generating.value = false
  }
}

watch([featureId, strategy, () => skillIds.value.join(','), useRag, ragQuery, ragTopK, ragRerank, ragAllowEmpty], () => { pending.value = undefined })
onBeforeRouteLeave(() => !generating.value && !reviewBusy.value && !judgeSubmitting.value && !controlling.value)
onBeforeRouteUpdate(() => !generating.value && !reviewBusy.value && !judgeSubmitting.value && !controlling.value)
function warnBeforeUnload(event: BeforeUnloadEvent) {
  if (generating.value || reviewBusy.value || judgeSubmitting.value || controlling.value) { event.preventDefault(); event.returnValue = '' }
}
onMounted(() => { loadPage(); loadSkills(); window.addEventListener('beforeunload', warnBeforeUnload) })
onBeforeUnmount(() => { disposed = true; clearTimeout(pollTimer); window.removeEventListener('beforeunload', warnBeforeUnload) })
</script>

<template>
  <main>
    <router-link :to="`/projects/${projectId}/requirements`" class="back-link">← 返回需求列表</router-link>
    <div class="page-heading"><div><div class="eyebrow">TEST CASE DRAFTS</div><h1>{{ requirement?.title || '用例草稿' }}</h1><p>从已确认的功能点开始，逐个生成可核对的测试用例。</p></div><el-button :loading="loading" :disabled="generating || reading || reviewBusy || judgeSubmitting" @click="loadPage">重新加载</el-button></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <div v-else-if="loading" class="loading-state"><el-skeleton :rows="6" animated /></div>
    <template v-else-if="requirement">
      <el-alert v-if="!sheet?.confirmed || sheet.stale" title="请先核对并确认功能点范围，再生成新用例。历史草稿仍可查看。" type="warning" :closable="false" show-icon class="feature-notice" />
      <section class="project-panel case-generator">
        <div class="panel-toolbar"><div><h2>生成用例草稿</h2><p>每次提交一个功能点，后台执行并保存进度</p></div><router-link :to="`/projects/${projectId}/requirements/${requirementId}/features`" class="row-link">核对功能点 →</router-link></div>
        <div class="case-generator-body">
          <el-form label-position="top" :disabled="busy" class="case-generate-form">
            <el-form-item label="已纳入范围的功能点"><el-select v-model="featureId" placeholder="请选择功能点" aria-label="已纳入范围的功能点"><el-option v-for="item in available" :key="item.id" :label="`${item.module} / ${item.feature}`" :value="item.id" /></el-select></el-form-item>
            <el-form-item label="生成策略"><el-select v-model="strategy" aria-label="生成策略"><el-option label="完整用例 · 3–8 条" value="full" /><el-option label="快速冒烟 · 1–3 条" value="quick" /></el-select></el-form-item>
            <el-button type="primary" :loading="generating" :disabled="!canGenerate" @click="generate">{{ pending ? '核实本次提交' : '生成用例草稿' }}</el-button>
          </el-form>
          <section class="specialist-options" aria-labelledby="specialist-title"><h3 id="specialist-title">专项 Skill（可选）</h3><p class="field-help">可单独或同时开启；未选择时使用普通生成。专项复用本次生成调用，不增加调用次数，提示词长度和费用可能增加。</p><el-alert v-if="skillsError" :title="`专项列表加载失败：${skillsError}`" type="warning" :closable="false"><el-button :disabled="skillsLoading" @click="loadSkills">重试专项列表</el-button></el-alert><el-checkbox-group v-model="skillIds" :disabled="busy || !!activeTask || skillsLoading || !!skillsError"><div v-for="skill in skillCatalog" :key="skill.id" class="specialist-option"><el-checkbox :value="skill.id">{{ skill.name }}</el-checkbox><el-tag size="small" type="info">{{ skill.version }}</el-tag><p class="field-help">{{ skill.description }} {{ skill.focus.join(' / ') }}</p></div></el-checkbox-group></section>
          <div class="rag-options"><el-checkbox v-model="useRag" :disabled="busy || !!activeTask">使用项目知识 RAG</el-checkbox><router-link :to="`/projects/${projectId}/knowledge`">管理项目知识 →</router-link><template v-if="useRag"><el-input v-model="ragQuery" aria-label="生成知识查询" placeholder="留空按功能点自动构造查询；最多 200 字符" :disabled="busy || !!activeTask" maxlength="200" /><label class="field-help">注入分块数 <el-select v-model="ragTopK" aria-label="注入知识分块数" style="width:110px" :disabled="busy || !!activeTask"><el-option v-for="n in [1,2,3,4,5]" :key="n" :value="n" :label="`${n} 块`" /></el-select></label><el-checkbox v-model="ragRerank" :disabled="busy || !!activeTask">知识检索使用 Rerank</el-checkbox><el-checkbox v-model="ragAllowEmpty" :disabled="busy || !!activeTask">检索无命中或不可用时，允许无知识继续生成</el-checkbox><p class="field-help">在准备输入阶段检索当前项目知识，最多注入 5 个分块。真实检索可能调用 Embedding、Rerank，再把入选正文发送给生成模型。默认无候选时停止；主动允许无知识继续时，会记录降级原因且不声明知识引用。有快照的恢复会复用资料，未知调用结果需确认费用后重试。</p></template></div>
          <p class="field-help">使用已保存的模型配置，发送当前需求与所选功能点。任务提交成功后可离开或刷新页面；服务重启时未完成任务标为中断，可核对检查点后手动恢复。<router-link to="/settings">模型设置</router-link></p>
          <el-alert v-if="actionError" :title="actionError" type="error" :closable="false" show-icon class="test-result" />
        </div>
      </section>

      <section class="project-panel draft-panel" aria-labelledby="task-title">
        <div class="panel-toolbar"><div><h2 id="task-title">生成任务 <span>{{ tasks.length }}</span></h2><p>暂停在节点边界生效；正在检索或生成时会等待当前节点结束并保存结果。恢复复用检查点，新生成会创建另一任务。</p></div><el-button :loading="polling" :disabled="loading || !!controlling" @click="refreshTasks">刷新任务</el-button></div>
        <el-alert v-if="controlError" :title="controlError" type="error" :closable="false" show-icon />
        <el-alert v-if="taskError" :title="`任务状态暂时无法更新：${taskError} 正在重试，请勿重复提交。`" type="warning" :closable="false" show-icon />
        <div v-if="activeTask" class="task-progress" role="status"><p>{{ activeTask.feature_name }} · {{ activeTask.pause_requested ? '暂停请求已记录，等待当前节点结束' : taskNames[activeTask.status] }} · {{ taskStages[activeTask.stage] || activeTask.stage }}</p><el-progress :percentage="activeTask.progress" /></div>
        <div v-if="!tasks.length" class="empty-state"><p>尚未提交后台生成任务，历史草稿仍保留在下方。</p></div>
        <div v-for="task in tasks" :key="task.id" class="task-row">
          <div class="task-row-heading"><strong>{{ task.feature_name }} · {{ taskNames[task.status] }}{{ task.status === 'running' && task.pause_requested ? '（等待暂停）' : '' }}</strong><span>{{ task.source === 'mock' ? 'Mock 示例' : task.model }} · {{ task.strategy === 'quick' ? '冒烟' : '完整' }} · {{ formatDate(task.created_at) }}</span><el-button v-if="task.status === 'queued' || task.status === 'running'" link type="warning" :loading="controlling === task.id" :disabled="!!controlling || task.pause_requested" @click="controlTask(task, 'pause')">{{ task.pause_requested ? '已请求暂停' : '暂停' }}</el-button><el-button v-if="task.can_resume" link type="primary" :loading="controlling === task.id" :disabled="busy || !!taskError" @click="controlTask(task, 'resume')">{{ task.requires_model_retry || task.requires_knowledge_retry ? '确认后恢复' : '恢复任务' }}</el-button><el-button v-if="task.status === 'succeeded'" link type="primary" :disabled="busy" @click="loadRun(task.id)">查看草稿</el-button></div>
          <p class="field-help">专项：{{ task.skill_ids.length ? task.skill_ids.map(skillName).join(' / ') : '普通生成' }}</p>
          <p class="field-help">{{ taskStages[task.stage] || task.stage }} · 阶段进度 {{ task.progress }}% · 已完成节点：{{ task.completed_nodes.map(node => stageNames[node] || node).join(' → ') || '无' }}</p>
          <p class="field-help">检查点已保存：{{ task.checkpoint_nodes.map(node => stageNames[node] || node).join(' → ') || '尚无完成节点' }}{{ task.status === 'paused' ? ' · 后台已停止执行，点击恢复后继续' : '' }}</p>
          <p v-if="task.resume_block_reason" class="field-help">无法恢复：{{ task.resume_block_reason }}</p>
          <p v-if="task.requires_knowledge_retry && task.can_resume" class="task-error">上次知识检索未留下快照，恢复前需确认重新检索。</p>
          <p v-if="task.requires_model_retry && task.can_resume" class="task-error">上次模型请求未留下可复用结果，恢复前需确认重新调用。</p>
          <p v-if="task.error_message" class="task-error">{{ task.error_message }}</p>
          <details class="field-help"><summary>任务标识</summary><code>{{ task.id }}</code></details>
        </div>
      </section>

      <section v-if="selected" class="project-panel draft-panel" aria-labelledby="draft-title">
        <div class="panel-toolbar"><div><h2 id="draft-title">{{ selected.feature_name }} <span>{{ selected.case_count }} 条草稿</span></h2><p>{{ formatDate(selected.created_at) }} · {{ selected.strategy === 'quick' ? '快速冒烟' : '完整用例' }} · {{ selected.source === 'mock' ? '生成环节 Mock' : selected.model }}</p></div><el-tag type="info">人工评审</el-tag></div>
        <div class="draft-panel-body">
          <el-alert v-if="selected.outdated" title="该草稿来自历史输入，当前需求、功能点或知识库已变化。可展开生成依据核对。" type="warning" :closable="false" show-icon class="feature-notice" />
          <SkillEvidence :generation="selected" />
          <KnowledgeEvidence :generation="selected" :project-id="projectId" />
          <CaseReviewPanel :key="selected.id" :project-id="projectId" :requirement-id="requirementId" :generation="selected" @busy="reviewBusy = $event" @changed="reviewRevision += 1" />
          <JudgePanel :key="`judge-${selected.id}`" :project-id="projectId" :requirement-id="requirementId" :generation-id="selected.id" :refresh-key="reviewRevision" :disabled="reviewBusy || generating || !!controlling" @submitting="judgeSubmitting = $event" />
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
          <el-table-column label="专项" min-width="150"><template #default="{ row }">{{ row.skill_names?.length ? row.skill_names.join(' / ') : '普通' }}</template></el-table-column>
          <el-table-column prop="case_count" label="条数" width="80" />
          <el-table-column label="来源" width="100"><template #default="{ row }">{{ row.source === 'mock' ? 'Mock' : 'AI' }}</template></el-table-column>
          <el-table-column label="操作" width="95" align="right"><template #default="{ row }"><el-button link type="primary" :disabled="busy" @click="loadRun(row.id)">查看</el-button></template></el-table-column>
        </el-table>
      </section>
    </template>
  </main>
</template>

<style scoped>
.specialist-options { margin: 18px 0; }
.specialist-option { margin: 8px 0; padding: 10px 14px; border: 1px solid #e1e7ef; border-radius: 8px; }
.specialist-option .el-tag { margin-left: 12px; }
.rag-options { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin: 14px 0; }
.rag-options > .el-input, .rag-options > p { flex-basis: 100%; }
</style>
