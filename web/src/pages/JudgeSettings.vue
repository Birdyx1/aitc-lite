<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { judgeSettingsApi } from '../api'
import type { ConnectionTestResult, JudgeSettings, JudgeSettingsInput } from '../api'
import { errorMessage } from '../utils'

const loading = ref(false)
const loaded = ref(false)
const saving = ref(false)
const testing = ref(false)
const loadError = ref('')
const saveError = ref('')
const testError = ref('')
const result = ref<ConnectionTestResult>()
const saved = ref<JudgeSettings>()
const formRef = ref<FormInstance>()
const form = reactive<JudgeSettingsInput>({
  use_generation_model: true, base_url: '', model: '', api_key: '', clear_api_key: false, mock_mode: false, timeout_seconds: 20,
})
const original = ref('')
const dirty = computed(() => loaded.value && JSON.stringify(form) !== original.value)
const busy = computed(() => loading.value || saving.value || testing.value)
const rules: FormRules = {
  base_url: [{ validator: (_rule, value, callback) => {
    if (!form.use_generation_model && !form.mock_mode && !String(value).trim()) return callback(new Error('真实模式需要填写 API 根地址'))
    if (value) {
      try {
        const url = new URL(value)
        if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash
          || /\s/.test(value) || ['/chat/completions', '/embeddings', '/rerank'].some(path => url.pathname.replace(/\/+$/, '').endsWith(path))) throw new Error()
      } catch { return callback(new Error('请填写 HTTP/HTTPS API 根地址，不含凭据、参数或接口路径')) }
    }
    callback()
  }, trigger: 'blur' }],
  model: [{ validator: (_rule, value, callback) => {
    if (!form.use_generation_model && !form.mock_mode && !String(value).trim()) return callback(new Error('真实模式需要填写模型名称'))
    callback()
  }, trigger: 'blur' }],
}


function applySettings(data: JudgeSettings) {
  saved.value = data
  Object.assign(form, {
    use_generation_model: data.use_generation_model, base_url: data.base_url, model: data.model, api_key: '', clear_api_key: false,
    mock_mode: data.mock_mode, timeout_seconds: data.timeout_seconds,
  })
  original.value = JSON.stringify(form)
  loaded.value = true
}

async function confirmDiscard() {
  if (saving.value || testing.value) return false
  if (!dirty.value) return true
  try {
    await ElMessageBox.confirm('Judge 配置尚未保存，确定放弃修改吗？', '放弃修改', {
      confirmButtonText: '放弃修改', cancelButtonText: '继续编辑', type: 'warning',
    })
    return true
  } catch { return false }
}

