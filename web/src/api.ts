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
export type SkillId = 'api' | 'security'

export interface CaseGenerateInput {
  request_id: string
  feature_id: string
  expected_revision: number
  input_hash: string
  strategy: CaseStrategy
  skill_ids?: SkillId[]
  use_rag?: boolean
  rag_query?: string
  rag_top_k?: number
  rag_rerank?: boolean
  rag_allow_empty?: boolean
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
  skill_names: string[]
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
  knowledge: RagEvidence | null
  skills: GenerationSkillsEvidence | null
}

async function requestResponse(path: string, options: RequestInit = {}, timeoutMs = 15_000): Promise<Response> {
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
  return response
}

async function request<T>(path: string, options: RequestInit = {}, timeoutMs = 15_000): Promise<T> {
  const response = await requestResponse(path, options, timeoutMs)
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

export type CaseExportFormat = 'xlsx' | 'markdown'

export async function exportAcceptedCases(projectId: number, ids: string[], format: CaseExportFormat) {
  const response = await requestResponse(`/projects/${projectId}/testcases/export`, {
    method: 'POST', body: JSON.stringify({ case_ids: ids, format }),
  }, 60_000)
  const expected = format === 'xlsx' ? 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' : 'text/markdown'
  if (!response.headers.get('Content-Type')?.includes(expected)) throw new Error('导出响应格式异常，请确认后端正常运行。')
  let blob: Blob
  try { blob = await response.blob() } catch { throw new Error('文件下载中断，请重试导出。') }
  if (!blob.size) throw new Error('导出文件为空，请重试。')
  let filename = `已采纳用例.${format === 'xlsx' ? 'xlsx' : 'md'}`
  const encoded = response.headers.get('Content-Disposition')?.match(/filename\*=UTF-8''([^;]+)/i)?.[1]
  if (encoded) {
    try { filename = decodeURIComponent(encoded) } catch { /* 使用本地默认文件名 */ }
  }
  return { blob, filename }
}



export interface GenerationTask {
  skill_ids: SkillId[]
  id: string
  requirement_id: number
  feature_name: string
  strategy: CaseStrategy
  source: 'real' | 'mock'
  model: string
  status: 'queued' | 'running' | 'paused' | 'succeeded' | 'failed' | 'interrupted'
  stage: string
  progress: number
  completed_nodes: string[]
  error_message: string
  pause_requested: boolean
  can_resume: boolean
  requires_model_retry: boolean
  requires_knowledge_retry: boolean
  checkpoint_nodes: string[]
  resume_block_reason: string
  created_at: string
  updated_at: string
}
const taskPath = (projectId: number, requirementId: number) => `/projects/${projectId}/requirements/${requirementId}/generation-tasks`
export const generationTasksApi = {
  pause: (projectId: number, requirementId: number, id: string) => request<GenerationTask>(
    `${taskPath(projectId, requirementId)}/${id}/pause`, { method: 'POST' },
  ),
  resume: (projectId: number, requirementId: number, id: string, retryUncertain = false, retryKnowledge = false) => request<GenerationTask>(
    `${taskPath(projectId, requirementId)}/${id}/resume`, { method: 'POST', body: JSON.stringify({ retry_uncertain_model_call: retryUncertain, retry_uncertain_knowledge_call: retryKnowledge }) },
  ),
  list: (projectId: number, requirementId: number) => request<GenerationTask[]>(taskPath(projectId, requirementId)),
  submit: (projectId: number, requirementId: number, payload: CaseGenerateInput) => request<GenerationTask>(
    taskPath(projectId, requirementId), { method: 'POST', body: JSON.stringify(payload) },
  ),
}

export interface KnowledgeDocumentInput {
  title: string
  content: string
  source_filename: string
}
export interface KnowledgeDocumentSummary {
  id: string
  project_id: number
  title: string
  source_filename: string
  revision: number
  char_count: number
  chunk_count: number
  chunker_version: string
  chunk_size: number
  chunk_overlap: number
  created_at: string
  updated_at: string
}
export interface KnowledgeChunk {
  id: string
  document_revision: number
  position: number
  start_offset: number
  end_offset: number
  content: string
  content_hash: string
}
export interface KnowledgeDocument extends KnowledgeDocumentSummary {
  content: string
  content_hash: string
  chunks: KnowledgeChunk[]
}
const knowledgePath = (projectId: number) => `/projects/${projectId}/knowledge-documents`
export const knowledgeApi = {
  list: (projectId: number) => request<KnowledgeDocumentSummary[]>(knowledgePath(projectId)),
  get: (projectId: number, id: string) => request<KnowledgeDocument>(`${knowledgePath(projectId)}/${id}`),
  create: (projectId: number, documentId: string, data: KnowledgeDocumentInput) => request<KnowledgeDocument>(
    knowledgePath(projectId), { method: 'POST', body: JSON.stringify({ ...data, document_id: documentId }) },
  ),
  update: (projectId: number, id: string, revision: number, data: KnowledgeDocumentInput) => request<KnowledgeDocument>(
    `${knowledgePath(projectId)}/${id}`, { method: 'PUT', body: JSON.stringify({ ...data, expected_revision: revision }) },
  ),
  remove: (projectId: number, id: string, revision: number) => request<void>(
    `${knowledgePath(projectId)}/${id}?expected_revision=${revision}`, { method: 'DELETE' },
  ),
}

export interface KnowledgeSearchHit {
  rank: number
  score: number
  matched_tokens: string[]
  chunk_id: string
  document_id: string
  document_title: string
  source_filename: string
  document_revision: number
  position: number
  start_offset: number
  end_offset: number
  content: string
  content_hash: string
}
export interface KnowledgeSearchResult {
  project_id: number
  query: string
  top_k: number
  query_tokens: string[]
  algorithm: string
  tokenizer: string
  k1: number
  b: number
  corpus_signature: string
  corpus_chunk_count: number
  indexed_chunk_count: number
  total_matches: number
  elapsed_ms: number
  status: 'matched' | 'no_matches' | 'no_terms' | 'empty_corpus' | 'no_indexable_chunks'
  message: string
  hits: KnowledgeSearchHit[]
}
export const knowledgeSearchApi = {
  search: (projectId: number, query: string, topK: number) => request<KnowledgeSearchResult>(
    `/projects/${projectId}/knowledge-search`, { method: 'POST', body: JSON.stringify({ query, top_k: topK }) }, 30_000,
  ),
}

export interface EmbeddingSettings extends GenerationSettings {
  expected_dimensions: number | null
}
export interface EmbeddingSettingsInput extends GenerationSettingsInput {
  expected_dimensions: number | null
}
export interface EmbeddingTestResult {
  mode: 'real' | 'mock'
  message: string
  dimensions: number
  vector_count: number
  elapsed_ms: number
}
export const embeddingSettingsApi = {
  get: () => request<EmbeddingSettings>('/settings/embedding'),
  save: (data: EmbeddingSettingsInput) => request<EmbeddingSettings>('/settings/embedding', {
    method: 'PUT', body: JSON.stringify(data),
  }),
  test: (timeoutSeconds: number) => request<EmbeddingTestResult>('/settings/embedding/test', {
    method: 'POST',
  }, (timeoutSeconds + 5) * 1000),
}

export interface VectorBuild {
  id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted'
  completed: number
  total: number
  error: string
}
export interface VectorIndexStatus {
  status: 'missing' | 'unconfigured' | 'empty' | 'stale' | 'ready'
  message: string
  chunk_count: number
  indexed_chunk_count: number
  dimensions: number | null
  model: string
  mock_mode: boolean
  updated_at: string | null
  task: VectorBuild | null
}
export interface VectorSearchResult {
  query: string
  model: string
  mock_mode: boolean
  dimensions: number
  elapsed_ms: number
  message: string
  hits: KnowledgeSearchHit[]
}
const vectorPath = (id: number) => `/projects/${id}/vector-index`
export const vectorApi = {
  status: (id: number) => request<VectorIndexStatus>(vectorPath(id)),
  build: (id: number, requestId: string) => request<VectorBuild>(`${vectorPath(id)}/builds`, {
    method: 'POST', body: JSON.stringify({ request_id: requestId }),
  }),
  search: (id: number, query: string, topK: number) => request<VectorSearchResult>(`${vectorPath(id)}/search`, {
    method: 'POST', body: JSON.stringify({ query, top_k: topK }),
  }, 70_000),
}

export interface HybridSearchHit extends KnowledgeSearchHit {
  rrf_rank: number
  rerank_score: number | null
  bm25_rank: number | null
  bm25_score: number | null
  bm25_contribution: number
  vector_rank: number | null
  vector_score: number | null
  vector_contribution: number
}
export interface HybridRoute {
  status: 'ok' | 'empty' | 'no_terms' | 'error' | 'unconfigured' | 'missing' | 'stale'
  message: string
  candidate_count: number
}
export interface HybridSearchResult {
  embedding_model: string
  query: string
  top_k: number
  candidate_k: number
  rrf_k: number
  algorithm: string
  corpus_signature: string
  corpus_chunk_count: number
  unique_candidate_count: number
  overlap_count: number
  query_tokens: string[]
  degraded: boolean
  mock_mode: boolean
  mode: 'fused' | 'bm25_only' | 'vector_only' | 'no_results' | 'empty'
  message: string
  elapsed_ms: number
  routes: Record<'bm25' | 'vector', HybridRoute>
  rerank: RerankResult
  hits: HybridSearchHit[]
}
export const hybridSearchApi = {
  search: (id: number, query: string, topK: number, candidateK: number, useRerank = false, rerankK = 20) => request<HybridSearchResult>(
    `/projects/${id}/hybrid-search`, { method: 'POST', body: JSON.stringify({ query, top_k: topK, candidate_k: candidateK, use_rerank: useRerank, rerank_k: rerankK }) }, 130_000,
  ),
}

export type RerankSettings = GenerationSettings
export type RerankSettingsInput = GenerationSettingsInput
export interface RerankTestResult {
  mode: 'real' | 'mock'
  message: string
  document_count: number
  order: number[]
  scores: number[]
  elapsed_ms: number
}
export interface RerankResult {
  status: 'disabled' | 'unconfigured' | 'empty' | 'applied' | 'fallback'
  message: string
  model: string
  mock_mode: boolean
  candidate_count: number
  elapsed_ms: number
}
export const rerankSettingsApi = {
  get: () => request<RerankSettings>('/settings/rerank'),
  save: (data: RerankSettingsInput) => request<RerankSettings>('/settings/rerank', { method: 'PUT', body: JSON.stringify(data) }),
  test: (timeout: number) => request<RerankTestResult>('/settings/rerank/test', { method: 'POST' }, (timeout + 5) * 1000),
}

export interface RagSource extends HybridSearchHit {
  citation_id: string
  current_status: 'current' | 'updated' | 'deleted'
}
export interface RagEvidence extends Omit<HybridSearchResult, 'hits'> {
  context_version: string
  sources: RagSource[]
  case_citations: Record<string, string[]>
  outdated: boolean
}

export interface SkillDefinition {
  id: SkillId
  name: string
  version: string
  handler_version: string
  description: string
  focus: string[]
  content_hash: string
}
export interface SkillSnapshot extends SkillDefinition { prompt: string }
export interface GenerationSkillsEvidence {
  items: SkillSnapshot[]
  case_tags: Record<string, SkillId[]>
}
export const skillsApi = { list: () => request<SkillDefinition[]>('/skills') }

export interface JudgeSettings extends GenerationSettings {
  use_generation_model: boolean
  effective_source: 'generation' | 'independent' | 'unconfigured'
  effective_model: string
  effective_mock_mode: boolean | null
  effective_timeout_seconds: number | null
}
export interface JudgeSettingsInput extends GenerationSettingsInput { use_generation_model: boolean }
export const judgeSettingsApi = {
  get: () => request<JudgeSettings>('/settings/judge'),
  save: (data: JudgeSettingsInput) => request<JudgeSettings>('/settings/judge', { method: 'PUT', body: JSON.stringify(data) }),
  test: (timeout: number) => request<ConnectionTestResult>('/settings/judge/test', { method: 'POST' }, (timeout + 5) * 1000),
}
export interface JudgeDimension { score: number; reason: string }
export interface JudgeItem {
  case_id: string
  relevance: JudgeDimension
  executability: JudgeDimension
  verifiability: JudgeDimension
  hallucinations: string[]
  suggestions: string[]
  average_score: number
  usable: boolean
}
export interface JudgeReport {
  items: JudgeItem[]
  checked_count: number
  usable_count: number
  suspect_count: number
  average_score: number
  threshold: number
  rubric_version: string
}
export interface JudgeRuleFinding { code: string; case_id: string | null; related_case_id: string | null; message: string }
export interface JudgeInputSnapshot {
  generation_id: string
  requirement: { title: string; content: string }
  feature: FeatureItem
  generation_outdated: boolean
  generation_source: string
  cases: Array<{ case_id: string; review_revision: number; review_status: string; was_edited: boolean; fields: CaseFields }>
  rule_findings: JudgeRuleFinding[]
  rules_version: string
}
export interface JudgeRunSummary {
  id: string
  generation_id: string
  retry_of: string | null
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted'
  input_hash: string
  model_source: 'generation' | 'independent'
  model: string
  mock_mode: boolean
  prompt_version: string
  rubric_version: string
  error_message: string
  model_call_pending: boolean
  elapsed_ms: number | null
  created_at: string
  outdated: boolean
  checked_count: number | null
  usable_count: number | null
}
export interface JudgeRun extends JudgeRunSummary {
  input_snapshot: JudgeInputSnapshot
  config_snapshot: { base_url: string; model: string; model_source: string; mock_mode: boolean; timeout_seconds: number }
  prompt_snapshot: string
  report: JudgeReport | null
}
const judgePath = (pid: number, rid: number, gid: string) => `/projects/${pid}/requirements/${rid}/case-generations/${gid}/judge-runs`
export const judgeApi = {
  list: (pid: number, rid: number, gid: string) => request<JudgeRunSummary[]>(judgePath(pid, rid, gid)),
  get: (pid: number, rid: number, gid: string, id: string) => request<JudgeRun>(`${judgePath(pid,rid,gid)}/${id}`),
  submit: (pid: number, rid: number, gid: string, id: string, retryOf: string | null) => request<JudgeRun>(judgePath(pid,rid,gid), {
    method: 'POST', body: JSON.stringify({ request_id: id, retry_of: retryOf }),
  }),
}

export interface AgentSessionSummary { id: string; project_id: number; title: string; created_at: string; updated_at: string }
export interface AgentToolCall {
  id: string
  source_id: string
  tool_name: string
  arguments: Record<string, unknown>
  status: 'running' | 'awaiting_approval' | 'unknown' | 'succeeded' | 'failed' | 'interrupted'
  mcp: McpInvocation | null
  result: Record<string, unknown> | null
  error_message: string
  elapsed_ms: number | null
  started_at: string
  updated_at: string
}
export interface AgentTurn {
  id: string
  session_id: string
  question: string
  answer: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted'
  error_message: string
  model: string
  mock_mode: boolean
  model_calls: number
  tool_calls: number
  model_call_pending: boolean
  prompt_version: string
  created_at: string
  tools: AgentToolCall[]
}
export interface AgentSession extends AgentSessionSummary { turns: AgentTurn[] }
const agentPath = (pid: number) => `/projects/${pid}/agent-sessions`
export const agentApi = {
  list: (pid: number) => request<AgentSessionSummary[]>(agentPath(pid)),
  create: (pid: number, id: string) => request<AgentSessionSummary>(agentPath(pid), { method: 'POST', body: JSON.stringify({ session_id: id }) }),
  get: (pid: number, id: string) => request<AgentSession>(`${agentPath(pid)}/${id}`),
  submit: (pid: number, sid: string, id: string, message: string) => request<AgentTurn>(`${agentPath(pid)}/${sid}/turns`, { method: 'POST', body: JSON.stringify({ request_id: id, message }) }),
  decide: (pid: number,sid: string,tid: string,tool: AgentToolCall,approve: boolean) => request<AgentTurn>(`${agentPath(pid)}/${sid}/turns/${tid}/tools/${tool.id}/decision`,{method:'POST',body:JSON.stringify({approval_token:tool.mcp?.approval_token,argument_hash:tool.mcp?.argument_hash,approve})}),
  turn: (pid: number, sid: string, id: string) => request<AgentTurn>(`${agentPath(pid)}/${sid}/turns/${id}`),
  eventsUrl: (pid: number, sid: string, id: string) => `/api${agentPath(pid)}/${sid}/turns/${id}/events`,
}

export interface McpInvocation { server_name:string; remote_tool:string; mode:'readonly'|'write'|'unknown'; approval_token:string; argument_hash:string; decision:string; decided_at:string|null; dispatched:boolean; description:string }
export interface McpTool { name:string; description:string; inputSchema:Record<string,unknown>; annotations:Record<string,unknown> }
export interface McpServer { id:string; name:string; transport:'stdio'|'http'; endpoint:string; command:string; args:string[]; credential_names:string[]; enabled:boolean; timeout_seconds:number; max_result_chars:number; revision:number; tools:McpTool[]; protocol_version:string }
export interface McpServerInput { name:string; transport:'stdio'|'http'; endpoint:string; command:string; args:string[]; credentials:Record<string,string>|null; clear_credentials:boolean; enabled:boolean; timeout_seconds:number; max_result_chars:number; revision?:number }
export interface McpGrant { enabled:boolean; mode:'readonly'|'write'|'unknown' }
export interface McpBinding { server:McpServer; revision:number; valid:boolean; tools:Record<string,McpGrant> }
export const mcpApi = {
  list:()=>request<McpServer[]>('/settings/mcp'),
  add:(data:McpServerInput)=>request<McpServer>('/settings/mcp',{method:'POST',body:JSON.stringify(data)}),
  edit:(id:string,data:McpServerInput)=>request<McpServer>(`/settings/mcp/${id}`,{method:'PUT',body:JSON.stringify(data)}),
  remove:(id:string)=>request<void>(`/settings/mcp/${id}`,{method:'DELETE'}),
  discover:(id:string)=>request<McpServer>(`/settings/mcp/${id}/discover`,{method:'POST'},50_000),
  bindings:(pid:number)=>request<McpBinding[]>(`/projects/${pid}/mcp-bindings`),
  bind:(pid:number,b:McpBinding)=>request(`/projects/${pid}/mcp-bindings/${b.server.id}`,{method:'PUT',body:JSON.stringify({revision:b.revision,server_revision:b.server.revision,tools:b.tools})}),
}

export type ExecutionStatus='not_run'|'passed'|'failed'|'blocked'
export interface ExecutionStats {not_run:number;passed:number;failed:number;blocked:number;total:number;recorded:number;pass_rate:number|null;pass_rate_denominator:number}
export interface ExecutionTaskInput {task_id:string;name:string;environment:string;executor:string;note:string;case_ids:string[];acknowledge_outdated:boolean}
export interface ExecutionTaskSummary {id:string;project_id:number;name:string;environment:string;executor:string;note:string;status:'open'|'completed';revision:number;created_at:string;updated_at:string;completed_at:string|null;stats:ExecutionStats;recording_mode:'manual'}
export interface ExecutionSnapshot {case_id:string;generation_id:string;requirement_id:number;requirement_title:string;module:string;feature_name:string;source:string;was_edited:boolean;review_revision:number;outdated_at_creation:boolean;fields:CaseFields}
export interface ExecutionItem {id:string;task_id:string;position:number;snapshot:ExecutionSnapshot;status:ExecutionStatus;actual_result:string;defect_note:string;revision:number;recorded_at:string|null}
export interface ExecutionTask extends ExecutionTaskSummary {items:ExecutionItem[]}
export interface ExecutionResultInput {request_id:string;expected_revision:number;status:ExecutionStatus;actual_result:string;defect_note:string}
export interface ExecutionEvent {id:string;revision:number;status:ExecutionStatus;actual_result:string;defect_note:string;created_at:string}
const executionPath=(pid:number)=>`/projects/${pid}/execution-tasks`
export const executionApi={
 list:(pid:number,offset=0)=>request<{total:number;items:ExecutionTaskSummary[]}>(`${executionPath(pid)}?offset=${offset}&limit=20`),
 create:(pid:number,data:ExecutionTaskInput)=>request<ExecutionTask>(executionPath(pid),{method:'POST',body:JSON.stringify(data)}),
 get:(pid:number,id:string)=>request<ExecutionTask>(`${executionPath(pid)}/${id}`),
 save:(pid:number,tid:string,iid:string,data:ExecutionResultInput)=>request<{item:ExecutionItem;task_revision:number;replayed:boolean}>(`${executionPath(pid)}/${tid}/items/${iid}`,{method:'PUT',body:JSON.stringify(data)}),
 history:(pid:number,tid:string,iid:string)=>request<ExecutionEvent[]>(`${executionPath(pid)}/${tid}/items/${iid}/history`),
 complete:(pid:number,id:string,revision:number)=>request<ExecutionTask>(`${executionPath(pid)}/${id}/complete`,{method:'POST',body:JSON.stringify({expected_revision:revision})}),
}
