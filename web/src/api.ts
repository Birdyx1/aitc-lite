export interface Project {
  id: number
  name: string
  description: string
  created_at: string
  updated_at: string
}

export interface ProjectInput {
  name: string
  description: string
}

export interface RequirementSummary {
  id: number
  project_id: number
  title: string
  created_at: string
  updated_at: string
}

export interface RequirementInput {
  title: string
  content: string
}

export interface Requirement extends RequirementSummary, RequirementInput {}

export interface GenerationSettings {
  base_url: string
  model: string
  has_api_key: boolean
  mock_mode: boolean
  timeout_seconds: number
  configured: boolean
}

export interface GenerationSettingsInput {
  base_url: string
  model: string
  api_key: string
  clear_api_key: boolean
  mock_mode: boolean
  timeout_seconds: number
}

export interface ConnectionTestResult {
  mode: 'mock' | 'real'
  message: string
  reply: string
  elapsed_ms: number
}

async function request<T>(path: string, options: RequestInit = {}, timeoutMs = 15_000): Promise<T> {
  let response: Response
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...options.headers },
      signal: AbortSignal.timeout(timeoutMs),
    })
  } catch {
    throw new Error('无法连接服务或请求超时，请确认后端已启动。若正在保存，请刷新列表核实结果后再重试。')
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const message = typeof body.detail === 'string' ? body.detail :
      response.status === 422 ? '输入不符合要求，请检查必填项与字数限制。' :
      `请求失败（${response.status}），请稍后重试。`
    throw new Error(message)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const projectsApi = {
  list: () => request<Project[]>('/projects'),
  get: (id: number) => request<Project>(`/projects/${id}`),
  create: (data: ProjectInput) => request<Project>('/projects', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: ProjectInput) => request<Project>(`/projects/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  remove: (id: number) => request<void>(`/projects/${id}`, { method: 'DELETE' }),
}

export const requirementsApi = {
  list: (projectId: number) => request<RequirementSummary[]>(`/projects/${projectId}/requirements`),
  get: (projectId: number, id: number) => request<Requirement>(`/projects/${projectId}/requirements/${id}`),
  create: (projectId: number, data: RequirementInput) => request<Requirement>(`/projects/${projectId}/requirements`, {
    method: 'POST', body: JSON.stringify(data),
  }),
  update: (projectId: number, id: number, data: RequirementInput) => request<Requirement>(`/projects/${projectId}/requirements/${id}`, {
    method: 'PUT', body: JSON.stringify(data),
  }),
  remove: (projectId: number, id: number) => request<void>(`/projects/${projectId}/requirements/${id}`, { method: 'DELETE' }),
}

export const settingsApi = {
  get: () => request<GenerationSettings>('/settings/generation'),
  save: (data: GenerationSettingsInput) => request<GenerationSettings>('/settings/generation', {
    method: 'PUT', body: JSON.stringify(data),
  }),
  test: (timeoutSeconds: number) => request<ConnectionTestResult>('/settings/generation/test', {
    method: 'POST',
  }, (timeoutSeconds + 5) * 1000),
}

