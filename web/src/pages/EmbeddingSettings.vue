<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { embeddingSettingsApi } from '../api'
import type { EmbeddingTestResult, EmbeddingSettings, EmbeddingSettingsInput } from '../api'
import { errorMessage } from '../utils'

const loading = ref(false)
const loaded = ref(false)
const saving = ref(false)
const testing = ref(false)
const loadError = ref('')
const saveError = ref('')
const testError = ref('')
const result = ref<EmbeddingTestResult>()
const saved = ref<EmbeddingSettings>()
const formRef = ref<FormInstance>()
const form = reactive<EmbeddingSettingsInput>({
  base_url: '', model: '', api_key: '', clear_api_key: false, mock_mode: false, timeout_seconds: 20, expected_dimensions: null,
})
const original = ref('')
const dirty = computed(() => loaded.value && JSON.stringify(form) !== original.value)
const busy = computed(() => loading.value || saving.value || testing.value)
const rules: FormRules = {
  base_url: [{ validator: (_rule, value, callback) => {
    if (!form.mock_mode && !String(value).trim()) return callback(new Error('真实模式需要填写 API 根地址'))
    if (value) {
      try {
        const url = new URL(value)
        if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash
          || /\s/.test(value) || ['/chat/completions', '/embeddings'].some(path => url.pathname.replace(/\/+$/, '').endsWith(path))) throw new Error()
      } catch { return callback(new Error('请填写 HTTP/HTTPS API 根地址，不含凭据、参数或 /embeddings')) }
    }
    callback()
  }, trigger: 'blur' }],
  model: [{ validator: (_rule, value, callback) => {
    if (!form.mock_mode && !String(value).trim()) return callback(new Error('真实模式需要填写模型名称'))
    callback()
  }, trigger: 'blur' }],
}

function applySettings(data: EmbeddingSettings) {
  saved.value = data
  Object.assign(form, {
    base_url: data.base_url, model: data.model, api_key: '', clear_api_key: false,
    mock_mode: data.mock_mode, timeout_seconds: data.timeout_seconds, expected_dimensions: data.expected_dimensions,
  })
  original.value = JSON.stringify(form)
  loaded.value = true
}

