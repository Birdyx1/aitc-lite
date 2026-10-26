import { createRouter, createWebHistory } from 'vue-router'
import ProjectList from './pages/ProjectList.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: ProjectList, meta: { title: '项目管理', module: '模块 01 · 项目管理' } },
    {
      path: '/projects/:projectId/requirements',
      component: () => import('./pages/ProjectRequirements.vue'),
      meta: { title: '需求管理', module: '模块 02 · 需求文本管理' },
    },
    {
      path: '/settings', component: () => import('./pages/Settings.vue'),
      meta: { title: '模型设置', module: '模块 03 · 生成模型配置' },
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.afterEach((to, _from, failure) => {
  if (!failure) document.title = `${to.meta.title} · AITC Lite`
})
