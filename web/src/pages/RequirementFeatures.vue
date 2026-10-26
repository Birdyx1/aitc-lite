<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { featuresApi, requirementsApi, settingsApi } from '../api'
import type { FeatureItem, FeatureSheet, FeatureSource, Requirement } from '../api'
import { errorMessage } from '../utils'

const route = useRoute()
const projectId = Number(route.params.projectId)
const requirementId = Number(route.params.requirementId)
const requirement = ref<Requirement>()
const items = ref<FeatureItem[]>([])
const revision = ref(0)
const currentHash = ref('')
const source = ref<FeatureSource>('manual')
const model = ref('')
const confirmed = ref(false)
const stale = ref(false)
const original = ref('[]')
const loading = ref(false)
const analyzing = ref(false)
const saving = ref(false)
const error = ref('')
const actionError = ref('')
const dirty = computed(() => JSON.stringify(items.value) !== original.value)
const busy = computed(() => loading.value || analyzing.value || saving.value)
const includedCount = computed(() => items.value.filter(item => item.included).length)
const status = computed(() => stale.value ? '需求已变化' : dirty.value ? '未保存' : confirmed.value ? '已确认' : '待确认')
const sourceLabel = computed(() => source.value === 'real' ? `AI · ${model.value}` : source.value === 'mock' ? 'Mock 示例（未联网）' : '人工整理')

const editorOpen = ref(false)
const editingId = ref<string | null>(null)
const formRef = ref<FormInstance>()
function blankItem(): FeatureItem {
  return { id: crypto.randomUUID(), module: '', feature: '', description: '', acceptance_criteria: '', constraints: '', priority: 'P1', included: true }
}
const draft = reactive<FeatureItem>(blankItem())
const draftOriginal = ref('')
const editorDirty = computed(() => editorOpen.value && JSON.stringify(draft) !== draftOriginal.value)
const rules: FormRules = {
  module: [{ required: true, whitespace: true, message: '请输入模块名', trigger: 'blur' }],
  feature: [{ required: true, whitespace: true, message: '请输入功能点名称', trigger: 'blur' }],
  description: [{ required: true, whitespace: true, message: '请输入功能描述', trigger: 'blur' }],
}

function applySheet(data: FeatureSheet) {
  items.value = data.items
  revision.value = data.revision
  currentHash.value = data.current_hash
  source.value = data.source
  model.value = data.model
  confirmed.value = data.confirmed
  stale.value = data.stale
  original.value = JSON.stringify(data.items)
}

async function askDiscard(message: string) {
  try {
    await ElMessageBox.confirm(message, '放弃修改', {
      confirmButtonText: '放弃修改', cancelButtonText: '继续编辑', type: 'warning',
    })
    return true
  } catch { return false }
}

async function canLeave() {
  if (busy.value) return false
  if (!dirty.value && !editorDirty.value) return true
  return askDiscard('功能点有未保存的修改，确定放弃吗？')
}

