# AITC Lite

按模块逐步实现的 AI 测试工作台。前端 Vue 3 + TypeScript + Element Plus，后端 FastAPI + SQLAlchemy + SQLite。

**当前实现模块 01：项目管理、模块 02：项目需求文本管理、模块 03：生成模型配置与连接验证。** 本轮交付模块 03，完成验收和学习后再进入下一模块。

## 启动

需要 Python 3.11+、Node.js 22.12+（也可用 Node 24）。以下终端均从本目录 `aitc-lite/` 开始操作。

首次安装：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements-dev.txt
cd web
npm ci
```

终端一，启动后端：

```bash
source .venv/bin/activate
cd backend
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

终端二，启动前端：

```bash
cd web
npm run dev
```

访问前端 <http://127.0.0.1:5174>，接口文档 <http://127.0.0.1:8001/docs>。在终端按 Ctrl+C 停止对应服务。前端 `/api` 由 Vite 代理到后端，因此无需额外配置 CORS。

使用独立端口，不占原项目的 8000/5173。开发数据位于 `backend/data/aitc-lite.sqlite3`，不读取原项目 `.env` 或数据库。单人本地使用，开发服务只监听回环地址。

## 检查

后端，从 `aitc-lite/` 开始：

```bash
source .venv/bin/activate
cd backend
python -m unittest discover -s tests -v
```

前端，从 `aitc-lite/` 开始：

```bash
cd web
npm run build
```

后端测试使用临时数据库，不污染开发数据。`build` 包含 TypeScript/Vue 类型检查和生产构建。`npm run preview` 仅预览静态产物；本模块联调和验收使用 `npm run dev` 的 API 代理。

## 当前功能

- 项目列表，按最近更新时间排序。
- 新建、编辑项目名称和描述，前后端校验输入。
- 按名称或描述在当前项目列表中搜索。
- 删除二次确认；取消不会删除。
- SQLite 持久化，刷新或重启后数据保留。
- 加载、空列表、搜索无结果、请求错误与重试状态。
- 点击项目名称或“需求”进入项目需求页面，直接刷新地址仍停留在该项目。
- 在项目下创建、查看、编辑、搜索标题和删除文本需求，正文保留空格、缩进与换行。
- 未保存的需求修改在关闭编辑器、切换页面和刷新时有提醒；保存失败保留输入。
- 删除项目会级联删除所属需求，确认框明确提示这一影响。
- 模型设置：保存生成模型根地址、模型标识、Key、超时与显式 Mock 开关。
- 测试已保存的配置；区分 Mock 流程验证和真实对话连接，失败不会自动启用 Mock。
- Key 留空保留、输入新值替换、勾选清除；读取和错误响应不回显原 Key。

不允许纯空白名称；项目名称最多 100 字、描述最多 2000 字。需求标题最多 200 字，正文最多 50000 字，两者均不能为空或纯空白。项目和需求允许重名，靠 ID 区分。删除不可恢复；需求之间及不同项目之间互不影响。

从模块 01 升级：在 `web/` 执行 `npm ci` 安装新增的 Vue Router，重启后端即可自动创建新增的需求表，已有项目保留。若服务以 `--reload` 运行，代码更新时会自动重启。

模块 03 将 HTTPX 加入后端运行依赖，按首次安装命令更新依赖并重启后端即可新增配置表。访问侧栏“模型设置”，或直接进入 <http://127.0.0.1:5174/settings>。第一次不默认启用 Mock，也不读取原项目的模型凭据。

## 学习入口

- [模块 01：项目管理](docs/01-项目管理.md)
- [模块 02：项目需求文本管理](docs/02-项目需求文本管理.md)
- [模块 03：生成模型配置](docs/03-生成模型配置.md)

每篇包含阅读顺序、请求链路和手工验收清单。模块 02 引入 Vue Router，并把项目列表从 `App.vue` 移至 `src/pages/ProjectList.vue`，`App.vue` 仅保留共用布局。

后续模块建议顺序：

1. 项目管理（已交付）。
2. 需求录入与持久化（文本版已交付；文档导入另行扩展）。
3. 生成模型配置与连接验证（当前，待用户验收）。
4. 功能点拆解、编辑与人工确认。
5. 基础用例生成、评审与导出。
6. 持久化生成任务、进度、暂停及断点恢复。
7. 知识入库与分块、向量检索、BM25、RRF、Rerank，逐项验收。
8. 专项 Skill 与 AI Judge，分别验收。
9. 项目 Agent 与 MCP 客户端，分别验收。
10. 测试任务及人工执行管理。
11. 离线评测及运行对比。

以上顺序可由用户在每次验收后调整。总范围以原项目 `docs/AITC-Lite方案.md` 为准。

