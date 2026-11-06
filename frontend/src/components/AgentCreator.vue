<script setup lang="ts">
import { ref, computed } from 'vue'
import { useExpertStore, useUiStore, friendlyError } from '../stores'
import type { RoleType, WorkflowPosition, ContextScope, TriggerMode, ExpertCreatePayload } from '../api/types'
import { MAX_SYSTEM_PROMPT_LENGTH, MAX_TOKENS_LIMIT } from '../api/types'
import { useRoute } from 'vue-router'
import BaseSelect from './BaseSelect.vue'

const route = useRoute()
const store = useExpertStore()
const ui = useUiStore()

const projectId = computed(() => route.params.id as string)

const name = ref('')
const description = ref('')
const systemPrompt = ref('')
const temperature = ref(0.7)
const roleType = ref<RoleType>('custom')
const maxTokens = ref(4096)
const workflowPosition = ref<WorkflowPosition>('standalone')
const contextScope = ref<ContextScope>({
  include_world: true,
  include_characters: true,
  include_outline: true,
  include_previous_chapters: 3,
})
const trigger = ref<TriggerMode>('manual')
const color = ref('#6366f1')
const creating = ref(false)
const createError = ref('')

const systemPromptError = computed(() => {
  if (systemPrompt.value.length > MAX_SYSTEM_PROMPT_LENGTH) {
    return `System Prompt 不能超过 ${MAX_SYSTEM_PROMPT_LENGTH} 字（当前 ${systemPrompt.value.length}）`
  }
  return ''
})

const maxTokensError = computed(() => {
  if (maxTokens.value > MAX_TOKENS_LIMIT) return `max_tokens 不能超过 ${MAX_TOKENS_LIMIT}`
  if (maxTokens.value < 1) return 'max_tokens 至少为 1'
  return ''
})

const canSubmit = computed(() =>
  name.value.trim() &&
  systemPrompt.value.trim() &&
  !systemPromptError.value &&
  !maxTokensError.value &&
  !creating.value,
)

const roleTypeOptions: { value: RoleType; label: string }[] = [
  { value: 'writer', label: '写手' },
  { value: 'critic', label: '评论家' },
  { value: 'editor', label: '编辑' },
  { value: 'researcher', label: '研究者' },
  { value: 'custom', label: '自定义' },
]

const workflowPositionOptions: { value: WorkflowPosition; label: string }[] = [
  { value: 'pre_writer', label: '写手前' },
  { value: 'post_writer', label: '写手后' },
  { value: 'replace_writer', label: '替代写手' },
  { value: 'pre_critic', label: '评论前' },
  { value: 'replace_critic', label: '替代评论' },
  { value: 'post_critic', label: '评论后' },
  { value: 'standalone', label: '独立运行' },
]

const triggerOptions: { value: TriggerMode; label: string }[] = [
  { value: 'manual', label: '手动触发' },
  { value: 'auto_on_draft', label: '草稿完成后' },
  { value: 'auto_on_save', label: '保存时' },
  { value: 'auto_on_chapter_complete', label: '章节完成时' },
]

async function createExpert() {
  if (!canSubmit.value) return

  const payload: ExpertCreatePayload = {
    name: name.value.trim(),
    role_type: roleType.value,
    description: description.value.trim(),
    system_prompt: systemPrompt.value.trim(),
    temperature: temperature.value,
    max_tokens: maxTokens.value,
    workflow_position: workflowPosition.value,
    context_scope: { ...contextScope.value },
    trigger: trigger.value,
    color: color.value,
  }

  creating.value = true
  createError.value = ''

  try {
    await store.addExpert(projectId.value, payload)
    // Refresh expert list from backend to ensure consistency
    await store.loadExperts(projectId.value)
    ui.showToast('Agent 创建成功', 'success')
    resetForm()
  } catch (e: unknown) {
    const msg = friendlyError(e, '创建 Agent 失败')
    createError.value = msg
    ui.showToast(msg, 'error')
  } finally {
    creating.value = false
  }
}

function resetForm() {
  name.value = ''
  description.value = ''
  systemPrompt.value = ''
  temperature.value = 0.7
  roleType.value = 'custom'
  maxTokens.value = 4096
  workflowPosition.value = 'standalone'
  contextScope.value = { include_world: true, include_characters: true, include_outline: true, include_previous_chapters: 3 }
  trigger.value = 'manual'
  color.value = '#6366f1'
  createError.value = ''
}
</script>

