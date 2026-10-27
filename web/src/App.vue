<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AgentDrawer from './components/AgentDrawer.vue'

const route = useRoute()
const assistantOpen = ref(false)
const projectId = computed(() => { const id=Number(route.params.projectId);return Number.isSafeInteger(id)&&id>0?id:undefined })
watch([projectId,()=>route.query.agent],([id,open],previous)=>{if(id!==previous?.[0])assistantOpen.value=false;if(id&&open==='1')assistantOpen.value=true},{immediate:true})
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark">A</span><div>AITC <span class="brand-lite">Lite</span><small>AI 测试工作台</small></div></div>
      <div class="nav-label">工作空间</div>
      <router-link to="/" class="nav-item" :class="{ active: !route.path.startsWith('/settings') }" :aria-current="route.path === '/' ? 'page' : undefined"><span class="nav-icon">▦</span>项目管理</router-link>
      <router-link to="/settings" class="nav-item" :class="{ active: route.path === '/settings' }" :aria-current="route.path === '/settings' ? 'page' : undefined"><span class="nav-icon">⚙</span>模型设置</router-link>
      <router-link to="/settings/embedding" class="nav-item" :class="{ active: route.path === '/settings/embedding' }" :aria-current="route.path === '/settings/embedding' ? 'page' : undefined"><span class="nav-icon">◇</span>Embedding 配置</router-link>
      <router-link to="/settings/rerank" class="nav-item" :class="{ active: route.path === '/settings/rerank' }" :aria-current="route.path === '/settings/rerank' ? 'page' : undefined"><span class="nav-icon">↕</span>Rerank 配置</router-link>
      <router-link to="/settings/judge" class="nav-item" :class="{ active: route.path === '/settings/judge' }"><span class="nav-icon">✓</span>Judge 配置</router-link>
      <router-link to="/settings/mcp" class="nav-item"><span class="nav-icon">⇄</span>MCP 服务</router-link>
      <div class="sidebar-note"><span class="status-dot"></span>本地工作空间<small>数据保存在本机 SQLite</small></div>
      <div class="sidebar-footer">AITC Lite <span>v0.21.0</span></div>
    </aside>
    <div class="main-shell">
      <header class="topbar">
        <span><router-link to="/" class="breadcrumb-link">工作空间</router-link><span class="breadcrumb-slash">/</span><strong>{{ route.meta.title }}</strong></span>
        <div class="topbar-actions"><span class="module-badge">{{ route.meta.module }}</span><el-button v-if="projectId" type="primary" plain @click="assistantOpen=true">项目助手</el-button></div>
      </header>
      <router-view :key="route.path" />
      <AgentDrawer v-if="projectId" :key="projectId" v-model="assistantOpen" :project-id="projectId" />
    </div>
  </div>
</template>

<style scoped>
.topbar-actions { display:flex;gap:14px;align-items:center; }
</style>
