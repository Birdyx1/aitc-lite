<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { knowledgeApi, projectsApi } from '../api'
import type { KnowledgeDocument, KnowledgeDocumentInput, KnowledgeDocumentSummary, KnowledgeSearchHit, Project } from '../api'
import { errorMessage, formatDate } from '../utils'
import KnowledgeSearchPanel from '../components/KnowledgeSearchPanel.vue'
import VectorSearchPanel from '../components/VectorSearchPanel.vue'
import HybridSearchPanel from '../components/HybridSearchPanel.vue'

const projectId = Number(useRoute().params.projectId)
const project = ref<Project>()
const documents = ref<KnowledgeDocumentSummary[]>([])
const search = ref('')
const loading = ref(false)
const reading = ref(false)
const saving = ref(false)
const importing = ref(false)
const deleting = ref(false)
const error = ref('')
const editorOpen = ref(false)
const editing = ref<KnowledgeDocument>()
const createId = ref('')
const form = reactive<KnowledgeDocumentInput>({ title: '', content: '', source_filename: '' })
const initialForm = ref('')
const formRef = ref<FormInstance>()
const fileInput = ref<HTMLInputElement>()
const formError = ref('')
const viewerOpen = ref(false)
const viewing = ref<KnowledgeDocument>()
const viewTab = ref('chunks')
const searchRevision = ref(0)
const focusedChunkId = ref('')
let disposed = false
const busy = computed(() => loading.value || reading.value || saving.value || importing.value || deleting.value)
const dirty = computed(() => editorOpen.value && JSON.stringify(form) !== initialForm.value)
const charCount = computed(() => Array.from(form.content).length)
const filtered = computed(() => {
  const query = search.value.trim().toLocaleLowerCase()
  return documents.value.filter(item => `${item.title} ${item.source_filename}`.toLocaleLowerCase().includes(query))
})
const rules: FormRules = {
  title: [{ validator: (_rule, value: string, callback) => callback(!value.trim() || Array.from(value).length > 200 ? new Error('标题需为 1–200 个字符') : undefined), trigger: 'blur' }],
  content: [{ validator: (_rule, value: string, callback) => callback(!value.trim() || Array.from(value).length > 100000 ? new Error('正文需为 1–100000 个字符') : undefined), trigger: 'blur' }],
}