async function loadSettings() {
  if (!(await confirmDiscard())) return
  loading.value = true
  loadError.value = ''
  saveError.value = ''
  result.value = undefined
  testError.value = ''
  try {
    applySettings(await judgeSettingsApi.get())
    formRef.value?.clearValidate()
  } catch (cause) {
    loadError.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

async function saveSettings() {
  if (busy.value || !(await formRef.value?.validate().catch(() => false))) return
  saving.value = true
  saveError.value = ''
  result.value = undefined
  testError.value = ''
  try {
    applySettings(await judgeSettingsApi.save({ ...form }))
    ElMessage.success('Judge 配置已保存')
  } catch (cause) {
    saveError.value = errorMessage(cause)
  } finally {
    saving.value = false
  }
}

async function testConnection() {
  if (busy.value || dirty.value || saved.value?.effective_source === 'unconfigured') return
  testing.value = true
  result.value = undefined
  testError.value = ''
  try {
    result.value = await judgeSettingsApi.test(saved.value?.effective_timeout_seconds || 20)
  } catch (cause) {
    testError.value = errorMessage(cause)
  } finally {
    testing.value = false
  }
}

watch(() => JSON.stringify(form), () => {
  result.value = undefined
  testError.value = ''
})
watch(() => form.use_generation_model, () => formRef.value?.clearValidate())
watch(() => form.mock_mode, () => formRef.value?.clearValidate())
watch(() => form.clear_api_key, clear => { if (clear) form.api_key = '' })

function warnBeforeUnload(event: BeforeUnloadEvent) {
  if (dirty.value || saving.value || testing.value) {
    event.preventDefault()
    event.returnValue = ''
  }
}

onBeforeRouteLeave(confirmDiscard)
onMounted(() => {
  loadSettings()
  window.addEventListener('beforeunload', warnBeforeUnload)
})
onBeforeUnmount(() => window.removeEventListener('beforeunload', warnBeforeUnload))
</script>

<template>
  <main>
    <div class="page-heading">
      <div><div class="eyebrow">JUDGE SETTINGS</div><h1>连接你的 Judge 模型</h1><p>默认复用生成模型；也可使用独立模型评估相关性、可执行性与可验证性。</p></div>
      <el-button :loading="loading" :disabled="saving || testing" @click="loadSettings">重新加载</el-button>
    </div>
    <p class="field-help feature-notice">复用模式直接使用当前生成配置，不复制 Key；独立模式使用本页配置，失败时不自动切换模型。<router-link to="/settings">返回生成模型设置 →</router-link></p>
    <el-alert v-if="loadError" :title="loadError" type="error" :closable="false" show-icon />
    <section v-else class="project-panel settings-panel" aria-labelledby="settings-title">
      <div class="panel-toolbar">
        <div><h2 id="settings-title">Judge 质量评估模型</h2><p>使用兼容 /chat/completions 的接口</p></div>
        <el-tag>{{ saved?.effective_source === 'generation' ? '复用生成模型' : saved?.effective_source === 'independent' ? '独立 Judge' : '需要配置模型' }}</el-tag>
      </div>
      <div v-if="loading" class="loading-state"><el-skeleton :rows="6" animated /></div>
      <div v-else-if="loaded" class="settings-body">
        <el-alert v-if="saveError" :title="saveError" type="error" :closable="false" show-icon class="form-error" />
        <el-form ref="formRef" :model="form" :rules="rules" label-position="top" :disabled="busy" @submit.prevent="saveSettings">
          <el-form-item label="评估模型来源"><el-switch v-model="form.use_generation_model" aria-label="复用生成模型" /><span class="field-inline-help">{{ form.use_generation_model ? '复用已保存的生成模型（默认）' : '使用独立 Judge 配置' }}</span></el-form-item>
          <p class="field-help">当前实际模型：{{ saved?.effective_model || '未配置或 Mock' }} · {{ saved?.effective_mock_mode ? 'Mock 演示' : '真实模式' }}</p>
          <div v-if="!form.use_generation_model">
          <el-form-item label="Mock 模式">
            <el-switch v-model="form.mock_mode" aria-label="Mock 模式" />
            <span class="field-inline-help">{{ form.mock_mode ? '本地演示，不调用外部模型' : '调用你配置的真实模型服务' }}</span>
          </el-form-item>
          <el-form-item label="API 根地址" prop="base_url">
            <el-input v-model="form.base_url" placeholder="填写兼容对话接口的根地址" maxlength="1000" :disabled="form.mock_mode" />
            <p class="field-help">填写对话服务根地址；后端追加 /chat/completions。</p>
          </el-form-item>
          <el-form-item label="模型名称" prop="model"><el-input v-model="form.model" placeholder="填写服务支持的完整模型标识" maxlength="200" :disabled="form.mock_mode" /></el-form-item>
          <el-form-item label="Judge API Key">
            <el-input v-model="form.api_key" type="password" autocomplete="new-password" :placeholder="saved?.has_api_key ? '已保存，留空保持原 Key；填写新值将替换' : '输入 API Key；免鉴权的本地服务可留空'" maxlength="4096" :disabled="form.clear_api_key || form.mock_mode" aria-label="Judge API Key" />
            <div class="key-status"><span>{{ saved?.has_api_key ? '已配置 Key，接口不回显原文' : '未配置 Key' }}</span><el-checkbox v-if="saved?.has_api_key" v-model="form.clear_api_key">清除已保存的 API Key</el-checkbox></div>
            <p class="field-help">Key 保存在本机 SQLite，页面重新加载后不会读回原文。更换根地址时需要替换或清除原 Key。</p>
          </el-form-item>
          <el-form-item label="调用超时（秒）" prop="timeout_seconds"><el-input-number v-model="form.timeout_seconds" :min="5" :max="60" :step="5" :value-on-clear="20" /></el-form-item>
          </div>
          <div class="settings-actions">
            <el-button type="primary" :loading="saving" :disabled="testing || (!dirty && !!saved?.configured)" @click="saveSettings">保存配置</el-button>
            <el-button :loading="testing" :disabled="saving || dirty || saved?.effective_source === 'unconfigured'" @click="testConnection">{{ saved?.effective_mock_mode ? '验证 Mock 流程' : '测试真实连接' }}</el-button>
            <span v-if="dirty" class="unsaved-label">有未保存修改</span>
          </div>
        </el-form>
        <p class="field-help test-help">连接测试发送固定短消息，不含项目正文；只验证对话连接，可能计费，不代表评分质量。更改来源后请先保存。Mock 只验证流程。</p>
        <el-alert v-if="testError" :title="testError" type="error" :closable="false" show-icon class="test-result" />
        <div v-if="result" class="test-result">
          <el-alert :title="result.message" :type="result.mode === 'mock' ? 'warning' : 'success'" :closable="false" show-icon />
          <div class="connection-details"><span>模式：{{ result.mode === 'mock' ? 'Mock（未联网）' : '真实调用' }}</span><span>耗时：{{ result.elapsed_ms }} ms</span><p>{{ result.reply }}</p></div>
        </div>
      </div>
    </section>
  </main>
</template>
