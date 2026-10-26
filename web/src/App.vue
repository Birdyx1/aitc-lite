<script setup lang="ts">
import { useRoute } from 'vue-router'

const route = useRoute()
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark">A</span><div>AITC <span class="brand-lite">Lite</span><small>AI 测试工作台</small></div></div>
      <div class="nav-label">工作空间</div>
      <router-link to="/" class="nav-item" :class="{ active: route.path !== '/settings' }" :aria-current="route.path === '/' ? 'page' : undefined"><span class="nav-icon">▦</span>项目管理</router-link>
      <router-link to="/settings" class="nav-item" :class="{ active: route.path === '/settings' }" :aria-current="route.path === '/settings' ? 'page' : undefined"><span class="nav-icon">⚙</span>模型设置</router-link>
      <div class="sidebar-note"><span class="status-dot"></span>本地工作空间<small>数据保存在本机 SQLite</small></div>
      <div class="sidebar-footer">AITC Lite <span>v0.3.0</span></div>
    </aside>
    <div class="main-shell">
      <header class="topbar">
        <span><router-link to="/" class="breadcrumb-link">工作空间</router-link><span class="breadcrumb-slash">/</span><strong>{{ route.meta.title }}</strong></span>
        <span class="module-badge">{{ route.meta.module }}</span>
      </header>
      <router-view :key="route.path" />
    </div>
  </div>
</template>
