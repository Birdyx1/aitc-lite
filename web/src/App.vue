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
      <div class="sidebar-footer">AITC Lite <span>v0.23.0</span></div>
    </aside>
    <div class="main-shell">
      <header class="topbar">
        <span><router-link to="/" class="breadcrumb-link">工作空间</router-link><span class="breadcrumb-slash">/</span><strong>{{ route.meta.title }}</strong></span>
        <div class="topbar-actions"><span class="module-badge">{{ route.meta.module }}</span><el-button v-if="projectId" type="primary" plain @click="assistantOpen=true">项目助手</el-button></div>
      </header>
      <nav v-if="projectId" class="project-navigation" aria-label="项目功能导航">
        <router-link v-for="item in [{path:'overview',label:'项目工作台'},{path:'requirements',label:'需求与生成'},{path:'knowledge',label:'知识库'},{path:'testcases',label:'已采纳用例'},{path:'execution-tasks',label:'测试执行'},{path:'evaluations',label:'离线评测'},{path:'mcp',label:'MCP 工具'}]" :key="item.path" :to="`/projects/${projectId}/${item.path}`" :class="{active:route.path===`/projects/${projectId}/${item.path}`||route.path.startsWith(`/projects/${projectId}/${item.path}/`) }" :aria-current="route.path===`/projects/${projectId}/${item.path}`||route.path.startsWith(`/projects/${projectId}/${item.path}/`)?'page':undefined">{{item.label}}</router-link>
      </nav>
      <router-view :key="route.path" />
      <AgentDrawer v-if="projectId" :key="projectId" v-model="assistantOpen" :project-id="projectId" />
    </div>
  </div>
</template>

<style scoped>
.project-navigation {display:flex;gap:8px;flex-wrap:wrap;padding:12px 40px;background:#fff;border-bottom:1px solid #e7ebf1;}
.project-navigation a {padding:8px 12px;border-radius:6px;color:#61718a;font-size:13px;text-decoration:none;}
.project-navigation a:hover,.project-navigation a.active {background:#edf3fd;color:#315b96;}
.project-navigation a:focus-visible {outline:2px solid #315b96;outline-offset:2px;}
@media(max-width:800px){.project-navigation{padding:10px 16px;gap:4px;}}
.topbar-actions { display:flex;gap:14px;align-items:center; }
</style>
