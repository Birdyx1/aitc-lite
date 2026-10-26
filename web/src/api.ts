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

export interface FeatureItem {
  id: string
  module: string
  feature: string
  description: string
  acceptance_criteria: string
  constraints: string
  priority: 'P0' | 'P1' | 'P2'
  included: boolean
}

export type FeatureSource = 'manual' | 'mock' | 'real'

export interface FeatureSheet {
  revision: number
  current_hash: string
  source_hash: string
  source: FeatureSource
  model: string
  confirmed: boolean
  stale: boolean
  items: FeatureItem[]
}

export interface FeatureSaveInput {
  expected_revision: number
  input_hash: string
  items: FeatureItem[]
  source: FeatureSource
  model: string
  confirmed: boolean
}

export interface FeaturePreview {
  base_revision: number
  input_hash: string
  source: 'mock' | 'real'
  model: string
  items: FeatureItem[]
}

export type CaseStrategy = 'full' | 'quick'

export interface CaseGenerateInput {
  request_id: string
  feature_id: string
  expected_revision: number
  input_hash: string
  strategy: CaseStrategy
}

export interface CaseFields {
  title: string
  priority: 'P0' | 'P1' | 'P2'
  case_type: 'functional' | 'boundary' | 'exception'
  is_smoke: boolean
  precondition: string
  steps: string[]
  expected_result: string
}

export interface CaseDraft extends CaseFields { id: string }

export interface CaseReview {
  draft_id: string
  revision: number
  status: 'pending' | 'accepted' | 'rejected'
  original: CaseFields
  current: CaseFields
  was_edited: boolean
  rejection_reason: string
}

export interface CaseReviews { generation_id: string; items: CaseReview[] }

export interface CaseReviewInput {
  expected_revision: number
  action: 'edit' | 'accept' | 'reject'
  fields?: CaseFields
  rejection_reason?: string
}

export interface AcceptedCase extends CaseFields {
  id: string
  generation_id: string
  requirement_id: number
  requirement_title: string
  module: string
  feature_name: string
  source: 'real' | 'mock'
  outdated: boolean
  was_edited: boolean
}

export interface CaseGenerationSummary {
  id: string
  feature_name: string
  strategy: CaseStrategy
  source: 'real' | 'mock'
  model: string
  case_count: number
  created_at: string
  outdated: boolean
}

export interface CaseGeneration extends CaseGenerationSummary {
  sheet_revision: number
  input_hash: string
  feature_snapshot: FeatureItem
  requirement_snapshot: { title: string; content: string }
  prompt_version: string
  graph_nodes: string[]
  cases: CaseDraft[]
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

const featurePath = (projectId: number, requirementId: number) => `/projects/${projectId}/requirements/${requirementId}/features`

export const featuresApi = {
  get: (projectId: number, requirementId: number) => request<FeatureSheet>(featurePath(projectId, requirementId)),
  analyze: (projectId: number, requirementId: number, timeoutSeconds: number) => request<FeaturePreview>(
    `${featurePath(projectId, requirementId)}/analyze`, { method: 'POST' }, (timeoutSeconds + 5) * 1000,
  ),
  save: (projectId: number, requirementId: number, data: FeatureSaveInput) => request<FeatureSheet>(
    featurePath(projectId, requirementId), { method: 'PUT', body: JSON.stringify(data) },
  ),
}

const casePath = (projectId: number, requirementId: number) => `/projects/${projectId}/requirements/${requirementId}/case-generations`

export const caseGenerationsApi = {
  list: (projectId: number, requirementId: number) => request<CaseGenerationSummary[]>(casePath(projectId, requirementId)),
  get: (projectId: number, requirementId: number, id: string) => request<CaseGeneration>(`${casePath(projectId, requirementId)}/${id}`),
  generate: (projectId: number, requirementId: number, payload: CaseGenerateInput, timeoutSeconds: number) => request<CaseGeneration>(
    casePath(projectId, requirementId), { method: 'POST', body: JSON.stringify(payload) }, (timeoutSeconds + 10) * 1000,
  ),
}

export const reviewsApi = {
  list: (projectId: number, requirementId: number, generationId: string) => request<CaseReviews>(`${casePath(projectId, requirementId)}/${generationId}/reviews`),
  save: (projectId: number, requirementId: number, generationId: string, draftId: string, data: CaseReviewInput) => request<CaseReview>(
    `${casePath(projectId, requirementId)}/${generationId}/reviews/${draftId}`, { method: 'PUT', body: JSON.stringify(data) },
  ),
  accepted: (projectId: number) => request<AcceptedCase[]>(`/projects/${projectId}/testcases`),
}

