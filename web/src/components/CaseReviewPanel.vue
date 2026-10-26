<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { reviewsApi } from '../api'
import type { CaseFields, CaseGeneration, CaseReview, CaseReviewInput } from '../api'
import { errorMessage } from '../utils'

const props = defineProps<{ projectId: number; requirementId: number; generation: CaseGeneration }>()
const emit = defineEmits<{ busy: [value: boolean] }>()
const items = ref<CaseReview[]>([])
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const filter = ref('all')
const editorOpen = ref(false)
const rejectOpen = ref(false)
const active = ref<CaseReview>()
const reason = ref('')
const formError = ref('')
const formRef = ref<FormInstance>()
const fields = reactive<CaseFields>({ title: '', priority: 'P1', case_type: 'functional', is_smoke: false, precondition: '', steps: [], expected_result: '' })
const stepsText = ref('')
const editorOriginal = ref('')
const editState = () => JSON.stringify({ ...fields, stepsText: stepsText.value })
const editorDirty = computed(() => editorOpen.value && editState() !== editorOriginal.value)
const busy = computed(() => loading.value || saving.value)
const visible = computed(() => items.value.filter(item => filter.value === 'all' || item.status === filter.value))
const count = (status: string) => items.value.filter(item => item.status === status).length
const statusNames = { pending: '待评审', accepted: '已采纳', rejected: '已驳回' }
const typeNames = { functional: '功能', boundary: '边界', exception: '异常' }
const rules: FormRules = {
  title: [{ required: true, whitespace: true, message: '请输入用例标题', trigger: 'blur' }],
  expected_result: [{ required: true, whitespace: true, message: '请输入预期结果', trigger: 'blur' }],
}
watch(() => busy.value || editorOpen.value || rejectOpen.value, value => emit('busy', value), { immediate: true })

