<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import { vectorApi } from '../api'
import type { KnowledgeSearchHit, VectorIndexStatus, VectorSearchResult } from '../api'
import { errorMessage } from '../utils'

const props = defineProps<{ projectId: number; refreshKey: number; disabled: boolean }>()
const emit = defineEmits<{ openDocument: [hit: KnowledgeSearchHit]; indexChanged: [] }>()
const status = ref<VectorIndexStatus>()
const statusError = ref('')
const error = ref('')
const query = ref('')
const topK = ref(5)
const result = ref<VectorSearchResult>()
const submitting = ref(false)
const searching = ref(false)
const checking = ref(false)
let disposed = false
let sequence = 0
let statusSequence = 0
let timer: ReturnType<typeof setTimeout> | undefined
let requestId = ''
const active = computed(() => ['queued', 'running'].includes(status.value?.task?.status ?? ''))
const progress = computed(() => {
  const task = status.value?.task
  return task?.total ? Math.floor(task.completed / task.total * 100) : 0
})
const canSearch = computed(() => status.value?.status === 'ready' && !!query.value.trim() && Array.from(query.value.trim()).length <= 200 && !searching.value && !props.disabled && !submitting.value)
const taskLabels: Record<string, string> = { queued: '排队中', running: '构建中', succeeded: '构建完成', failed: '构建失败', interrupted: '已中断' }
function schedule() {
  if (timer) clearTimeout(timer)
  if (!disposed) timer = setTimeout(() => { void loadStatus() }, active.value ? 1000 : 10_000)
}
async function loadStatus() {
  if (timer) clearTimeout(timer)
  const current = ++statusSequence
  checking.value = true
  try {
    const response = await vectorApi.status(props.projectId)
    if (!disposed && current === statusSequence) {
      const changed = status.value && ((status.value.status === 'ready' && response.status !== 'ready') || response.updated_at !== status.value.updated_at)
      status.value = response
      statusError.value = ''
      if (changed) { invalidate(); emit('indexChanged') }
    }
  } catch (cause) {
    if (!disposed && current === statusSequence) { statusError.value = errorMessage(cause); status.value = undefined; invalidate() }
  } finally {
    if (!disposed && current === statusSequence) { checking.value = false; schedule() }
  }
}
function invalidate() { sequence += 1; result.value = undefined; error.value = ''; searching.value = false }
async function build() {
  if (props.disabled || submitting.value || active.value || !status.value || ['unconfigured', 'empty'].includes(status.value.status)) return
  submitting.value = true
  try {
    await ElMessageBox.confirm('将使用已保存的 Embedding 配置，重新向量化当前项目的全部分块。真实模式会调用服务并可能计费；失败后手动重建也可能重复计费。', '构建向量索引', { confirmButtonText: '开始构建', cancelButtonText: '取消' })
    if (disposed) return
    requestId ||= crypto.randomUUID()
    await vectorApi.build(props.projectId, requestId)
    requestId = ''
    invalidate()
    await loadStatus()
  } catch (cause) {
    if (!disposed && cause !== 'cancel' && cause !== 'close') {
      error.value = `${errorMessage(cause)} 若提交超时，请先刷新状态；再次点击会使用同一请求标识核对任务。`
      await loadStatus()
    }
  } finally { if (!disposed) submitting.value = false }
}
async function search() {
  if (!canSearch.value) return
  const current = ++sequence
  searching.value = true
  error.value = ''
  result.value = undefined
  try {
    const response = await vectorApi.search(props.projectId, query.value.trim(), topK.value)
    if (!disposed && current === sequence) result.value = response
  } catch (cause) {
    if (!disposed && current === sequence) error.value = errorMessage(cause)
  } finally { if (!disposed && current === sequence) searching.value = false }
}
watch([query, topK], invalidate)
watch(() => props.refreshKey, () => { invalidate(); status.value = undefined; void loadStatus() })
onMounted(loadStatus)
onBeforeUnmount(() => { disposed = true; sequence += 1; statusSequence += 1; if (timer) clearTimeout(timer) })
</script>

