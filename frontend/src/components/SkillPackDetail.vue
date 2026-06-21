<script setup lang="ts">
import { computed, ref } from 'vue'
import type { SkillPackPayload } from '../api/types'

const props = withDefaults(defineProps<{
  pack: SkillPackPayload
  compact?: boolean
}>(), {
  compact: false,
})

const open = ref(false)

const sourceRows = computed(() => props.pack.sources ?? [])
const warnings = computed(() => props.pack.warnings ?? [])
const tokenLabel = computed(() =>
  typeof props.pack.token_estimate === 'number' ? `${props.pack.token_estimate} tokens` : '未估算',
)
const plannerLabel = computed(() => {
  if (!props.pack.planner && !props.pack.planner_reason) return ''
  return [props.pack.planner, props.pack.planner_reason].filter(Boolean).join(': ')
})
const hasMeta = computed(() =>
  sourceRows.value.length > 0
  || warnings.value.length > 0
  || typeof props.pack.token_estimate === 'number'
  || Boolean(plannerLabel.value)
  || props.pack.truncated
  || props.pack.has_content === false,
)

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function sourceTitle(source: Record<string, unknown>, index: number): string {
  const raw = source.title ?? source.name ?? source.path ?? source.source
  return typeof raw === 'string' && raw.trim() ? raw : `Source ${index + 1}`
}

function sourceMeta(source: Record<string, unknown>): string {
  const parts = Object.entries(source)
    .filter(([key]) => !['title', 'name', 'path', 'source', 'content'].includes(key))
    .map(([key, value]) => `${key}: ${formatValue(value)}`)
  return parts.join(' · ')
}
</script>

<template>
  <div class="skill-pack" :class="{ compact }">
    <button class="skill-pack-trigger" type="button" @click="open = !open">
      <span class="skill-pack-name">{{ pack.skill }}</span>
      <span class="skill-pack-dir">{{ pack.skill_dir }}</span>
      <span v-if="warnings.length || pack.truncated" class="skill-pack-alert">!</span>
    </button>

    <div v-if="open" class="skill-pack-body">
      <div class="skill-pack-meta">
        <span>{{ pack.expert }}</span>
        <span>{{ tokenLabel }}</span>
        <span v-if="plannerLabel">{{ plannerLabel }}</span>
        <span v-if="pack.truncated">truncated</span>
        <span v-if="pack.has_content === false">empty</span>
      </div>

      <div v-if="warnings.length" class="skill-pack-warnings">
        <div v-for="warning in warnings" :key="warning" class="skill-pack-warning">
          {{ warning }}
        </div>
      </div>

      <div v-if="sourceRows.length" class="skill-pack-sources">
        <div v-for="(source, index) in sourceRows" :key="index" class="skill-pack-source">
          <div class="source-title">{{ sourceTitle(source, index) }}</div>
          <div v-if="sourceMeta(source)" class="source-meta">{{ sourceMeta(source) }}</div>
        </div>
      </div>

      <div v-else-if="!hasMeta" class="skill-pack-empty">
        暂无详情
      </div>
    </div>
  </div>
</template>

<style scoped>
.skill-pack {
  min-width: 0;
}

.skill-pack-trigger {
  display: inline-flex;
  align-items: center;
  max-width: 100%;
  gap: var(--sp-1);
  padding: 2px 7px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--text-secondary);
  font-size: 10px;
  cursor: pointer;
}

.skill-pack-trigger:hover {
  border-color: var(--border-focus);
  color: var(--accent);
  background: var(--bg-hover);
}

.skill-pack-name,
.skill-pack-dir {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.skill-pack-name {
  font-weight: 600;
}

.skill-pack-dir {
  color: var(--text-tertiary);
}

.skill-pack-alert {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #fef3c7;
  color: #92400e;
  font-weight: 700;
  flex-shrink: 0;
}

.skill-pack-body {
  margin-top: var(--sp-2);
  padding: var(--sp-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--text-secondary);
  font-size: var(--text-xs);
  line-height: 1.55;
}

.compact .skill-pack-body {
  margin-left: 0;
}

.skill-pack-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  color: var(--text-tertiary);
  margin-bottom: var(--sp-2);
}

.skill-pack-warnings {
  display: grid;
  gap: var(--sp-1);
  margin-bottom: var(--sp-2);
}

.skill-pack-warning {
  color: #92400e;
}

.skill-pack-sources {
  display: grid;
  gap: var(--sp-2);
}

.skill-pack-source {
  min-width: 0;
}

.source-title {
  color: var(--text);
  font-weight: 600;
  overflow-wrap: anywhere;
}

.source-meta {
  color: var(--text-tertiary);
  overflow-wrap: anywhere;
}

.skill-pack-empty {
  color: var(--text-tertiary);
}
</style>
