<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { knowledgeSearchApi } from '../api'
import type { KnowledgeSearchHit, KnowledgeSearchResult } from '../api'
import { errorMessage } from '../utils'

const props = defineProps<{ projectId: number; refreshKey: number; disabled: boolean }>()
const emit = defineEmits<{ openDocument: [hit: KnowledgeSearchHit] }>()
const query = ref('')
const topK = ref(5)
const loading = ref(false)
const error = ref('')
const result = ref<KnowledgeSearchResult>()
let sequence = 0
let disposed = false
const canSearch = computed(() => !!query.value.trim() && Array.from(query.value.trim()).length <= 200 && !loading.value && !props.disabled)
function invalidate() {
  sequence += 1
  result.value = undefined
  error.value = ''
  loading.value = false
}
async function search() {
  if (!canSearch.value) return
  const requestId = ++sequence
  const input = query.value.trim()
  const limit = topK.value
  loading.value = true
  error.value = ''
  result.value = undefined
  try {
    const response = await knowledgeSearchApi.search(props.projectId, input, limit)
    if (!disposed && sequence === requestId) result.value = response
  } catch (cause) {
    if (!disposed && sequence === requestId) error.value = errorMessage(cause)
  } finally {
    if (!disposed && sequence === requestId) loading.value = false
  }
}
watch([query, topK, () => props.refreshKey], invalidate)
onBeforeUnmount(() => { disposed = true; sequence += 1 })
</script>

<template>
  <section class="project-panel draft-panel" aria-labelledby="knowledge-search-title">
    <div class="panel-toolbar"><div><h2 id="knowledge-search-title">BM25 关键词检索</h2><p>搜索当前项目的分块正文 · 本地分词与排序 · 不调用模型</p></div><el-tag type="info">关键词召回</el-tag></div>
    <div class="knowledge-search-body">
      <form class="knowledge-search-form" @submit.prevent="search">
        <el-input v-model="query" placeholder="输入关键词或错误码，例如：验证码过期 E1001" aria-label="知识检索关键词" :disabled="loading || disabled" clearable />
        <el-select v-model="topK" aria-label="检索返回条数" :disabled="loading || disabled"><el-option v-for="n in [1,5,10,20]" :key="n" :value="n" :label="`Top ${n}`" /></el-select>
        <el-button native-type="submit" type="primary" :loading="loading" :disabled="!canSearch">检索知识</el-button>
      </form>
      <p class="field-help">1–200 个字符。中文分词，英文及错误码忽略大小写；多词查询允许命中部分词。文档标题和文件名仅用于上方列表筛选。</p>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="test-result"><el-button :disabled="!canSearch" @click="search">重新检索</el-button></el-alert>
      <div v-if="loading" class="loading-state"><el-skeleton :rows="3" animated /></div>
      <template v-else-if="result">
        <div class="knowledge-search-summary" role="status"><p>{{ result.message }}</p><p>有效分块 {{ result.corpus_chunk_count }} · 本次参与索引 {{ result.indexed_chunk_count }} · 命中 {{ result.total_matches }} · 返回 {{ result.hits.length }} · {{ result.elapsed_ms }} ms</p><div class="knowledge-query-tokens"><span>查询词项：</span><el-tag v-for="token in result.query_tokens" :key="token" size="small" type="info">{{ token }}</el-tag><span v-if="!result.query_tokens.length">无</span></div></div>
        <article v-for="hit in result.hits" :key="hit.chunk_id" class="knowledge-search-hit">
          <div class="knowledge-hit-heading"><h3>#{{ hit.rank }} {{ hit.document_title }} · 分块 {{ hit.position }}</h3><span>BM25 {{ hit.score.toFixed(6) }}</span></div>
          <p class="field-help">v{{ hit.document_revision }} · {{ hit.source_filename || '手工录入' }} · 位置 [{{ hit.start_offset }}, {{ hit.end_offset }})</p>
          <p class="field-help">命中词项：{{ hit.matched_tokens.join(' / ') }}</p>
          <pre class="requirement-content">{{ hit.content }}</pre>
          <el-button link type="primary" :disabled="disabled" @click="emit('openDocument', hit)">查看来源与命中分块</el-button>
          <details class="field-help"><summary>分块标识与内容摘要</summary><p>{{ hit.chunk_id }}</p><p>SHA-256：{{ hit.content_hash }}</p></details>
        </article>
        <details class="field-help knowledge-search-details"><summary>本次检索参数</summary><p>{{ result.algorithm }} · {{ result.tokenizer }} · k1={{ result.k1 }} / b={{ result.b }}</p><p>语料摘要：{{ result.corpus_signature }}</p><p>每次检索读取当前有效分块临时构建 BM25，分数只用于本次查询排序，不能视为置信度或与其他查询直接比较。</p></details>
      </template>
      <p v-else-if="!error && !loading" class="field-help knowledge-search-empty">输入关键词后检索；文档刷新、修改或删除后会清空本页旧结果。</p>
    </div>
  </section>
</template>