async function confirmDiscard() {
  if (saving.value || testing.value) return false
  if (!dirty.value) return true
  try {
    await ElMessageBox.confirm('Embedding 配置尚未保存，确定放弃修改吗？', '放弃修改', {
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
    applySettings(await embeddingSettingsApi.get())
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
    applySettings(await embeddingSettingsApi.save({ ...form }))
    ElMessage.success('Embedding 配置已保存')
  } catch (cause) {
    saveError.value = errorMessage(cause)
  } finally {
    saving.value = false
  }
}

async function testConnection() {
  if (busy.value || dirty.value || !saved.value?.configured) return
  testing.value = true
  result.value = undefined
  testError.value = ''
  try {
    result.value = await embeddingSettingsApi.test(form.timeout_seconds)
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
      <div><div class="eyebrow">EMBEDDING SETTINGS</div><h1>连接你的 Embedding 模型</h1><p>用于将文本转换为向量。保存并验证连接后，可在项目知识库中构建向量索引和进行语义检索。</p></div>
      <el-button :loading="loading" :disabled="saving || testing" @click="loadSettings">重新加载</el-button>
    </div>
    <p class="field-help feature-notice">生成模型与 Embedding 模型使用各自的根地址、模型和 Key，不会自动复用。<router-link to="/settings">返回生成模型设置 →</router-link></p>
    <el-alert v-if="loadError" :title="loadError" type="error" :closable="false" show-icon />
    <section v-else class="project-panel settings-panel" aria-labelledby="settings-title">
      <div class="panel-toolbar">
        <div><h2 id="settings-title">Embedding 向量模型</h2><p>独立于用例生成模型 · 使用兼容 /embeddings 的接口</p></div>
        <el-tag :type="saved?.configured ? (saved.mock_mode ? 'warning' : 'primary') : 'info'">{{ saved?.configured ? (saved.mock_mode ? '已保存 · Mock 模式' : '已保存 · 真实模式') : '尚未配置' }}</el-tag>
      </div>
      <div v-if="loading" class="loading-state"><el-skeleton :rows="6" animated /></div>
      <div v-else-if="loaded" class="settings-body">
        <el-alert v-if="saveError" :title="saveError" type="error" :closable="false" show-icon class="form-error" />
        <el-form ref="formRef" :model="form" :rules="rules" label-position="top" :disabled="busy" @submit.prevent="saveSettings">
          <el-form-item label="Mock 模式">
            <el-switch v-model="form.mock_mode" aria-label="Mock 模式" />
            <span class="field-inline-help">{{ form.mock_mode ? '本地演示，不调用外部模型' : '调用你配置的真实模型服务' }}</span>
          </el-form-item>
          <el-form-item label="API 根地址" prop="base_url">
            <el-input v-model="form.base_url" placeholder="例如：http://127.0.0.1:11434/v1" maxlength="1000" :disabled="form.mock_mode" />
            <p class="field-help">填写支持 Embedding 的服务根地址；后端追加 /embeddings，不追加 /chat/completions。</p>
          </el-form-item>
          <el-form-item label="模型名称" prop="model"><el-input v-model="form.model" placeholder="填写服务支持的完整模型标识" maxlength="200" :disabled="form.mock_mode" /></el-form-item>
          <el-form-item label="Embedding API Key">
            <el-input v-model="form.api_key" type="password" autocomplete="new-password" :placeholder="saved?.has_api_key ? '已保存，留空保持原 Key；填写新值将替换' : '输入 API Key；免鉴权的本地服务可留空'" maxlength="4096" :disabled="form.clear_api_key || form.mock_mode" aria-label="Embedding API Key" />
            <div class="key-status"><span>{{ saved?.has_api_key ? '已配置 Key，接口不回显原文' : '未配置 Key' }}</span><el-checkbox v-if="saved?.has_api_key" v-model="form.clear_api_key">清除已保存的 API Key</el-checkbox></div>
            <p class="field-help">Key 保存在本机 SQLite，页面重新加载后不会读回原文。更换根地址时需要替换或清除原 Key。</p>
          </el-form-item>
          <el-form-item label="预期向量维度（可留空）"><el-input-number v-model="form.expected_dimensions" :min="1" :max="32768" :precision="0" :value-on-clear="null" aria-label="预期向量维度" /><p class="field-help">留空接受模型实际维度；填写后仅校验返回值，不向服务请求截断或修改维度。</p></el-form-item>
          <el-form-item label="调用超时（秒）" prop="timeout_seconds"><el-input-number v-model="form.timeout_seconds" :min="5" :max="60" :step="5" :value-on-clear="20" /></el-form-item>
          <div class="settings-actions">
            <el-button type="primary" :loading="saving" :disabled="testing || (!dirty && !!saved?.configured)" @click="saveSettings">保存配置</el-button>
            <el-button :loading="testing" :disabled="saving || dirty || !saved?.configured" @click="testConnection">{{ saved?.mock_mode ? '验证 Mock 流程' : '测试真实连接' }}</el-button>
            <span v-if="dirty" class="unsaved-label">有未保存修改</span>
          </div>
        </el-form>
        <p class="field-help test-help">测试使用已保存配置，修改后请先保存。真实测试仅向本页配置的服务发送两段固定短文本，不含项目或知识正文，会发起一次 Embedding 请求。Mock 使用哈希示例向量，不具备语义能力。</p>
        <el-alert v-if="testError" :title="testError" type="error" :closable="false" show-icon class="test-result" />
        <div v-if="result" class="test-result">
          <el-alert :title="result.message" :type="result.mode === 'mock' ? 'warning' : 'success'" :closable="false" show-icon />
          <div class="connection-details"><span>模式：{{ result.mode === 'mock' ? 'Mock（未联网）' : '真实调用' }}</span><span v-if="result.mode === 'real'">耗时：{{ result.elapsed_ms }} ms</span><p>向量数量：{{ result.vector_count }} · 实际维度：{{ result.dimensions }}</p><p>已校验非零、有限数值和一致维度；连接成功不代表召回质量。</p></div>
        </div>
      </div>
    </section>
  </main>
</template>
