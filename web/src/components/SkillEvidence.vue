<script setup lang="ts">
import type { CaseGeneration } from '../api'
const props = defineProps<{ generation: CaseGeneration }>()
function name(id: string) { return props.generation.skills?.items.find(item => item.id === id)?.name || id }
</script>

<template>
  <section class="skill-evidence" aria-labelledby="skill-evidence-title">
    <h3 id="skill-evidence-title">专项 Skill 与覆盖声明</h3>
    <p v-if="!generation.skills" class="field-help">本次为普通生成，未启用专项 Skill，或为无专项快照的历史记录。</p>
    <template v-else>
      <p>本次启用：{{ generation.skills.items.map(item => item.name).join(' / ') }}</p>
      <p class="field-help">以下为 AI 原稿声明的专项覆盖，已校验标识属于本次启用范围；不代表已验证覆盖充分。人工编辑后的内容需重新核对。</p>
      <el-alert v-if="generation.source === 'mock'" title="Mock 仅演示专项选择、标记和快照流程，不代表真实专项测试质量。" type="warning" :closable="false" />
      <ul><li v-for="item in generation.cases" :key="item.id">{{ item.title }}：{{ generation.skills.case_tags[item.id]?.length ? generation.skills.case_tags[item.id]!.map(name).join(' / ') : '未声明专项覆盖' }}</li></ul>
      <details v-for="item in generation.skills.items" :key="item.id"><summary>{{ item.name }} · {{ item.version }} · 查看本次规则快照</summary><p class="field-help">{{ item.description }}</p><p class="field-help">handler：{{ item.handler_version }} · 内容摘要：{{ item.content_hash }}</p><pre>{{ item.prompt }}</pre></details>
    </template>
  </section>
</template>

<style scoped>
.skill-evidence { margin: 16px 0; padding: 18px; background: #f7f9fc; border: 1px solid #e1e7ef; border-radius: 10px; }
.skill-evidence h3 { margin-top: 0; }
.skill-evidence details { margin: 12px 0; }
.skill-evidence summary { cursor: pointer; color: #315b8f; }
.skill-evidence pre { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.8; }
</style>