async function load() {
  if (busy.value) return
  loading.value = true
  error.value = ''
  try {
    if (!Number.isSafeInteger(projectId) || projectId < 1) throw new Error('项目地址无效，请返回项目列表。')
    const [loaded, list] = await Promise.all([projectsApi.get(projectId), knowledgeApi.list(projectId)])
    if (!disposed) { project.value = loaded; documents.value = list; searchRevision.value += 1 }
  } catch (cause) { if (!disposed) error.value = errorMessage(cause) }
  finally { loading.value = false }
}
function initializeEditor(document?: KnowledgeDocument) {
  editing.value = document
  createId.value = crypto.randomUUID()
  form.title = document?.title ?? ''
  form.content = document?.content ?? ''
  form.source_filename = document?.source_filename ?? ''
  initialForm.value = JSON.stringify(form)
  formError.value = ''
  editorOpen.value = true
}
async function openDocument(item: Pick<KnowledgeDocumentSummary, 'id'>, edit = false, hit?: KnowledgeSearchHit) {
  if (busy.value) return
  reading.value = true
  try {
    const document = await knowledgeApi.get(projectId, item.id)
    if (disposed) return
    if (hit && (document.revision !== hit.document_revision || !document.chunks.some(chunk => chunk.id === hit.chunk_id))) {
      throw new Error('来源文档已更新，请重新检索后查看命中分块。')
    }
    if (edit) initializeEditor(document)
    else { viewing.value = document; focusedChunkId.value = hit?.chunk_id ?? ''; viewTab.value = 'chunks'; viewerOpen.value = true }
  } catch (cause) {
    if (!disposed) {
      if (hit) searchRevision.value += 1
      ElMessage.error(errorMessage(cause))
    }
  } finally { reading.value = false }
}
async function importFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || saving.value || importing.value) return
  importing.value = true
  formError.value = ''
  try {
    if (!/\.(txt|md|markdown)$/i.test(file.name)) throw new Error('仅支持 UTF-8 的 TXT、MD 或 Markdown 文件。')
    if (file.size > 512 * 1024) throw new Error('文件不能超过 512 KiB，请拆分后导入。')
    if (Array.from(file.name).length > 255) throw new Error('来源文件名不能超过 255 个字符。')
    let text: string
    try { text = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer()) }
    catch { throw new Error('文件不是有效 UTF-8 文本，请转换编码后重新导入。') }
    if (!text.trim() || Array.from(text).length > 100000 || text.includes('\0')) throw new Error('文件正文需为 1–100000 个字符，且不能包含空字符。')
    if (disposed || !editorOpen.value) return
    form.content = text
    form.source_filename = file.name
    if (!form.title.trim()) form.title = Array.from(file.name.replace(/\.(txt|md|markdown)$/i, '')).slice(0, 200).join('')
    ElMessage.success('文件已载入编辑器，点击保存后入库并分块')
  } catch (cause) { if (!disposed) formError.value = errorMessage(cause) }
  finally { importing.value = false }
}
async function confirmDiscard() {
  if (saving.value || importing.value || deleting.value) return false
  if (!dirty.value) return true
  try {
    await ElMessageBox.confirm('未保存的知识文档修改将丢失，确定放弃吗？', '放弃修改', {
      confirmButtonText: '放弃修改', cancelButtonText: '继续编辑', type: 'warning',
    })
    return true
  } catch { return false }
}
async function closeEditor() { if (await confirmDiscard()) editorOpen.value = false }
async function save() {
  if (saving.value || importing.value || !(await formRef.value?.validate().catch(() => false))) return
  saving.value = true
  formError.value = ''
  try {
    const payload = { title: form.title.trim(), content: form.content, source_filename: form.source_filename }
    const saved = editing.value
      ? await knowledgeApi.update(projectId, editing.value.id, editing.value.revision, payload)
      : await knowledgeApi.create(projectId, createId.value, payload)
    if (disposed) return
    const { content: _content, content_hash: _hash, chunks: _chunks, ...summary } = saved
    documents.value = [summary, ...documents.value.filter(item => item.id !== saved.id)]
    editorOpen.value = false
    viewing.value = saved
    viewTab.value = 'chunks'
    focusedChunkId.value = ''
    viewerOpen.value = true
    searchRevision.value += 1
    ElMessage.success(`文档已保存，共 ${saved.chunk_count} 个分块`)
  } catch (cause) { if (!disposed) formError.value = errorMessage(cause) }
  finally { saving.value = false }
}
async function remove(item: KnowledgeDocumentSummary) {
  if (busy.value) return
  deleting.value = true
  try {
    try {
      await ElMessageBox.confirm(`确定删除“${item.title}”（版本 ${item.revision}）吗？文档与全部分块会一并删除，无法恢复。`, '删除知识文档', {
        confirmButtonText: '确认删除', cancelButtonText: '取消', type: 'warning', confirmButtonClass: 'el-button--danger',
      })
    } catch { return }
    await knowledgeApi.remove(projectId, item.id, item.revision)
    if (disposed) return
    documents.value = documents.value.filter(row => row.id !== item.id)
    searchRevision.value += 1
    ElMessage.success('文档与分块已删除')
  } catch (cause) { if (!disposed) ElMessage.error(errorMessage(cause)) }
  finally { deleting.value = false }
}
function openSearchHit(hit: KnowledgeSearchHit) { return openDocument({ id: hit.document_id }, false, hit) }
function scrollToHit() {
  if (focusedChunkId.value) document.getElementById(`knowledge-chunk-${focusedChunkId.value}`)?.scrollIntoView({ block: 'center' })
}
function beforeUnload(event: BeforeUnloadEvent) {
  if (dirty.value || saving.value || importing.value) { event.preventDefault(); event.returnValue = '' }
}
onBeforeRouteLeave(confirmDiscard)
onBeforeRouteUpdate(confirmDiscard)
onMounted(() => { load(); window.addEventListener('beforeunload', beforeUnload) })
onBeforeUnmount(() => { disposed = true; window.removeEventListener('beforeunload', beforeUnload) })
</script>

