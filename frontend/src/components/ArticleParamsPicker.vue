<script setup lang="ts">
import { computed, ref } from 'vue'
import type { GenerateMode } from '../api/types'
import type { ArticleGenerateParams } from '../api/types'

const props = withDefaults(defineProps<{
  initialParams?: ArticleGenerateParams | null
  mode?: GenerateMode
}>(), {
  initialParams: null,
  mode: 'full_pipeline',
})

const emit = defineEmits<{
  confirm: [params: ArticleGenerateParams]
  cancel: []
}>()

const contentType = ref(props.initialParams?.content_type ?? '')
const platform = ref(props.initialParams?.platform ?? '')
const audience = ref(props.initialParams?.audience ?? '')
const contentGoal = ref(props.initialParams?.content_goal ?? '')
const tone = ref(props.initialParams?.tone ?? '')
const keyPoints = ref(props.initialParams?.key_points ?? '')
const targetWords = ref(props.initialParams?.target_words ?? 2000)

const CONTENT_TYPES = ['博客', '新闻稿', '产品文案', '营销邮件', '社媒帖子', '白皮书', '技术文档', '其他']
const PLATFORMS = ['微信公众号', '知乎', '掘金', 'CSDN', '小红书', '微博', '官网', '其他']
const TONES = ['专业', '轻松', '正式', '亲切', '权威', '活泼', '中性']

const title = computed(() => {
  if (props.mode === 'enhance') return '改写优化参数'
  if (props.mode === 'continue') return '内容方向参数'
  if (props.mode === 'summarize') return '受众反馈参数'
  return '生成参数'
})

const confirmLabel = computed(() => {
  if (props.mode === 'enhance') return '确认分析'
  if (props.mode === 'continue') return '确认获取方向'
  if (props.mode === 'summarize') return '确认分析'
  return '确认生成'
})

function handleConfirm() {
  emit('confirm', {
    content_type: contentType.value,
    platform: platform.value,
    audience: audience.value.trim(),
    content_goal: contentGoal.value.trim(),
    tone: tone.value,
    key_points: keyPoints.value.trim(),
    target_words: targetWords.value,
  })
}
</script>

<template>
  <div class="picker-overlay" @click.self="emit('cancel')">
    <div class="picker-card">
      <div class="picker-header">
        <h3>{{ title }}</h3>
        <p class="picker-subtitle">设定内容方向和风格，让 AI 更精准地理解本次任务</p>
      </div>

      <div class="picker-body">
        <div class="form-row">
          <label>内容类型</label>
          <div class="chip-group">
            <button
              v-for="ct in CONTENT_TYPES" :key="ct"
              class="chip" :class="{ active: contentType === ct }"
              @click="contentType = ct"
            >{{ ct }}</button>
          </div>
        </div>

        <div class="form-row">
          <label>发布平台</label>
          <div class="chip-group">
            <button
              v-for="p in PLATFORMS" :key="p"
              class="chip" :class="{ active: platform === p }"
              @click="platform = p"
            >{{ p }}</button>
          </div>
        </div>

        <div class="form-row">
          <label>目标受众</label>
          <input v-model="audience" type="text" placeholder="例如：互联网产品经理、创业者" class="form-input" />
        </div>

        <div class="form-row">
          <label>内容目标</label>
          <textarea v-model="contentGoal" placeholder="例如：介绍新功能、分享行业洞见" class="form-textarea" rows="2"></textarea>
        </div>

        <div class="form-row">
          <label>语气风格</label>
          <div class="chip-group">
            <button
              v-for="t in TONES" :key="t"
              class="chip" :class="{ active: tone === t }"
              @click="tone = t"
            >{{ t }}</button>
          </div>
        </div>

        <div class="form-row">
          <label>核心要点</label>
          <textarea v-model="keyPoints" placeholder="每行一个要点，AI 将围绕这些要点展开" class="form-textarea" rows="3"></textarea>
        </div>

        <div class="form-row">
          <label>目标字数</label>
          <input v-model.number="targetWords" type="number" min="500" max="10000" class="form-input form-input-sm" />
        </div>
      </div>

      <div class="picker-footer">
        <button class="btn-cancel" @click="emit('cancel')">取消</button>
        <button class="btn-confirm" @click="handleConfirm">{{ confirmLabel }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.picker-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.picker-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  width: 90vw;
  max-width: 560px;
  max-height: 85vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
}
.picker-header {
  padding: var(--sp-3) var(--sp-5);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.picker-header h3 {
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--text);
  margin: 0;
}
.picker-subtitle {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  margin: var(--sp-1) 0 0;
}
.picker-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--sp-3) var(--sp-5);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.form-row {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.form-row label {
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--text-secondary);
}
.form-input {
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  background: var(--bg);
  color: var(--text);
  transition: border-color var(--transition);
}
.form-input:focus { border-color: var(--border-focus); outline: none; }
.form-input-sm { width: 120px; }
.form-textarea {
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  background: var(--bg);
  color: var(--text);
  resize: vertical;
  transition: border-color var(--transition);
}
.form-textarea:focus { border-color: var(--border-focus); outline: none; }
.chip-group {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1);
}
.chip {
  padding: var(--sp-1) var(--sp-3);
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg);
  font-size: var(--text-xs);
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--transition), border-color var(--transition), color var(--transition);
}
.chip:hover { border-color: var(--border-focus); }
.chip.active {
  background: var(--accent-subtle);
  border-color: var(--accent);
  color: var(--accent);
}
.picker-footer {
  display: flex;
  gap: var(--sp-2);
  justify-content: flex-end;
  padding: var(--sp-3) var(--sp-5);
  border-top: 1px solid var(--border);
  flex-shrink: 0;
}
.btn-cancel {
  padding: var(--sp-2) var(--sp-4);
  background: none;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--transition);
}
.btn-cancel:hover { background: var(--bg-hover); }
.btn-confirm {
  padding: var(--sp-2) var(--sp-4);
  background: var(--accent);
  color: var(--text-inverse);
  border: none;
  border-radius: var(--radius);
  font-size: var(--text-sm);
  font-weight: 500;
  cursor: pointer;
  transition: background var(--transition);
}
.btn-confirm:hover { background: var(--accent-hover); }
</style>
