<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { projectsApi, reviewsApi } from '../api'
import type { AcceptedCase, Project } from '../api'
import { errorMessage } from '../utils'

const projectId = Number(useRoute().params.projectId)
const project = ref<Project>()
const items = ref<AcceptedCase[]>([])
const search = ref('')
const smokeOnly = ref(false)
const loading = ref(false)
const error = ref('')
const typeNames = { functional: '功能', boundary: '边界', exception: '异常' }
const filtered = computed(() => items.value.filter(item =>
  (!smokeOnly.value || item.is_smoke) && `${item.title} ${item.feature_name} ${item.module} ${item.requirement_title}`.toLocaleLowerCase().includes(search.value.trim().toLocaleLowerCase()),
))
async function load() {
  loading.value = true
  error.value = ''
  try {
    if (!Number.isSafeInteger(projectId) || projectId < 1) throw new Error('项目地址无效。')
    const [loaded, cases] = await Promise.all([projectsApi.get(projectId), reviewsApi.accepted(projectId)])
    project.value = loaded
    items.value = cases
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}
onMounted(load)
</script>

<template>
  <main>
    <router-link to="/" class="back-link">← 返回项目列表</router-link>
    <div class="page-heading"><div><div class="eyebrow">ACCEPTED TEST CASES</div><h1>{{ project?.name || '项目' }} · 已采纳用例</h1><p>仅展示人工采纳的用例，保留生成来源和历史依据。</p></div><el-button :loading="loading" @click="load">刷新</el-button></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <div v-else-if="loading" class="loading-state"><el-skeleton :rows="5" animated /></div>
    <section v-else class="project-panel">
      <div class="panel-toolbar"><h2>已采纳 {{ filtered.length }} / {{ items.length }}</h2><div class="toolbar-actions"><el-checkbox v-model="smokeOnly">仅冒烟</el-checkbox><el-input v-model="search" placeholder="搜索标题、需求或功能点" clearable class="search-input" /></div></div>
      <div class="draft-panel-body">
        <article v-for="item in filtered" :key="item.id" class="case-card">
          <div class="case-card-title"><h3>{{ item.title }}</h3><span>{{ item.priority }} · {{ typeNames[item.case_type] }}</span><el-tag v-if="item.is_smoke" size="small">冒烟</el-tag><el-tag v-if="item.source === 'mock'" type="warning" size="small">Mock 示例</el-tag></div>
          <p class="field-help">{{ item.requirement_title }} / {{ item.module }} / {{ item.feature_name }}{{ item.was_edited ? ' · 已人工编辑' : '' }}</p>
          <p v-if="item.outdated" class="outdated-label">生成依据已变化，请重新核对适用性。</p>
          <dl><dt>前置条件</dt><dd>{{ item.precondition || '无' }}</dd><dt>操作步骤</dt><dd><ol><li v-for="(step,index) in item.steps" :key="index">{{ step }}</li></ol></dd><dt>预期结果</dt><dd>{{ item.expected_result }}</dd></dl>
          <router-link :to="`/projects/${projectId}/requirements/${item.requirement_id}/cases`" class="row-link">查看来源需求的生成记录 →</router-link>
        </article>
        <div v-if="!filtered.length" class="empty-state"><h3>{{ items.length ? '没有匹配的用例' : '还没有已采纳用例' }}</h3><p>在用例草稿页完成评审采纳后，这里会展示对应结果。</p><router-link :to="`/projects/${projectId}/requirements`" class="row-link">查看项目需求 →</router-link></div>
      </div>
    </section>
  </main>
</template>
