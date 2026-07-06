<script setup lang="ts">
import { ref, watch } from 'vue'
import { api } from '../api/client'
import type { ApiCharacterArcResponse, ApiCharacterArcItem } from '../api/types'
import { friendlyError } from '../stores'

const props = defineProps<{
  projectId: string
  characterId: string
  characterName: string
}>()

const loading = ref(false)
const arc = ref<ApiCharacterArcResponse | null>(null)
const error = ref('')

async function loadArc() {
  loading.value = true
  error.value = ''
  try {
    arc.value = await api.getCharacterArc(props.projectId, props.characterId)
  } catch (e: unknown) {
    error.value = friendlyError(e, '加载角色弧线失败')
  } finally {
    loading.value = false
  }
}

watch(() => props.characterId, () => {
  if (props.characterId) loadArc()
}, { immediate: true })

function sourceLabel(item: ApiCharacterArcItem): string {
  const map: Record<string, string> = {
    CharacterEvent: '章节事件',
    WritingMemory: 'AI提取',
  }
  return map[item.source_type] ?? item.source_type
}
</script>

<template>
  <div class="character-arc-panel" v-if="arc || loading">
    <div v-if="loading" class="arc-loading">加载中...</div>
    <div v-else-if="error" class="arc-error">{{ error }}</div>
    <template v-else-if="arc">
      <div class="arc-header">
        <h4>{{ characterName }} — 角色弧线</h4>
        <span class="arc-range" v-if="arc.chapter_range">{{ arc.chapter_range }}</span>
      </div>
      <div v-if="!arc.items.length" class="arc-empty">暂无弧线数据</div>
      <div v-else class="arc-timeline">
        <div v-for="item in arc.items" :key="`${item.chapter_sequence_number}-${item.source_type}`" class="arc-item">
          <div class="arc-item-header">
            <span class="arc-chapter" v-if="item.chapter_sequence_number">第{{ item.chapter_sequence_number }}章</span>
            <span class="arc-source">{{ sourceLabel(item) }}</span>
            <span v-if="item.confidence === 'ai_extracted'" class="arc-confidence">AI</span>
          </div>
          <div class="arc-item-title">{{ item.title }}</div>
          <div v-if="item.summary && item.summary !== item.title" class="arc-item-summary">{{ item.summary }}</div>
          <div v-if="item.state_change" class="arc-item-change">变化：{{ item.state_change }}</div>
          <div v-if="item.emotion" class="arc-item-emotion">情绪：{{ item.emotion }}</div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.character-arc-panel {
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: var(--sp-4);
  margin-top: var(--sp-3);
}
.arc-header {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
}
.arc-header h4 { margin: 0; font-size: 14px; }
.arc-range { font-size: 12px; color: var(--color-text-muted); }
.arc-empty, .arc-loading, .arc-error { font-size: 13px; color: var(--color-text-muted); padding: var(--sp-2) 0; }
.arc-error { color: var(--color-danger); }
.arc-timeline { display: flex; flex-direction: column; gap: var(--sp-2); }
.arc-item { border-left: 2px solid var(--color-primary); padding-left: var(--sp-3); }
.arc-item-header { display: flex; align-items: center; gap: var(--sp-2); margin-bottom: var(--sp-1); }
.arc-chapter { font-size: 12px; font-weight: 600; color: var(--color-primary); }
.arc-source { font-size: 11px; color: var(--color-text-muted); }
.arc-confidence { font-size: 10px; background: var(--color-surface); border: 1px solid var(--color-border); padding: 0 4px; border-radius: 3px; }
.arc-item-title { font-size: 13px; font-weight: 500; }
.arc-item-summary { font-size: 12px; color: var(--color-text-secondary); margin-top: 2px; }
.arc-item-change { font-size: 12px; color: #3b82f6; margin-top: 2px; }
.arc-item-emotion { font-size: 12px; color: var(--color-text-muted); margin-top: 2px; }
</style>
