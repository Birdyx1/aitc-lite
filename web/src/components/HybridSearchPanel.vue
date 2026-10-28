<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { hybridSearchApi } from '../api'
import type { HybridSearchResult, KnowledgeSearchHit } from '../api'
import { errorMessage } from '../utils'

const props = defineProps<{ projectId: number; refreshKey: number; disabled: boolean }>()
const emit = defineEmits<{ openDocument: [hit: KnowledgeSearchHit] }>()
const query = ref('')
const topK = ref(5)
const candidateK = ref(20)
const useRerank = ref(false)
const rerankK = ref(20)
const loading = ref(false)
const error = ref('')
const result = ref<HybridSearchResult>()
let sequence = 0
let disposed = false
const modeLabels = { fused: '双路融合', bm25_only: '仅 BM25 有候选', vector_only: '仅向量有候选', no_results: '无候选结果', empty: '知识库为空' }
const canSearch = computed(() => !!query.value.trim() && Array.from(query.value.trim()).length <= 200 && candidateK.value >= topK.value && (!useRerank.value || rerankK.value >= topK.value) && !loading.value && !props.disabled)
function invalidate() { sequence += 1; result.value = undefined; error.value = ''; loading.value = false }
async function search() {
  if (!canSearch.value) return
  const current = ++sequence
  loading.value = true
  result.value = undefined
  error.value = ''
  try {
    const response = await hybridSearchApi.search(props.projectId, query.value.trim(), topK.value, candidateK.value, useRerank.value, rerankK.value)
    if (!disposed && current === sequence) result.value = response
  } catch (cause) {
    if (!disposed && current === sequence) error.value = errorMessage(cause)
  } finally { if (!disposed && current === sequence) loading.value = false }
}
watch([query, topK, candidateK, useRerank, rerankK, () => props.refreshKey], invalidate)
onBeforeUnmount(() => { disposed = true; sequence += 1 })
</script>