<template>
  <main>
    <router-link to="/" class="back-link">← 返回项目列表</router-link>
    <div class="page-heading"><div><div class="eyebrow">PROJECT KNOWLEDGE</div><h1>{{ project?.name || '项目' }} · 知识库</h1><p>保存业务规则、术语和接口说明，查看可追溯的原文分块。</p></div><el-button type="primary" size="large" :disabled="busy || !!error" @click="initializeEditor()">＋ 新建知识文档</el-button></div>
    <el-alert title="当前支持文档分块、BM25、向量语义检索、RRF 混合检索及可选 Rerank 精排；用例生成页可开启 RAG 注入并查看引用快照。" type="info" :closable="false" show-icon class="feature-notice" />
    <section class="project-panel" aria-labelledby="knowledge-list-title">
      <div class="panel-toolbar"><div><h2 id="knowledge-list-title">知识文档 <span>{{ filtered.length }} / {{ documents.length }}</span></h2><p>项目内独立保存 · 修改后原子替换旧分块</p></div><div class="toolbar-actions"><el-input v-model="search" placeholder="搜索文档标题或来源文件名" clearable class="search-input" /><el-button :loading="loading" :disabled="busy" @click="load">刷新</el-button></div></div>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="load-error"><el-button @click="load">重新加载</el-button></el-alert>
      <div v-if="loading" class="loading-state"><el-skeleton :rows="5" animated /></div>
      <template v-else-if="!error">
        <el-table v-if="filtered.length" :data="filtered" row-key="id" class="project-table">
          <el-table-column label="文档" min-width="260"><template #default="{ row }"><el-button link type="primary" class="requirement-title" :disabled="busy" @click="openDocument(row)">{{ row.title }}</el-button><p class="field-help">{{ row.source_filename || '手工录入' }}</p></template></el-table-column>
          <el-table-column prop="char_count" label="字符数" width="100" />
          <el-table-column prop="chunk_count" label="分块数" width="90" />
          <el-table-column label="版本" width="80"><template #default="{ row }">v{{ row.revision }}</template></el-table-column>
          <el-table-column label="最近更新" width="160"><template #default="{ row }">{{ formatDate(row.updated_at) }}</template></el-table-column>
          <el-table-column label="操作" width="200" align="right"><template #default="{ row }"><el-button link type="primary" :disabled="busy" @click="openDocument(row)">查看分块</el-button><el-button link type="primary" :disabled="busy" @click="openDocument(row, true)">编辑</el-button><el-button link type="danger" :disabled="busy" @click="remove(row)">删除</el-button></template></el-table-column>
        </el-table>
        <div v-else class="empty-state"><h3>{{ search.trim() ? '没有匹配的知识文档' : '添加第一份项目知识' }}</h3><p>粘贴文本或载入 UTF-8 的 TXT/Markdown，保存后自动分块。</p><el-button v-if="search.trim()" @click="search = ''">清空搜索</el-button><el-button v-else type="primary" :disabled="busy" @click="initializeEditor()">＋ 新建知识文档</el-button></div>
      </template>
      <div class="panel-footer">共 {{ documents.length }} 份文档 · {{ documents.reduce((total, item) => total + item.chunk_count, 0) }} 个分块<span>只显示当前项目 · BM25 在检索时构建</span></div>
    </section>
    <HybridSearchPanel v-if="project && !error" :project-id="projectId" :refresh-key="searchRevision" :disabled="busy || editorOpen || viewerOpen" @open-document="openSearchHit" />
    <KnowledgeSearchPanel v-if="project && !error" :project-id="projectId" :refresh-key="searchRevision" :disabled="busy || editorOpen || viewerOpen" @open-document="openSearchHit" />
    <VectorSearchPanel v-if="project && !error" :project-id="projectId" :refresh-key="searchRevision" :disabled="busy || editorOpen || viewerOpen" @open-document="openSearchHit" @index-changed="searchRevision += 1" />
    <p class="field-help knowledge-nav"><router-link :to="`/projects/${projectId}/requirements`">查看项目需求 →</router-link></p>
  </main>

  <el-dialog v-model="editorOpen" :title="editing ? '编辑知识文档' : '新建知识文档'" width="850px" destroy-on-close :close-on-click-modal="false" :close-on-press-escape="!saving && !importing" :show-close="!saving && !importing" :before-close="closeEditor" @opened="formRef?.clearValidate()">
    <p class="dialog-intro">粘贴文本，或导入 UTF-8 文件填入正文。文件上限 512 KiB，正文最多 100000 个字符；保存前不会入库，不调用模型。</p>
    <el-alert v-if="editing" :title="`当前编辑版本 v${editing.revision}；有修改时保存会替换全部旧分块，旧版本分块 ID 失效。`" type="warning" :closable="false" show-icon class="feature-notice" />
    <el-alert v-if="formError" :title="formError" type="error" :closable="false" show-icon class="form-error" />
    <div class="knowledge-import"><input ref="fileInput" type="file" accept=".txt,.md,.markdown" hidden @change="importFile" /><el-button :loading="importing" :disabled="saving || importing" @click="fileInput?.click()">导入 TXT / Markdown</el-button><span>{{ form.source_filename || '尚未选择文件，可直接粘贴正文' }}</span></div>
    <el-form ref="formRef" :model="form" :rules="rules" label-position="top" :disabled="saving || importing" @submit.prevent="save">
      <el-form-item label="文档标题" prop="title"><el-input v-model="form.title" placeholder="例如：登录规则与错误码说明" /></el-form-item>
      <el-form-item label="知识正文" prop="content"><el-input v-model="form.content" type="textarea" :rows="14" placeholder="粘贴业务规则、术语、接口说明或历史测试经验" /><p class="field-help">{{ charCount }} / 100000 个 Unicode 字符 · 分块目标 800 字符，重叠 120 字符，优先段落和句末边界</p></el-form-item>
    </el-form>
    <template #footer><el-button :disabled="saving || importing" @click="closeEditor">取消</el-button><el-button type="primary" :loading="saving" :disabled="importing" @click="save">保存并分块</el-button></template>
  </el-dialog>

  <el-dialog v-model="viewerOpen" title="知识文档与分块" width="900px" destroy-on-close @opened="scrollToHit">
    <template v-if="viewing">
      <h2 class="document-title">{{ viewing.title }}</h2><p class="document-meta">v{{ viewing.revision }} · {{ viewing.char_count }} 字符 · {{ viewing.chunk_count }} 个分块 · {{ viewing.source_filename || '手工录入' }} · {{ formatDate(viewing.updated_at) }}</p>
      <p class="field-help">{{ viewing.chunker_version }} · 上限 {{ viewing.chunk_size }} 字符 / 重叠 {{ viewing.chunk_overlap }} 字符。位置从 0 开始、结束位置不包含；按 Unicode 字符计数。</p>
      <el-tabs v-model="viewTab"><el-tab-pane label="分块预览" name="chunks"><div class="knowledge-chunks"><article v-for="chunk in viewing.chunks" :key="chunk.id" class="knowledge-chunk" :id="`knowledge-chunk-${chunk.id}`" :class="{ 'knowledge-chunk-focused': chunk.id === focusedChunkId }"><h3>分块 {{ chunk.position }} <el-tag v-if="chunk.id === focusedChunkId" size="small">命中分块</el-tag> <span>位置 [{{ chunk.start_offset }}, {{ chunk.end_offset }}) · {{ chunk.end_offset - chunk.start_offset }} 字符</span></h3><pre class="requirement-content">{{ chunk.content }}</pre><details class="field-help"><summary>分块标识与内容摘要</summary><p>分块 ID：{{ chunk.id }}</p><p>文档版本：{{ chunk.document_revision }} · SHA-256：{{ chunk.content_hash }}</p></details></article></div></el-tab-pane><el-tab-pane label="完整原文" name="original"><pre class="requirement-content">{{ viewing.content }}</pre></el-tab-pane></el-tabs>
    </template>
    <template #footer><el-button @click="viewerOpen = false">关闭</el-button></template>
  </el-dialog>
</template>