async function loadFeatures() {
  if (!(await canLeave())) return
  loading.value = true
  error.value = ''
  actionError.value = ''
  try {
    if (![projectId, requirementId].every(id => Number.isSafeInteger(id) && id > 0)) throw new Error('项目或需求地址无效。')
    const [document, sheet] = await Promise.all([
      requirementsApi.get(projectId, requirementId), featuresApi.get(projectId, requirementId),
    ])
    requirement.value = document
    applySheet(sheet)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

async function analyze() {
  if (busy.value) return
  if (items.value.length || dirty.value) {
    try {
      await ElMessageBox.confirm('重新拆解将替换当前页面的功能点预览。保存前不会改动已入库清单，是否继续？', '重新拆解', {
        confirmButtonText: '开始拆解', cancelButtonText: '取消', type: 'warning',
      })
    } catch { return }
  }
  analyzing.value = true
  actionError.value = ''
  try {
    const config = await settingsApi.get()
    if (!config.configured) throw new Error('请先到模型设置保存生成模型配置。')
    const preview = await featuresApi.analyze(projectId, requirementId, config.timeout_seconds)
    if (preview.input_hash !== currentHash.value) throw new Error('需求原文已变化，请重新加载页面后拆解。已有清单未修改。')
    items.value = preview.items
    revision.value = preview.base_revision
    source.value = preview.source
    model.value = preview.model
    confirmed.value = false
    stale.value = false
    ElMessage.success('拆解完成，请核对并保存功能点')
  } catch (cause) {
    actionError.value = errorMessage(cause)
  } finally {
    analyzing.value = false
  }
}

function openEditor(item?: FeatureItem) {
  editingId.value = item?.id ?? null
  Object.assign(draft, item ?? blankItem())
  draftOriginal.value = JSON.stringify(draft)
  editorOpen.value = true
}

async function closeEditor() {
  if (!editorDirty.value || await askDiscard('当前功能点的修改尚未应用到清单，确定放弃吗？')) editorOpen.value = false
}

async function applyItem() {
  if (!(await formRef.value?.validate().catch(() => false))) return
  const item = { ...draft, module: draft.module.trim(), feature: draft.feature.trim(), description: draft.description.trim() }
  if (editingId.value === null) {
    if (items.value.length >= 50) return
    items.value.push(item)
  } else {
    items.value = items.value.map(existing => existing.id === editingId.value ? item : existing)
  }
  editorOpen.value = false
}

async function removeItem(item: FeatureItem) {
  try {
    await ElMessageBox.confirm(`从当前清单移除“${item.feature}”？保存清单后才会生效。`, '移除功能点', {
      confirmButtonText: '移除', cancelButtonText: '取消', type: 'warning',
    })
  } catch { return }
  items.value = items.value.filter(existing => existing.id !== item.id)
}

async function saveFeatures(confirm = false) {
  if (busy.value) return
  saving.value = true
  actionError.value = ''
  try {
    applySheet(await featuresApi.save(projectId, requirementId, {
      expected_revision: revision.value, input_hash: currentHash.value,
      items: items.value, source: source.value, model: model.value, confirmed: confirm,
    }))
    ElMessage.success(confirm ? '测试范围已确认' : '功能点已保存，请核对后确认')
  } catch (cause) {
    actionError.value = errorMessage(cause)
  } finally {
    saving.value = false
  }
}

function warnBeforeUnload(event: BeforeUnloadEvent) {
  if (dirty.value || editorDirty.value || busy.value) { event.preventDefault(); event.returnValue = '' }
}
onBeforeRouteLeave(canLeave)
onBeforeRouteUpdate(canLeave)
onMounted(() => { loadFeatures(); window.addEventListener('beforeunload', warnBeforeUnload) })
onBeforeUnmount(() => window.removeEventListener('beforeunload', warnBeforeUnload))
</script>

<template>
  <main>
    <router-link :to="`/projects/${projectId}/requirements`" class="back-link">← 返回需求列表</router-link>
    <div class="page-heading">
      <div><div class="eyebrow">FEATURE ANALYSIS</div><h1>{{ requirement?.title || '功能点拆解' }}</h1><p>从需求中提取可测试功能，核对后确认本次测试范围。</p></div>
      <el-button :loading="loading" :disabled="analyzing || saving" @click="loadFeatures">重新加载</el-button>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <div v-else-if="loading" class="loading-state"><el-skeleton :rows="6" animated /></div>
    <template v-else-if="requirement">
      <details class="requirement-reference"><summary>查看需求原文</summary><pre class="requirement-content">{{ requirement.content }}</pre></details>
      <el-alert v-if="stale" title="需求原文已变化。已有功能点仍保留，请重新核对并保存，再确认测试范围。" type="warning" :closable="false" show-icon class="feature-notice" />
      <el-alert v-if="actionError" :title="actionError" type="error" :closable="false" show-icon class="feature-notice" />
      <section class="project-panel">
        <div class="panel-toolbar feature-toolbar">
          <div><h2>功能点清单 <span>{{ items.length }} / 50</span></h2><p>已选 {{ includedCount }} 项 · {{ sourceLabel }}</p></div>
          <div class="toolbar-actions"><el-tag :type="confirmed && !dirty && !stale ? 'success' : 'warning'">{{ status }}</el-tag><el-button :disabled="busy || items.length >= 50" @click="openEditor()">新增功能点</el-button><el-button type="primary" :loading="analyzing" :disabled="saving" @click="analyze">AI 拆解</el-button></div>
        </div>
        <p class="feature-help">AI 拆解会将本份需求发送至已配置模型。结果先预览，点击“保存功能点”后入库。<router-link to="/settings">模型设置</router-link></p>
        <el-table v-if="items.length" :data="items" row-key="id" class="project-table feature-table">
          <el-table-column label="纳入范围" width="100" align="center"><template #default="{ row }"><el-checkbox v-model="row.included" :disabled="busy" :aria-label="`纳入范围：${row.feature}`" /></template></el-table-column>
          <el-table-column prop="module" label="模块" width="140" show-overflow-tooltip />
          <el-table-column label="功能点" min-width="300"><template #default="{ row }"><div class="feature-copy"><strong>{{ row.feature }}</strong><p>{{ row.description }}</p><details v-if="row.acceptance_criteria || row.constraints"><summary>验收标准与约束</summary><pre>{{ row.acceptance_criteria || '未提供验收标准' }}{{ row.constraints ? '\n约束 / 待澄清：' + row.constraints : '' }}</pre></details></div></template></el-table-column>
          <el-table-column prop="priority" label="优先级" width="90" />
          <el-table-column label="操作" width="135" align="right"><template #default="{ row }"><el-button link type="primary" :disabled="busy" @click="openEditor(row)">编辑</el-button><el-button link type="danger" :disabled="busy" @click="removeItem(row)">移除</el-button></template></el-table-column>
        </el-table>
        <div v-else class="empty-state"><div class="empty-illustration">≡</div><h3>还没有功能点</h3><p>使用 AI 拆解需求，或手动添加功能点。</p></div>
        <div class="feature-actions"><el-button :loading="saving" :disabled="analyzing || (!dirty && !stale)" @click="saveFeatures(false)">保存功能点</el-button><el-button type="primary" :disabled="busy || dirty || stale || confirmed || !includedCount" @click="saveFeatures(true)">确认测试范围</el-button><router-link :to="`/projects/${projectId}/requirements/${requirementId}/cases`" class="row-link case-entry">查看用例草稿 →</router-link><span>{{ dirty ? '当前修改尚未保存' : confirmed && !stale ? '已确认，可基于选中的功能点生成用例' : '先保存，再确认选中的功能点' }}</span></div>
      </section>
    </template>
  </main>

  <el-dialog v-model="editorOpen" :title="editingId === null ? '新增功能点' : '编辑功能点'" width="720px" destroy-on-close :close-on-click-modal="false" :before-close="closeEditor" @opened="formRef?.clearValidate()">
    <p class="dialog-intro">编辑完成后先应用到清单，再通过页面的“保存功能点”写入数据库。</p>
    <el-form ref="formRef" :model="draft" :rules="rules" label-position="top" @submit.prevent="applyItem">
      <div class="feature-form-row"><el-form-item label="模块" prop="module"><el-input v-model="draft.module" maxlength="100" placeholder="例如：账号模块" /></el-form-item><el-form-item label="优先级"><el-select v-model="draft.priority" aria-label="优先级"><el-option v-for="priority in ['P0', 'P1', 'P2']" :key="priority" :label="priority" :value="priority" /></el-select></el-form-item></div>
      <el-form-item label="功能点名称" prop="feature"><el-input v-model="draft.feature" maxlength="200" placeholder="例如：手机号验证码登录" /></el-form-item>
      <el-form-item label="功能描述" prop="description"><el-input v-model="draft.description" type="textarea" :rows="3" maxlength="4000" placeholder="描述该功能的业务行为" /></el-form-item>
      <el-form-item label="验收标准"><el-input v-model="draft.acceptance_criteria" type="textarea" :rows="3" maxlength="4000" placeholder="依据原文填写可验证的验收标准（选填）" /></el-form-item>
      <el-form-item label="约束 / 待澄清"><el-input v-model="draft.constraints" type="textarea" :rows="2" maxlength="2000" placeholder="需求中的限制或需要进一步确认的信息（选填）" /></el-form-item>
    </el-form>
    <template #footer><el-button @click="closeEditor">取消</el-button><el-button type="primary" @click="applyItem">应用到清单</el-button></template>
  </el-dialog>
</template>