async function load() {
  loading.value = true
  error.value = ''
  try {
    items.value = (await reviewsApi.list(props.projectId, props.requirementId, props.generation.id)).items
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally { loading.value = false }
}

async function submit(item: CaseReview, payload: CaseReviewInput) {
  saving.value = true
  try {
    const saved = await reviewsApi.save(props.projectId, props.requirementId, props.generation.id, item.draft_id, payload)
    items.value = items.value.map(row => row.draft_id === saved.draft_id ? saved : row)
    error.value = ''
    return true
  } catch (cause) {
    const message = errorMessage(cause)
    if (editorOpen.value || rejectOpen.value) formError.value = message
    else error.value = message
    return false
  } finally { saving.value = false }
}

function edit(item: CaseReview) {
  active.value = item
  Object.assign(fields, { ...item.current, steps: [...item.current.steps] })
  stepsText.value = item.current.steps.join('\n')
  formError.value = ''
  editorOriginal.value = editState()
  editorOpen.value = true
}

async function closeEditor() {
  if (saving.value) return
  if (editorDirty.value) {
    try {
      await ElMessageBox.confirm('用例修改尚未保存，确定放弃吗？', '放弃修改', {
        confirmButtonText: '放弃修改', cancelButtonText: '继续编辑', type: 'warning',
      })
    } catch { return }
  }
  editorOpen.value = false
}

async function saveEdit() {
  if (!active.value || saving.value || !(await formRef.value?.validate().catch(() => false))) return
  const steps = stepsText.value.split('\n').map(step => step.trim()).filter(Boolean)
  if (!steps.length || steps.length > 20 || steps.some(step => step.length > 1000)) {
    formError.value = '操作步骤需为 1–20 行，每行最多 1000 字。'
    return
  }
  if (await submit(active.value, { action: 'edit', expected_revision: active.value.revision, fields: { ...fields, steps } })) {
    editorOpen.value = false
    ElMessage.success('人工修改已保存，仍需评审采纳')
  }
}

async function accept(item: CaseReview) {
  if (busy.value) return
  if (await submit(item, { action: 'accept', expected_revision: item.revision })) ElMessage.success('已采纳，可在项目用例库查看')
}

function reject(item: CaseReview) {
  active.value = item
  reason.value = ''
  formError.value = ''
  rejectOpen.value = true
}

async function saveRejection() {
  if (!active.value || saving.value) return
  if (!reason.value.trim()) { formError.value = '请填写驳回原因'; return }
  if (await submit(active.value, { action: 'reject', expected_revision: active.value.revision, rejection_reason: reason.value.trim() })) {
    rejectOpen.value = false
    ElMessage.success('已驳回')
  }
}
onMounted(load)
</script>

<template>
  <div class="review-toolbar">
    <el-radio-group v-model="filter" :disabled="busy" size="small"><el-radio-button value="all">全部 {{ items.length }}</el-radio-button><el-radio-button value="pending">待评审 {{ count('pending') }}</el-radio-button><el-radio-button value="accepted">已采纳 {{ count('accepted') }}</el-radio-button><el-radio-button value="rejected">已驳回 {{ count('rejected') }}</el-radio-button></el-radio-group>
    <router-link :to="`/projects/${projectId}/testcases`" class="row-link">查看已采纳用例 →</router-link>
  </div>
  <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="feature-notice"><el-button size="small" :disabled="busy" @click="load">重新加载评审</el-button></el-alert>
  <el-skeleton v-if="loading" :rows="5" animated />
  <template v-else>
    <article v-for="item in visible" :key="item.draft_id" class="case-card">
      <div class="case-card-title"><h3>{{ item.current.title }}</h3><span>{{ item.current.priority }} · {{ typeNames[item.current.case_type] }}</span><el-tag v-if="item.current.is_smoke" size="small">冒烟</el-tag><el-tag :type="item.status === 'accepted' ? 'success' : item.status === 'rejected' ? 'danger' : 'warning'" size="small">{{ statusNames[item.status] }}</el-tag></div>
      <dl><dt>前置条件</dt><dd>{{ item.current.precondition || '无' }}</dd><dt>操作步骤</dt><dd><ol><li v-for="(step, index) in item.current.steps" :key="index">{{ step }}</li></ol></dd><dt>预期结果</dt><dd>{{ item.current.expected_result }}</dd></dl>
      <p v-if="item.status === 'rejected'" class="rejection-reason">驳回原因：{{ item.rejection_reason }}</p>
      <details v-if="item.was_edited" class="generation-evidence"><summary>已人工编辑 · 查看 AI 原稿</summary><h4>{{ item.original.title }}</h4><p>{{ item.original.priority }} · {{ typeNames[item.original.case_type] }} · {{ item.original.is_smoke ? '冒烟' : '普通用例' }}</p><p>前置条件：{{ item.original.precondition || '无' }}</p><ol><li v-for="(step, index) in item.original.steps" :key="index">{{ step }}</li></ol><p>预期结果：{{ item.original.expected_result }}</p></details>
      <div v-if="item.status === 'pending'" class="review-actions"><el-button :disabled="busy || !!error" size="small" @click="edit(item)">编辑</el-button><el-button type="primary" :disabled="busy || !!error || generation.outdated" size="small" @click="accept(item)">采纳</el-button><el-button :disabled="busy || !!error" size="small" @click="reject(item)">驳回</el-button></div>
    </article>
    <p v-if="!visible.length && !error" class="field-help">当前筛选下没有用例。</p>
  </template>

  <el-dialog v-model="editorOpen" title="编辑用例草稿" width="760px" destroy-on-close :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving" :before-close="closeEditor" @opened="formRef?.clearValidate()">
    <el-alert v-if="formError" :title="formError" type="error" :closable="false" show-icon class="form-error" />
    <el-form ref="formRef" :model="fields" :rules="rules" label-position="top" :disabled="saving">
      <el-form-item label="用例标题" prop="title"><el-input v-model="fields.title" placeholder="输入用例标题" maxlength="200" /></el-form-item>
      <div class="review-form-row"><el-form-item label="优先级"><el-select v-model="fields.priority" aria-label="用例优先级"><el-option v-for="p in ['P0','P1','P2']" :key="p" :value="p" :label="p" /></el-select></el-form-item><el-form-item label="类型"><el-select v-model="fields.case_type" aria-label="用例类型"><el-option v-for="(label, value) in typeNames" :key="value" :value="value" :label="label" /></el-select></el-form-item><el-form-item label="冒烟"><el-switch v-model="fields.is_smoke" aria-label="用例冒烟标记" /></el-form-item></div>
      <el-form-item label="前置条件"><el-input v-model="fields.precondition" type="textarea" :rows="2" maxlength="2000" placeholder="前置条件（选填）" /></el-form-item>
      <el-form-item label="操作步骤（每行一个步骤）"><el-input v-model="stepsText" type="textarea" :rows="5" maxlength="20400" placeholder="每行输入一个操作步骤" /></el-form-item>
      <el-form-item label="预期结果" prop="expected_result"><el-input v-model="fields.expected_result" type="textarea" :rows="3" maxlength="4000" placeholder="输入可验证的预期结果" /></el-form-item>
    </el-form>
    <template #footer><el-button :disabled="saving" @click="closeEditor">取消</el-button><el-button type="primary" :loading="saving" @click="saveEdit">保存人工修改</el-button></template>
  </el-dialog>
  <el-dialog v-model="rejectOpen" title="驳回用例" width="500px" :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving">
    <el-alert v-if="formError" :title="formError" type="error" :closable="false" show-icon class="form-error" />
    <el-input v-model="reason" type="textarea" :rows="4" maxlength="1000" show-word-limit :disabled="saving" placeholder="说明驳回原因，例如：预期结果缺乏需求依据" />
    <template #footer><el-button :disabled="saving" @click="rejectOpen = false">取消</el-button><el-button type="danger" :loading="saving" @click="saveRejection">确认驳回</el-button></template>
  </el-dialog>
</template>