<template>
  <section class="project-panel draft-panel" aria-labelledby="hybrid-search-title">
    <div class="panel-toolbar"><div><h2 id="hybrid-search-title">RRF 混合检索</h2><p>BM25 + 向量双路召回 · 分块去重 · 可复核排名贡献</p></div><el-tag>模块 14–15</el-tag></div>
    <div class="knowledge-search-body">
      <form class="hybrid-search-form" @submit.prevent="search">
        <el-input v-model="query" placeholder="输入业务问题、专有名词或错误码" aria-label="混合检索问题" :disabled="loading || disabled" clearable />
        <label class="field-help">最终返回<el-select v-model="topK" aria-label="混合检索返回条数" :disabled="loading || disabled"><el-option v-for="n in [1,5,10,20]" :key="n" :value="n" :label="`Top ${n}`" /></el-select></label>
        <label class="field-help">每路召回<el-select v-model="candidateK" aria-label="每路召回条数" :disabled="loading || disabled"><el-option v-for="n in [20,30,50]" :key="n" :value="n" :label="`${n} 条`" /></el-select></label>
        <el-button native-type="submit" type="primary" :loading="loading" :disabled="!canSearch">混合检索</el-button>
      </form>
      <div class="toolbar-actions rerank-controls"><el-checkbox v-model="useRerank" :disabled="loading || disabled">启用 Rerank 精排</el-checkbox><label v-if="useRerank" class="field-help">精排候选 <el-select v-model="rerankK" aria-label="精排候选条数" style="width:110px" :disabled="loading || disabled"><el-option v-for="n in [20,30,50]" :key="n" :value="n" :label="`${n} 条`" /></el-select></label><router-link to="/settings/rerank">Rerank 配置 →</router-link></div>
      <p v-if="useRerank" class="field-help">先取 RRF 前 {{ rerankK }} 条候选发送正文与查询进行精排，再返回 Top K；真实精排会额外调用一次服务并可能计费。失败或未配置时保留 RRF 排名。</p>
      <p class="field-help">查询 1–200 字符；每路最多召回指定条数，融合后再截取 Top K。向量可用时每次调用一次 Embedding，可能计费；不会自动建索引或重试。</p>
      <p class="field-help">等权 RRF：每路贡献 = 1 / (60 + 该路名次)，未召回贡献为 0。同一分块合并为一条，融合分数不代表置信度。</p>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon><el-button :disabled="!canSearch" @click="search">重新混合检索</el-button></el-alert>
      <div v-if="loading" class="loading-state"><el-skeleton :rows="3" animated /></div>
      <template v-else-if="result">
        <el-alert :title="result.message" :type="result.degraded ? 'warning' : 'info'" :closable="false" show-icon />
        <el-alert :title="result.rerank.message" :type="['fallback', 'unconfigured'].includes(result.rerank.status) ? 'warning' : 'info'" :closable="false" show-icon />
        <el-alert v-if="result.rerank.mock_mode" title="当前精排为 Mock 字符重合排序，不代表语义精排能力。" type="warning" :closable="false" />
        <p v-if="result.rerank.candidate_count" class="field-help">精排模型 {{ result.rerank.model || 'Mock' }} · 输入 {{ result.rerank.candidate_count }} 条 · {{ result.rerank.elapsed_ms }} ms</p>
        <el-alert v-if="result.mock_mode" title="当前 Embedding 为 Mock，哈希向量不具备语义检索能力。" type="warning" :closable="false" />
        <div class="knowledge-search-summary" role="status"><p>{{ modeLabels[result.mode] }} · 有效分块 {{ result.corpus_chunk_count }} · 去重候选 {{ result.unique_candidate_count }} · 双路重合 {{ result.overlap_count }} · 返回 {{ result.hits.length }} · {{ result.elapsed_ms }} ms</p>
          <p v-for="source in (['bm25','vector'] as const)" :key="source">{{ source === 'bm25' ? 'BM25' : '向量' }}：{{ result.routes[source].message }} 候选 {{ result.routes[source].candidate_count }}（{{ result.routes[source].status }}）</p>
          <p>BM25 查询词项：{{ result.query_tokens.join(' / ') || '无' }}</p>
        </div>
        <article v-for="hit in result.hits" :key="hit.chunk_id" class="knowledge-search-hit">
          <div class="knowledge-hit-heading"><h3>#{{ hit.rank }} {{ hit.document_title }} · 分块 {{ hit.position }}</h3><span>RRF {{ hit.score.toFixed(8) }}</span></div>
          <p class="field-help">RRF 原名次 #{{ hit.rrf_rank }} → 最终 #{{ hit.rank }}<span v-if="hit.rerank_score !== null"> · Rerank 分数 {{ hit.rerank_score.toFixed(6) }}</span></p>
          <p class="field-help">v{{ hit.document_revision }} · {{ hit.source_filename || '手工录入' }} · 位置 [{{ hit.start_offset }}, {{ hit.end_offset }})</p>
          <el-table :data="[{ source: 'BM25', rank: hit.bm25_rank, raw: hit.bm25_score, contribution: hit.bm25_contribution }, { source: '向量', rank: hit.vector_rank, raw: hit.vector_score, contribution: hit.vector_contribution }]" size="small">
            <el-table-column prop="source" label="召回路" width="90" />
            <el-table-column label="该路名次" min-width="90"><template #default="{ row }">{{ row.rank ? `#${row.rank}` : '未召回' }}</template></el-table-column>
            <el-table-column label="原始分数（不直接相加）" min-width="180"><template #default="{ row }">{{ row.raw === null ? '—' : row.raw.toFixed(6) }}</template></el-table-column>
            <el-table-column label="RRF 贡献" min-width="130"><template #default="{ row }">{{ row.rank ? `1 / (${result.rrf_k} + ${row.rank}) = ` : '' }}{{ row.contribution.toFixed(8) }}</template></el-table-column>
          </el-table>
          <p class="field-help">BM25 命中词项：{{ hit.matched_tokens.join(' / ') || '无' }}</p>
          <pre class="requirement-content">{{ hit.content }}</pre>
          <el-button link type="primary" :disabled="disabled" @click="emit('openDocument', hit)">查看来源与命中分块</el-button>
        </article>
        <details class="field-help"><summary>本次融合参数</summary><p>{{ result.algorithm }} · 每路最多 {{ result.candidate_k }} 条 · 最终 Top {{ result.top_k }} · k={{ result.rrf_k }}</p><p>语料摘要：{{ result.corpus_signature }}</p><p>同分按分块 ID 排序；候选池之外的分块不参与融合。精排状态 {{ result.rerank.status }}；精排只重排入选候选，不生成答案。</p></details>
      </template>
    </div>
  </section>
</template>

<style scoped>
.rerank-controls { margin: 14px 0; }
.hybrid-search-form { display: grid; grid-template-columns: minmax(200px, 1fr) 110px 110px auto; align-items: end; gap: 12px; }
.hybrid-search-form label { display: grid; gap: 4px; }
@media (max-width: 760px) { .hybrid-search-form { grid-template-columns: 1fr 1fr; } .hybrid-search-form > :first-child { grid-column: 1 / -1; } }
</style>
