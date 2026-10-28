import { createRouter, createWebHistory } from 'vue-router'
import ProjectList from './pages/ProjectList.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {path:'/projects/:projectId/overview',component:()=>import('./pages/ProjectWorkbench.vue'),meta:{title:'项目工作台',module:'模块 23 · 验收进度与下一步'}},
    { path: '/', component: ProjectList, meta: { title: '项目管理', module: '模块 01 · 项目管理' } },
    {
      path: '/projects/:projectId/requirements',
      component: () => import('./pages/ProjectRequirements.vue'),
      meta: { title: '需求管理', module: '模块 02 · 需求文本管理' },
    },
    {
      path: '/projects/:projectId/requirements/:requirementId/features',
      component: () => import('./pages/RequirementFeatures.vue'),
      meta: { title: '功能点确认', module: '模块 04 · 功能点拆解' },
    },
    {
      path: '/projects/:projectId/requirements/:requirementId/cases',
      component: () => import('./pages/CaseGeneration.vue'),
      meta: { title: '用例生成与评审', module: '模块 05–09 / 16–17 · 专项与知识增强生成' },
    },
    {
      path: '/projects/:projectId/testcases', component: () => import('./pages/ProjectTestcases.vue'),
      meta: { title: '已采纳用例', module: '模块 06–07 · 评审与导出' },
    },
    {
      path: '/projects/:projectId/knowledge', component: () => import('./pages/ProjectKnowledge.vue'),
      meta: { title: '项目知识库', module: '模块 10–15 · 知识库与混合检索' },
    },
    {
      path: '/settings', component: () => import('./pages/Settings.vue'),
      meta: { title: '模型设置', module: '模块 03 · 生成模型配置' },
    },
    {
      path: '/settings/embedding', component: () => import('./pages/EmbeddingSettings.vue'),
      meta: { title: 'Embedding 配置', module: '模块 12 · Embedding 配置与验证' },
    },
    { path: '/settings/rerank', component: () => import('./pages/RerankSettings.vue'), meta: { title: 'Rerank 配置', module: '模块 15 · 精排配置与验证' } },
    { path: '/settings/judge', component: () => import('./pages/JudgeSettings.vue'), meta: { title: 'Judge 配置', module: '模块 18 · 质量评估配置' } },
    {path:'/projects/:projectId/evaluations',component:()=>import('./pages/Evaluations.vue'),meta:{title:'离线评测',module:'模块 22 · 样本与运行对比'}},
    {path:'/projects/:projectId/evaluations/runs/:runId',component:()=>import('./pages/EvaluationRun.vue'),meta:{title:'评测运行',module:'模块 22 · 独立评测结果'}},
    {path:'/projects/:projectId/execution-tasks',component:()=>import('./pages/ExecutionTasks.vue'),meta:{title:'人工执行任务',module:'模块 21 · 测试任务'}},
    {path:'/projects/:projectId/execution-tasks/:taskId',component:()=>import('./pages/ExecutionTaskDetail.vue'),meta:{title:'人工执行详情',module:'模块 21 · 执行记录'}},
    { path:'/settings/mcp', component:()=>import('./pages/McpSettings.vue'), meta:{title:'MCP 服务',module:'模块 20 · MCP 客户端'} },
    { path:'/projects/:projectId/mcp', component:()=>import('./pages/ProjectMcp.vue'), meta:{title:'项目 MCP 工具',module:'模块 20 · 项目授权'} },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.afterEach((to, _from, failure) => {
  if (!failure) document.title = `${to.meta.title} · AITC Lite`
})
