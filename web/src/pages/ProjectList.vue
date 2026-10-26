<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { formatDate, errorMessage } from '../utils'
import { projectsApi } from '../api'
import type { Project, ProjectInput } from '../api'

const projects = ref<Project[]>([])
const search = ref('')
const loading = ref(false)
const error = ref('')
const dialogOpen = ref(false)
const saving = ref(false)
const deletingId = ref<number | null>(null)
const editingId = ref<number | null>(null)
const formRef = ref<FormInstance>()
const formError = ref('')
const form = reactive<ProjectInput>({ name: '', description: '' })
const rules: FormRules = {
  name: [
    { required: true, whitespace: true, message: '请输入项目名称', trigger: 'blur' },
    { max: 100, message: '项目名称不能超过 100 字', trigger: 'blur' },
  ],
  description: [{ max: 2000, message: '描述不能超过 2000 字', trigger: 'blur' }],
}
const filteredProjects = computed(() => {
  const keyword = search.value.trim().toLocaleLowerCase()
  return projects.value.filter(project => `${project.name} ${project.description}`.toLocaleLowerCase().includes(keyword))
})
const latestUpdate = computed(() => projects.value[0] ? formatDate(projects.value[0].updated_at) : '暂无记录')

async function loadProjects() {
  loading.value = true
  error.value = ''
  try {
    projects.value = await projectsApi.list()
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

function openForm(project?: Project) {
  editingId.value = project?.id ?? null
  form.name = project?.name ?? ''
  form.description = project?.description ?? ''
  formError.value = ''
  dialogOpen.value = true
}

async function saveProject() {
  if (saving.value || !(await formRef.value?.validate().catch(() => false))) return
  saving.value = true
  formError.value = ''
  try {
    const payload = { name: form.name.trim(), description: form.description.trim() }
    const saved = editingId.value === null
      ? await projectsApi.create(payload)
      : await projectsApi.update(editingId.value, payload)
    projects.value = [saved, ...projects.value.filter(project => project.id !== saved.id)]
    error.value = ''
    dialogOpen.value = false
    ElMessage.success(editingId.value === null ? '项目已创建' : '项目已更新')
  } catch (cause) {
    formError.value = errorMessage(cause)
  } finally {
    saving.value = false
  }
}

async function removeProject(project: Project) {
  try {
    await ElMessageBox.confirm(`确定删除“${project.name}”吗？该项目下的全部需求也会一并删除，且无法恢复。`, '删除项目', {
      confirmButtonText: '确认删除', cancelButtonText: '取消', type: 'warning',
      confirmButtonClass: 'el-button--danger',
    })
  } catch { return }
  deletingId.value = project.id
  try {
    await projectsApi.remove(project.id)
    projects.value = projects.value.filter(item => item.id !== project.id)
    ElMessage.success('项目已删除')
  } catch (cause) {
    ElMessage.error(errorMessage(cause))
  } finally {
    deletingId.value = null
  }
}

onMounted(loadProjects)
</script>

<template>
      <main>
        <div class="page-heading"><div><div class="eyebrow">PROJECTS</div><h1>从一个项目开始</h1><p>集中管理你的测试项目，为需求分析与用例生成做好准备。</p></div><el-button type="primary" size="large" :disabled="loading || !!error" @click="openForm()"><span class="plus">＋</span>新建项目</el-button></div>

        <div class="overview"><div><span class="overview-label">全部项目</span><strong>{{ projects.length }}<small>个项目</small></strong></div><div class="overview-divider"></div><div><span class="overview-label">最近更新</span><strong class="latest-update">{{ latestUpdate }}</strong></div><div class="overview-description">整理项目范围<br><span>让每一次测试都有清晰的起点</span></div></div>

        <section class="project-panel" aria-labelledby="project-list-title">
          <div class="panel-toolbar"><div><h2 id="project-list-title">项目列表 <span>{{ filteredProjects.length }}</span></h2><p>按最近更新时间排序</p></div><div class="toolbar-actions"><el-input v-model="search" placeholder="搜索项目名称或描述" clearable aria-label="搜索项目" class="search-input" /><el-button :loading="loading" :disabled="saving || deletingId !== null" @click="loadProjects">刷新</el-button></div></div>
          <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="load-error"><el-button size="small" @click="loadProjects">重新加载</el-button></el-alert>
          <div v-if="loading" class="loading-state"><el-skeleton :rows="5" animated /></div>
          <template v-else-if="!error">
            <el-table v-if="filteredProjects.length" :data="filteredProjects" row-key="id" class="project-table">
              <el-table-column label="项目" min-width="350"><template #default="{ row }"><div class="project-name-cell"><span class="project-avatar">{{ row.name.slice(0, 1) }}</span><div class="project-copy"><router-link :to="`/projects/${row.id}/requirements`" class="project-link">{{ row.name }}</router-link><p :title="row.description">{{ row.description || '暂无项目描述' }}</p></div></div></template></el-table-column>
              <el-table-column label="创建时间" width="160"><template #default="{ row }"><span class="table-date">{{ formatDate(row.created_at) }}</span></template></el-table-column>
              <el-table-column label="最近更新" width="160"><template #default="{ row }"><span class="table-date">{{ formatDate(row.updated_at) }}</span></template></el-table-column>
              <el-table-column label="操作" width="320" align="right"><template #default="{ row }"><router-link :to="`/projects/${row.id}/requirements`" class="row-link">需求</router-link><router-link :to="`/projects/${row.id}/testcases`" class="row-link">已采纳用例</router-link><el-button link type="primary" :disabled="deletingId !== null" @click="openForm(row)">编辑</el-button><el-button link type="danger" :loading="deletingId === row.id" :disabled="deletingId !== null" @click="removeProject(row)">删除</el-button></template></el-table-column>
            </el-table>
            <div v-else class="empty-state"><div class="empty-illustration">▦</div><h3>{{ search.trim() ? '没有找到匹配的项目' : '创建你的第一个测试项目' }}</h3><p>{{ search.trim() ? '试试其他关键词，或清空搜索查看全部项目。' : '用一个清晰的名称和简短描述，定义这次测试的范围。' }}</p><el-button v-if="search.trim()" @click="search = ''">清空搜索</el-button><el-button v-else type="primary" @click="openForm()">＋ 新建项目</el-button></div>
          </template>
          <div class="panel-footer">共 {{ projects.length }} 个项目<span>项目数据自动保存至本机</span></div>
        </section>
        <footer class="page-footer">从需求到用例，一步一步构建你的测试工作流。</footer>
      </main>
    <el-dialog v-model="dialogOpen" :title="editingId === null ? '新建项目' : '编辑项目'" width="520px" destroy-on-close :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving" @opened="formRef?.clearValidate()">
      <p class="dialog-intro">用名称和描述明确项目范围，后续需求与用例将归属于该项目。</p>
      <el-alert v-if="formError" :title="formError" type="error" :closable="false" show-icon class="form-error" />
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" :disabled="saving" @submit.prevent="saveProject">
        <el-form-item label="项目名称" prop="name"><el-input v-model="form.name" placeholder="例如：商城订单系统" maxlength="100" show-word-limit /></el-form-item>
        <el-form-item label="项目描述" prop="description"><el-input v-model="form.description" type="textarea" :rows="5" placeholder="描述业务背景、测试目标或项目范围（选填）" maxlength="2000" show-word-limit /></el-form-item>
      </el-form>
      <template #footer><el-button :disabled="saving" @click="dialogOpen = false">取消</el-button><el-button type="primary" :loading="saving" @click="saveProject">{{ editingId === null ? '创建项目' : '保存修改' }}</el-button></template>
    </el-dialog>
</template>