<template>
  <section class="project-panel draft-panel" aria-labelledby="vector-search-title">
    <div class="panel-toolbar"><div><h2 id="vector-search-title">向量索引与语义检索</h2><p>项目独立 Chroma 索引 · 余弦相似度排序</p></div><el-tag>模块 13</el-tag></div>
    <div class="knowledge-search-body">
      <el-alert v-if="statusError" :title="statusError" type="error" :closable="false" show-icon />
      <div class="toolbar-actions">
        <el-button :loading="checking" :disabled="disabled || submitting" @click="loadStatus">刷新索引状态</el-button>
        <el-button type="primary" :loading="submitting || active" :disabled="disabled || !status || ['unconfigured', 'empty'].includes(status.status) || active || searching" @click="build">{{ status?.indexed_chunk_count ? '重建向量索引' : '构建向量索引' }}</el-button>
        <router-link to="/settings/embedding">Embedding 配置 →</router-link>
      </div>
      <div v-if="status" class="knowledge-search-summary" role="status">
        <p>{{ status.message }}</p>
        <p>当前分块 {{ status.chunk_count }} · 已发布索引 {{ status.indexed_chunk_count }} · 维度 {{ status.dimensions ?? '—' }} · 模型 {{ status.model || '—' }}</p>
        <el-alert v-if="status.mock_mode" title="Mock 哈希向量只验证流程，不具备语义检索能力。" type="warning" :closable="false" />
        <template v-if="status.task">
          <p>最近任务：{{ taskLabels[status.task.status] }} · {{ status.task.completed }} / {{ status.task.total }}</p>
          <el-progress v-if="active" :percentage="progress" />
          <p v-if="status.task.error" class="field-help">{{ status.task.error }}</p>
        </template>
      </div>
      <p class="field-help">构建每批最多 16 个分块，全部成功后发布；文档或模型配置变化需手动重建。服务中断后不自动重试付费调用。本模块暂不提供构建断点续传。</p>
      <form class="knowledge-search-form" @submit.prevent="search">
        <el-input v-model="query" placeholder="换一种说法提问，例如：收不到登录短信怎么办？" aria-label="语义检索问题" :disabled="searching || disabled" clearable />
        <el-select v-model="topK" aria-label="语义检索返回条数" :disabled="searching || disabled"><el-option v-for="n in [1,5,10,20]" :key="n" :value="n" :label="`Top ${n}`" /></el-select>
        <el-button type="primary" native-type="submit" :loading="searching" :disabled="!canSearch">语义检索</el-button>
      </form>
      <p class="field-help">每次检索调用一次 Embedding；输入 1–200 个字符。最近分块不等于有效答案，相似度不是置信度；可使用上方 RRF 混合检索合并 BM25；尚不生成答案。</p>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <template v-if="result">
        <p role="status">{{ result.message }} 返回 {{ result.hits.length }} 个分块 · {{ result.elapsed_ms }} ms</p>
        <article v-for="hit in result.hits" :key="hit.chunk_id" class="knowledge-search-hit">
          <div class="knowledge-hit-heading"><h3>#{{ hit.rank }} {{ hit.document_title }} · 分块 {{ hit.position }}</h3><span>余弦相似度 {{ hit.score.toFixed(6) }}</span></div>
          <p class="field-help">v{{ hit.document_revision }} · {{ hit.source_filename || '手工录入' }} · 位置 [{{ hit.start_offset }}, {{ hit.end_offset }})</p>
          <pre class="requirement-content">{{ hit.content }}</pre>
          <el-button link type="primary" :disabled="disabled" @click="emit('openDocument', hit)">查看来源与命中分块</el-button>
        </article>
      </template>
    </div>
  </section>
</template>