<template>
  <div class="agent-creator">
    <h3>创建自定义 Agent</h3>
    <form @submit.prevent="createExpert" class="creator-form">
      <div class="form-row">
        <div class="form-group flex-1">
          <label>名称 *</label>
          <input v-model="name" placeholder="例：伏笔追踪者" required />
        </div>
        <div class="form-group" style="width: 64px;">
          <label>颜色</label>
          <input v-model="color" type="color" />
        </div>
      </div>

      <div class="form-group">
        <label>描述</label>
        <input v-model="description" placeholder="简述这个 Agent 的用途" />
      </div>

      <div class="form-group">
        <label>角色类型</label>
        <div class="radio-group">
          <label v-for="opt in roleTypeOptions" :key="opt.value" class="radio-label">
            <input type="radio" v-model="roleType" :value="opt.value" />
            {{ opt.label }}
          </label>
        </div>
      </div>

      <div class="form-group">
        <label>角色描述（System Prompt）* <span v-if="systemPromptError" class="field-error">{{ systemPromptError }}</span></label>
        <textarea
          v-model="systemPrompt"
          placeholder="描述这个 Agent 的专长、行为方式和输出要求..."
          rows="3"
          required
        />
      </div>

      <div class="form-row">
        <div class="form-group flex-1">
          <label>创造性温度: {{ temperature.toFixed(1) }}</label>
          <input v-model.number="temperature" type="range" min="0.1" max="1.0" step="0.1" />
          <div class="range-labels"><span>严谨</span><span>创意</span></div>
        </div>
        <div class="form-group" style="width: 120px;">
          <label>最大 Token <span v-if="maxTokensError" class="field-error">{{ maxTokensError }}</span></label>
          <input v-model.number="maxTokens" type="number" min="1" :max="MAX_TOKENS_LIMIT" />
        </div>
      </div>

      <div class="form-row">
        <div class="form-group flex-1">
          <label>工作流位置</label>
          <BaseSelect v-model="workflowPosition" :options="workflowPositionOptions" />
        </div>
        <div class="form-group flex-1">
          <label>触发方式</label>
          <div class="radio-group compact">
            <label v-for="opt in triggerOptions" :key="opt.value" class="radio-label">
              <input type="radio" v-model="trigger" :value="opt.value" />
              {{ opt.label }}
            </label>
          </div>
        </div>
      </div>

      <div class="form-group">
        <label>上下文范围</label>
        <div class="scope-row">
          <label class="checkbox-label"><input type="checkbox" v-model="contextScope.include_world" /> 世界观</label>
          <label class="checkbox-label"><input type="checkbox" v-model="contextScope.include_characters" /> 角色</label>
          <label class="checkbox-label"><input type="checkbox" v-model="contextScope.include_outline" /> 大纲</label>
          <label class="checkbox-label">前文 <input v-model.number="contextScope.include_previous_chapters" type="number" min="0" max="50" class="inline-num" /> 章</label>
        </div>
      </div>

      <button type="submit" class="btn-create" :disabled="!canSubmit">
        {{ creating ? '创建中...' : '创建 Agent' }}
      </button>
      <div v-if="createError" class="create-error">{{ createError }}</div>
    </form>
  </div>
</template>

<style scoped>
.agent-creator h3 {
  font-size: var(--text-base);
  font-weight: 600;
  margin-bottom: var(--sp-4);
}
.creator-form {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}
.form-row {
  display: flex;
  gap: var(--sp-3);
  align-items: flex-start;
}
.flex-1 { flex: 1; }
.form-group {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.form-group label {
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--text-secondary);
}
.radio-group {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3);
}
.radio-group.compact {
  gap: var(--sp-2);
}
.radio-label {
  font-size: var(--text-xs);
  display: flex;
  align-items: center;
  gap: 3px;
  cursor: pointer;
  color: var(--text-secondary);
}
.scope-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3);
  align-items: center;
}
.checkbox-label {
  font-size: var(--text-xs);
  display: flex;
  align-items: center;
  gap: 3px;
  cursor: pointer;
  color: var(--text-secondary);
}
.inline-num {
  width: 48px;
  padding: 2px 4px;
  font-size: var(--text-xs);
  text-align: center;
}
.range-labels {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--text-tertiary);
}
.field-error {
  color: var(--status-error);
  font-weight: 400;
}
.btn-create {
  padding: var(--sp-2) var(--sp-5);
  background: var(--accent);
  color: var(--text-inverse);
  border: none;
  border-radius: var(--radius);
  font-size: var(--text-sm);
  font-weight: 500;
  transition: background var(--transition);
}
.btn-create:hover:not(:disabled) { background: var(--accent-hover); }
.btn-create:disabled { background: var(--status-pending); cursor: not-allowed; }
.create-error {
  font-size: var(--text-xs);
  color: var(--status-error);
  margin-top: var(--sp-1);
}
</style>
