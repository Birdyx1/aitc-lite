<script setup lang="ts">
import type { CaseGeneration } from '../api'
defineProps<{ generation: CaseGeneration; projectId: number }>()
const statuses = { current: '来源版本仍有效', updated: '来源已更新，以下为历史快照', deleted: '来源已删除，以下为历史快照' }
</script>

<template>
  <section class="knowledge-evidence" aria-labelledby="knowledge-evidence-title">
    <h3 id="knowledge-evidence-title">知识注入与引用追溯</h3>
    <p v-if="!generation.knowledge" class="field-help">本次未启用 RAG，或为无知识快照的历史生成。</p>
    <template v-else>
      <el-alert v-if="generation.knowledge.outdated" title="知识库已变化；此处保留生成时的资料，不会用当前正文覆盖历史快照。" type="warning" :closable="false" />
      <p>知识查询：{{ generation.knowledge.query }} · 注入 {{ generation.knowledge.sources.length }} 块</p>
      <p class="field-help">{{ generation.knowledge.message }} 精排：{{ generation.knowledge.rerank.message }}</p>
      <el-alert v-if="generation.knowledge.mock_mode || generation.knowledge.rerank.mock_mode || generation.source === 'mock'" title="本次包含 Mock 环节，引用只验证流程，不代表真实语义依据。" type="warning" :closable="false" />
      <p class="field-help">下列引用由 AI 原稿声明，已校验 ID 属于本次注入；尚未验证引用是否足以支持业务结论。人工编辑后的内容需重新核对。</p>
      <ul><li v-for="item in generation.cases" :key="item.id">{{ item.title }}：{{ generation.knowledge.case_citations[item.id]?.join('、') || '未声明引用' }}</li></ul>
      <details v-for="source in generation.knowledge.sources" :key="source.citation_id" class="knowledge-source">
        <summary>{{ source.citation_id }} · {{ source.document_title }} · v{{ source.document_revision }} · 分块 {{ source.position }}</summary>
        <p class="field-help">{{ statuses[source.current_status] }} · {{ source.source_filename || '手工录入' }} · 位置 [{{ source.start_offset }}, {{ source.end_offset }})</p>
        <pre class="requirement-content">{{ source.content }}</pre>
        <p class="field-help">RRF 原名次 {{ source.rrf_rank }} · RRF 分数 {{ source.score.toFixed(8) }}<span v-if="source.rerank_score !== null"> · 精排分数 {{ source.rerank_score.toFixed(6) }}</span></p>
        <p class="field-help">文档 {{ source.document_id }} · 分块 {{ source.chunk_id }} · SHA-256 {{ source.content_hash }}</p>
      </details>
      <details class="field-help"><summary>检索快照参数</summary><p>{{ generation.knowledge.context_version }} · {{ generation.knowledge.algorithm }} · 每路候选 {{ generation.knowledge.candidate_k }} · 向量模型 {{ generation.knowledge.embedding_model || '未配置' }}</p><p>语料摘要：{{ generation.knowledge.corpus_signature }}</p></details>
      <router-link :to="`/projects/${projectId}/knowledge`">查看当前项目知识库 →</router-link>
    </template>
  </section>
</template>

<style scoped>
.knowledge-evidence { margin: 16px 0; padding: 18px; background: #f7f9fc; border: 1px solid #e1e7ef; border-radius: 10px; }
.knowledge-evidence h3 { margin-top: 0; }
.knowledge-source { margin: 12px 0; }
.knowledge-source summary { cursor: pointer; color: #315b8f; }
</style>
