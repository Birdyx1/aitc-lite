<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { projectsApi, requirementsApi } from '../api'
import type { Project, Requirement, RequirementInput, RequirementSummary } from '../api'
import { errorMessage, formatDate } from '../utils'

const route = useRoute()
const projectId = Number(route.params.projectId)
const project = ref<Project>()
const requirements = ref<RequirementSummary[]>([])
const loading = ref(false)
const error = ref('')
const search = ref('')
const busyId = ref<number | null>(null)
const editorOpen = ref(false)
const saving = ref(false)
const editingId = ref<number | null>(null)
const formRef = ref<FormInstance>()
const formError = ref('')
const form = reactive<RequirementInput>({ title: '', content: '' })
const original = ref('')
const viewing = ref<Requirement>()
const viewerOpen = ref(false)
const dirty = computed(() => editorOpen.value && JSON.stringify(form) !== original.value)
const filtered = computed(() => {
  const keyword = search.value.trim().toLocaleLowerCase()
  return requirements.value.filter(item => item.title.toLocaleLowerCase().includes(keyword))
})
const rules: FormRules = {
  title: [
    { required: true, whitespace: true, message: '请输入需求标题', trigger: 'blur' },
    { max: 200, message: '标题不能超过 200 字', trigger: 'blur' },
  ],
  content: [
    { required: true, whitespace: true, message: '请输入需求正文', trigger: 'blur' },
    { max: 50000, message: '正文不能超过 50000 字', trigger: 'blur' },
  ],
}

