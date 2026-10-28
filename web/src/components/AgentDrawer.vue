<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { agentApi, projectsApi, settingsApi } from '../api'
import type { AgentSession, AgentSessionSummary, AgentTurn, AgentToolCall, GenerationSettings } from '../api'
import { errorMessage, formatDate } from '../utils'

const props = defineProps<{ projectId: number; modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
const sessions = ref<AgentSessionSummary[]>([])
const conversation = ref<AgentSession>()
const selectedId = ref('')
const projectName = ref('')
const config = ref<GenerationSettings>()
const question = ref('')
const loading = ref(false)
const submitting = ref(false)
const error = ref('')
const liveError = ref('')
let disposed = false
let revision = 0
let liveRevision = 0
let source: EventSource | undefined
let timer: ReturnType<typeof setTimeout> | undefined
const deciding=ref('')
async function decide(turn:AgentTurn,tool:AgentToolCall,approve:boolean){
  if(deciding.value)return
  deciding.value=tool.id;error.value=''
  try{updateTurn(await agentApi.decide(props.projectId,turn.session_id,turn.id,tool,approve))}
  catch(cause){error.value=errorMessage(cause)}finally{deciding.value=''}
}
let pendingSession = ''
let pendingTurn: { id: string; sessionId: string; question: string } | undefined
const active = computed(() => conversation.value?.turns.find(turn => turn.status === 'queued' || turn.status === 'running'))
const canSend = computed(() => !!question.value.trim() && Array.from(question.value.trim()).length <= 2000 && !loading.value && !submitting.value && !active.value && !!config.value?.configured)
const statusNames = { queued:'排队中', running:'运行中', succeeded:'已完成', failed:'失败', interrupted:'已中断' }
const toolStatusNames = { awaiting_approval:'待你确认', unknown:'结果待核实', running:'执行中', succeeded:'已完成', failed:'失败', interrupted:'已中断' }
const toolNames: Record<string,string> = { builtin_project_overview:'项目概况', builtin_list_requirements:'需求列表', builtin_get_requirement:'需求详情', builtin_list_cases:'用例查询', builtin_list_judge_reports:'Judge 查询', builtin_search_knowledge:'知识检索' }
function closeStream() { liveRevision += 1; source?.close(); source=undefined; if (timer) clearTimeout(timer) }
function updateTurn(turn: AgentTurn) {
  if (conversation.value?.id !== turn.session_id) return
  const index=conversation.value.turns.findIndex(item=>item.id===turn.id)
  if (index<0) conversation.value.turns.push(turn)
  else conversation.value.turns[index]=turn
}
function updateTool(turnId: string, tool: AgentToolCall) {
  const turn=conversation.value?.turns.find(item=>item.id===turnId)
  if (!turn) return
  const index=turn.tools.findIndex(item=>item.id===tool.id)
  if(index<0)turn.tools.push(tool)
  else turn.tools[index]=tool
}
function poll(sid: string,id: string,token: number) {
  timer=setTimeout(async()=>{
    if(disposed||token!==liveRevision||!props.modelValue)return
    try {
      const turn=await agentApi.turn(props.projectId,sid,id)
      if(disposed||token!==liveRevision)return
      updateTurn(turn)
      if(turn.status==='queued'||turn.status==='running')poll(sid,id,token)
      else { liveError.value='';closeStream();void refreshList() }
    } catch(cause) { if(!disposed&&token===liveRevision){liveError.value=errorMessage(cause);poll(sid,id,token)} }
  },1500)
}
async function refreshList() {
  try { const list=await agentApi.list(props.projectId);if(!disposed)sessions.value=list } catch { /* 刷新会话名失败不影响已保存答复。 */ }
}
function connect(turn: AgentTurn) {
  closeStream();liveError.value=''
  if(!props.modelValue||!['queued','running'].includes(turn.status))return
  const token=liveRevision
  if(typeof EventSource==='undefined'){poll(turn.session_id,turn.id,token);return}
  const stream=new EventSource(agentApi.eventsUrl(props.projectId,turn.session_id,turn.id));source=stream
  stream.onmessage=event=>{
    if(disposed||token!==liveRevision)return
    try {
      const data=JSON.parse(event.data)
      if(data.type==='snapshot'){
        updateTurn(data.turn)
        if(!['queued','running'].includes(data.turn.status)){closeStream();void refreshList()}
      } else if(data.type==='tool_started'||data.type==='tool_finished')updateTool(data.turn_id,data.tool)
      else if(data.type==='error'){liveError.value=data.message;closeStream()}
      else if(data.type==='done'){closeStream();void refreshList()}
    } catch { liveError.value='实时消息格式异常，请刷新对话核对。';closeStream();poll(turn.session_id,turn.id,liveRevision) }
  }
  stream.onerror=()=>{
    if(disposed||token!==liveRevision)return
    closeStream();liveError.value='实时连接中断，正在只读轮询状态；不会重新发送模型请求。'
    poll(turn.session_id,turn.id,liveRevision)
  }
}
async function openSession(id: string) {
  closeStream();const token=++revision;loading.value=true;error.value=''
  if(conversation.value?.id!==id){question.value='';pendingTurn=undefined}
  try {
    const data=await agentApi.get(props.projectId,id)
    if(disposed||token!==revision)return
    conversation.value=data;selectedId.value=data.id
    const turn=data.turns.find(item=>item.status==='queued'||item.status==='running');if(turn)connect(turn)
  } catch(cause){if(!disposed&&token===revision)error.value=errorMessage(cause)}
  finally{if(!disposed&&token===revision)loading.value=false}
}
async function load() {
  if(submitting.value)return
  loading.value=true;error.value=''
  try {
    const [project,list,settings]=await Promise.all([projectsApi.get(props.projectId),agentApi.list(props.projectId),settingsApi.get()])
    if(disposed)return
    projectName.value=project.name;sessions.value=list;config.value=settings
    const id=list.some(item=>item.id===selectedId.value)?selectedId.value:list[0]?.id
    if(id)await openSession(id)
    else{conversation.value=undefined;selectedId.value='';closeStream()}
  }catch(cause){if(!disposed)error.value=errorMessage(cause)}
  finally{if(!disposed)loading.value=false}
}
async function createConversation() {
  pendingSession ||= crypto.randomUUID()
  const saved=await agentApi.create(props.projectId,pendingSession)
  pendingSession=''
  if(disposed)return
  sessions.value=[saved,...sessions.value.filter(item=>item.id!==saved.id)]
  conversation.value={...saved,turns:[]};selectedId.value=saved.id;closeStream()
  return saved
}
async function newConversation() {
  if(loading.value||submitting.value)return
  loading.value=true;error.value=''
  try{await createConversation();question.value='';pendingTurn=undefined}catch(cause){if(!disposed)error.value=errorMessage(cause)}
  finally{if(!disposed)loading.value=false}
}
async function send() {
  if(!canSend.value)return
  const input=question.value.trim();submitting.value=true;error.value=''
  try {
    const current=conversation.value||await createConversation()
    if(!current||disposed)return
    if(!pendingTurn||pendingTurn.question!==input||pendingTurn.sessionId!==current.id)pendingTurn={id:crypto.randomUUID(),sessionId:current.id,question:input}
    const turn=await agentApi.submit(props.projectId,current.id,pendingTurn.id,input)
    if(disposed)return
    pendingTurn=undefined;question.value='';updateTurn(turn);connect(turn);void refreshList()
  }catch(cause){if(!disposed)error.value=`${errorMessage(cause)} 提交超时请先刷新对话核对；核实相同问题会沿用原请求标识。`}
  finally{if(!disposed)submitting.value=false}
}
watch(()=>props.modelValue,open=>{if(open)void load();else closeStream()},{immediate:true})
onBeforeUnmount(()=>{disposed=true;revision+=1;closeStream()})
</script>

<template>
  <el-drawer :model-value="modelValue" :title="`${projectName || '当前项目'} · Agent 助手`" size="min(620px, 100vw)" :modal="false" modal-penetrable :lock-scroll="false" aria-modal="false" append-to-body :before-close="(done:()=>void)=>{if(!submitting)done()}" @update:model-value="emit('update:modelValue',$event)">
    <div class="agent-drawer-content">
      <p class="field-help">内置工具查询当前项目及人工执行统计；外部 MCP 按项目授权，写入/未知操作逐次确认。使用生成模型，每轮最多 4 次模型调用、6 次工具调用；知识检索可能额外调用 Embedding/Rerank 并计费。</p>
      <el-alert v-if="config?.mock_mode" title="Mock 演示：读取本地项目数据，知识使用 BM25，不连接外部模型或 MCP。" type="warning" :closable="false" />
      <p class="field-help"><router-link :to="`/projects/${projectId}/mcp`">配置本项目 MCP 工具 →</router-link> · 外部数据范围由服务账号权限决定。</p>
      <div class="agent-toolbar"><el-select v-model="selectedId" aria-label="Agent 对话" placeholder="发送问题时创建对话" :disabled="loading||submitting" @change="openSession"><el-option v-for="item in sessions" :key="item.id" :value="item.id" :label="item.title" /></el-select><el-button :disabled="loading||submitting" @click="newConversation">新对话</el-button><el-button :loading="loading" :disabled="submitting" @click="load">刷新对话</el-button></div>
      <el-alert v-if="error" :title="error" type="error" :closable="false" /><el-alert v-if="liveError" :title="liveError" type="warning" :closable="false" />
      <p v-if="!config?.configured&&!loading" class="field-help">请先保存生成模型配置。<router-link to="/settings">前往设置 →</router-link></p>
      <div v-if="!conversation?.turns.length&&!loading" class="agent-empty"><p>可以先查询项目概况，再追问需求、用例或知识。</p><div class="agent-examples"><el-button v-for="text in ['项目概况','有哪些需求','查看已采纳用例','最近 Judge 评估']" :key="text" size="small" :disabled="loading||submitting||!!active" @click="question=text">{{ text }}</el-button></div></div>
      <article v-for="turn in conversation?.turns" :key="turn.id" class="agent-turn">
        <h4>你的问题 · {{ formatDate(turn.created_at) }}</h4><pre class="agent-question">{{ turn.question }}</pre>
        <p class="field-help">{{ statusNames[turn.status] }} · {{ turn.mock_mode?'Mock':turn.model }} · 模型 {{ turn.model_calls }}/4 · 工具 {{ turn.tool_calls }}/6</p>
        <details v-for="tool in turn.tools" :key="tool.id" class="agent-tool" :open="tool.status==='awaiting_approval'"><summary>{{ tool.source_id }} · {{ tool.mcp ? `${tool.mcp.server_name} / ${tool.mcp.remote_tool}` : toolNames[tool.tool_name] || tool.tool_name }} · {{ toolStatusNames[tool.status] }}<span v-if="tool.elapsed_ms!==null"> · {{ tool.elapsed_ms }} ms</span></summary><p class="field-help">{{ tool.mcp ? `外部 ${tool.mcp.mode}` : '内置只读' }} · 开始 {{ formatDate(tool.started_at) }}<span v-if="tool.status!=='running'&&tool.status!=='awaiting_approval'"> → {{ toolStatusNames[tool.status] }} {{ formatDate(tool.updated_at) }}</span></p><template v-if="tool.mcp&&tool.status==='awaiting_approval'"><el-alert title="确认以下服务、工具和参数后才会执行。确认最多等待 120 秒，整轮最多 180 秒（先到为准）；外部写入失败或结果未知时请先核实，避免重复操作。" type="warning" :closable="false"/><p>{{ tool.mcp.description }}</p></template><p>参数</p><pre>{{ JSON.stringify(tool.arguments,null,2) }}</pre><div v-if="tool.mcp&&tool.status==='awaiting_approval'&&tool.mcp.decision==='pending'" class="agent-approval"><el-button type="primary" :loading="deciding===tool.id" :disabled="!!deciding" @click="decide(turn,tool,true)">确认执行本次调用</el-button><el-button :disabled="!!deciding" @click="decide(turn,tool,false)">拒绝本次调用</el-button></div><p v-if="tool.mcp" class="field-help">确认状态：{{ tool.mcp.decision }} · {{ tool.mcp.dispatched?'已进入远端调用阶段，失败/中断需核实结果':'尚未发送远端' }}</p><el-alert v-if="tool.error_message" :title="tool.error_message" type="warning" :closable="false" /><template v-if="tool.result"><p>本次查询结果快照</p><pre>{{ JSON.stringify(tool.result,null,2) }}</pre></template></details>
        <pre v-if="turn.answer" class="agent-answer">{{ turn.answer }}</pre><p v-else-if="turn.status==='queued'||turn.status==='running'" role="status">{{ turn.status==='queued'?'等待后台执行…':'正在查询或整理答复…' }}</p>
        <el-alert v-if="turn.error_message" :title="turn.error_message" type="error" :closable="false" />
        <el-button v-if="turn.status==='failed'||turn.status==='interrupted'" link :disabled="submitting||!!active" @click="question=turn.question">复制问题到输入框（不自动重试）</el-button>
      </article>
      <form class="agent-compose" @submit.prevent="send"><el-input v-model="question" type="textarea" :rows="3" maxlength="2000" show-word-limit aria-label="向项目助手提问" placeholder="查询当前项目，例如：有哪些待评审用例？" :disabled="loading||submitting||!!active" /><div class="agent-send"><span class="field-help">每个对话最多 50 轮；模型只接收最近 4 轮成功历史。答复不会自动写入知识库。</span><el-button native-type="submit" type="primary" :loading="submitting" :disabled="!canSend">发送问题</el-button></div></form>
    </div>
  </el-drawer>
</template>

<style scoped>
.agent-drawer-content { display:flex;flex-direction:column;gap:14px; }
.agent-toolbar { display:flex;gap:8px;align-items:center; }
.agent-toolbar .el-select { min-width:140px;flex:1; }
.agent-examples { display:flex;flex-wrap:wrap;gap:8px; }
.agent-examples .el-button { margin-left:0; }
.agent-turn { border:1px solid #dbe4ed;border-radius:10px;padding:16px; }
.agent-turn h4 { margin:0 0 12px; }
.agent-turn pre { white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.8;font-family:inherit; }
.agent-question { padding:10px;background:#eef4fa;border-radius:8px; }
.agent-tool { margin:10px 0;border-top:1px solid #e1e7ef;padding-top:8px; }
.agent-tool summary { cursor:pointer;color:#315b8f; }
.agent-tool pre { background:#f7f9fc;padding:10px;max-height:300px;overflow:auto;font-size:12px; }
.agent-compose { position:sticky;bottom:0;background:#fff;padding-top:14px;border-top:1px solid #e1e7ef; }
.agent-send { display:flex;justify-content:space-between;gap:12px;align-items:center;margin-top:8px; }
</style>