async function loadRequirements() {
  loading.value = true
  error.value = ''
  try {
    if (!Number.isSafeInteger(projectId) || projectId < 1) throw new Error('项目地址无效，请返回项目列表。')
    const [loadedProject, list] = await Promise.all([
      projectsApi.get(projectId), requirementsApi.list(projectId),
    ])
    project.value = loadedProject
    requirements.value = list
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

function initializeEditor(requirement?: Requirement) {
  editingId.value = requirement?.id ?? null
  form.title = requirement?.title ?? ''
  form.content = requirement?.content ?? ''
  original.value = JSON.stringify(form)
  formError.value = ''
  editorOpen.value = true
}

async function openRequirement(item: RequirementSummary, edit: boolean) {
  busyId.value = item.id
  try {
    const document = await requirementsApi.get(projectId, item.id)
    if (edit) initializeEditor(document)
    else {
      viewing.value = document
      viewerOpen.value = true
    }
  } catch (cause) {
    ElMessage.error(errorMessage(cause))
  } finally {
    busyId.value = null
  }
}

async function confirmDiscard() {
  if (saving.value) return false
  if (!dirty.value) return true
  try {
    await ElMessageBox.confirm('未保存的需求修改将丢失，确定放弃吗？', '放弃修改', {
      confirmButtonText: '放弃修改', cancelButtonText: '继续编辑', type: 'warning',
    })
    return true
  } catch { return false }
}

async function closeEditor() {
  if (await confirmDiscard()) editorOpen.value = false
}

async function saveRequirement() {
  if (saving.value || !(await formRef.value?.validate().catch(() => false))) return
  saving.value = true
  formError.value = ''
  try {
    const payload = { title: form.title.trim(), content: form.content }
    const saved = editingId.value === null
      ? await requirementsApi.create(projectId, payload)
      : await requirementsApi.update(projectId, editingId.value, payload)
    const { content: _content, ...summary } = saved
    requirements.value = [summary, ...requirements.value.filter(item => item.id !== saved.id)]
    editorOpen.value = false
    ElMessage.success(editingId.value === null ? '需求已保存' : '需求已更新')
  } catch (cause) {
    formError.value = errorMessage(cause)
  } finally {
    saving.value = false
  }
}

async function removeRequirement(item: RequirementSummary) {
  try {
    await ElMessageBox.confirm(`确定删除“${item.title}”吗？删除后无法恢复。`, '删除需求', {
      confirmButtonText: '确认删除', cancelButtonText: '取消', type: 'warning',
      confirmButtonClass: 'el-button--danger',
    })
  } catch { return }
  busyId.value = item.id
  try {
    await requirementsApi.remove(projectId, item.id)
    requirements.value = requirements.value.filter(row => row.id !== item.id)
    ElMessage.success('需求已删除')
  } catch (cause) {
    ElMessage.error(errorMessage(cause))
  } finally {
    busyId.value = null
  }
}

function warnBeforeUnload(event: BeforeUnloadEvent) {
  if (dirty.value || saving.value) {
    event.preventDefault()
    event.returnValue = ''
  }
}

onBeforeRouteLeave(confirmDiscard)
onBeforeRouteUpdate(confirmDiscard)
onMounted(() => {
  loadRequirements()
  window.addEventListener('beforeunload', warnBeforeUnload)
})
onBeforeUnmount(() => window.removeEventListener('beforeunload', warnBeforeUnload))
</script>

<template>
  <main>
    <router-link to="/" class="back-link">← 返回项目列表</router-link>
    <div class="page-heading">
      <div>
        <div class="eyebrow">REQUIREMENTS</div>
        <h1>{{ project?.name || '项目需求' }}</h1>
        <p>录入并整理原始需求，为后续功能点分析准备依据。</p>
      </div>
      <el-button type="primary" size="large" :disabled="loading || !!error || busyId !== null" @click="initializeEditor()">＋ 新建需求</el-button>
    </div>

    <section class="project-panel" aria-labelledby="requirement-list-title">
      <div class="panel-toolbar">
        <div><h2 id="requirement-list-title">需求列表 <span>{{ filtered.length }}</span></h2><p>按最近更新时间排序 · 原文完整保留</p></div>
        <div class="toolbar-actions">
          <router-link :to="`/projects/${projectId}/knowledge`" class="row-link">项目知识库 →</router-link>
          <el-input v-model="search" placeholder="搜索需求标题" clearable aria-label="搜索需求标题" class="search-input" />
          <el-button :loading="loading" :disabled="busyId !== null || saving" @click="loadRequirements">刷新</el-button>
        </div>
      </div>

      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="load-error"><el-button size="small" @click="loadRequirements">重新加载</el-button></el-alert>
      <div v-if="loading" class="loading-state"><el-skeleton :rows="5" animated /></div>
      <template v-else-if="!error">
        <el-table v-if="filtered.length" :data="filtered" row-key="id" class="project-table">
          <el-table-column label="需求标题" min-width="300">
            <template #default="{ row }"><el-button link type="primary" class="requirement-title" :disabled="busyId !== null" @click="openRequirement(row, false)">{{ row.title }}</el-button></template>
          </el-table-column>
          <el-table-column label="创建时间" width="160"><template #default="{ row }"><span class="table-date">{{ formatDate(row.created_at) }}</span></template></el-table-column>
          <el-table-column label="最近更新" width="160"><template #default="{ row }"><span class="table-date">{{ formatDate(row.updated_at) }}</span></template></el-table-column>
          <el-table-column label="操作" width="350" align="right">
            <template #default="{ row }">
              <router-link :to="`/projects/${projectId}/requirements/${row.id}/features`" class="row-link">功能点</router-link>
              <router-link :to="`/projects/${projectId}/requirements/${row.id}/cases`" class="row-link">用例草稿</router-link>
              <el-button link type="primary" :disabled="busyId !== null" @click="openRequirement(row, false)">查看</el-button>
              <el-button link type="primary" :disabled="busyId !== null" @click="openRequirement(row, true)">编辑</el-button>
              <el-button link type="danger" :loading="busyId === row.id" :disabled="busyId !== null" @click="removeRequirement(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div v-else class="empty-state">
          <div class="empty-illustration">≡</div>
          <h3>{{ search.trim() ? '没有找到匹配的需求' : '添加这个项目的第一份需求' }}</h3>
          <p>{{ search.trim() ? '试试其他标题关键词。' : '粘贴需求原文并保存，随时查看和继续编辑。' }}</p>
          <el-button v-if="search.trim()" @click="search = ''">清空搜索</el-button>
          <el-button v-else type="primary" @click="initializeEditor()">＋ 新建需求</el-button>
        </div>
      </template>
      <div class="panel-footer">共 {{ requirements.length }} 份需求<span>仅显示当前项目的需求</span></div>
    </section>
  </main>

  <el-dialog v-model="editorOpen" :title="editingId === null ? '新建需求' : '编辑需求'" width="800px" destroy-on-close :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving" :before-close="closeEditor" @opened="formRef?.clearValidate()">
    <p class="dialog-intro">标题用于检索，正文保留原始换行和缩进。仅在点击保存后写入数据库。</p>
    <p v-if="editingId !== null" class="field-help">修改需求后，已有功能点需重新核对并确认。</p>
    <el-alert v-if="formError" :title="formError" type="error" :closable="false" show-icon class="form-error" />
    <el-form ref="formRef" :model="form" :rules="rules" label-position="top" :disabled="saving" @submit.prevent="saveRequirement">
      <el-form-item label="需求标题" prop="title"><el-input v-model="form.title" placeholder="例如：用户登录需求 v1" maxlength="200" show-word-limit /></el-form-item>
      <el-form-item label="需求正文" prop="content"><el-input v-model="form.content" type="textarea" :rows="14" placeholder="粘贴需求原文，包括业务流程、校验规则和验收标准" maxlength="50000" show-word-limit /></el-form-item>
    </el-form>
    <template #footer><el-button :disabled="saving" @click="closeEditor">取消</el-button><el-button type="primary" :loading="saving" @click="saveRequirement">保存需求</el-button></template>
  </el-dialog>

  <el-dialog v-model="viewerOpen" title="需求详情" width="800px" destroy-on-close>
    <template v-if="viewing">
      <h2 class="document-title">{{ viewing.title }}</h2>
      <p class="document-meta">最近更新 {{ formatDate(viewing.updated_at) }} · {{ viewing.content.length }} 字符</p>
      <pre class="requirement-content">{{ viewing.content }}</pre>
    </template>
    <template #footer><el-button @click="viewerOpen = false">关闭</el-button></template>
  </el-dialog>
</template>
