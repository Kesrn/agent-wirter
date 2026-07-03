<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../api/client'
import type { ApiWritingMemoryStaging } from '../api/types'

const props = defineProps<{ projectId: string }>()

const stagingItems = ref<ApiWritingMemoryStaging[]>([])
const loading = ref(false)

async function loadStaging() {
  loading.value = true
  try {
    stagingItems.value = await api.listMemoryStaging(props.projectId)
  } catch {
    stagingItems.value = []
  } finally {
    loading.value = false
  }
}

async function confirmItem(id: string) {
  try {
    await api.confirmMemoryStaging(props.projectId, id)
    await loadStaging()
  } catch (e) {
    // silent
  }
}

async function rejectItem(id: string) {
  try {
    await api.rejectMemoryStaging(props.projectId, id)
    await loadStaging()
  } catch (e) {
    // silent
  }
}

const typeLabels: Record<string, string> = {
  CHARACTER: '角色',
  WORLD_RULE: '设定',
  PLOT_FACT: '剧情',
  EVENT: '事件',
  FORESHADOWING: '伏笔',
}

onMounted(loadStaging)
</script>

<template>
  <div class="memory-staging-panel">
    <div class="panel-header">
      <span class="section-label">写作记忆</span>
      <button class="refresh-btn" type="button" @click="loadStaging">刷新</button>
    </div>
    <div v-if="loading" class="loading-text">加载中...</div>
    <div v-else-if="stagingItems.length === 0" class="empty-text">暂无记忆</div>
    <div v-else class="staging-list">
      <div
        v-for="item in stagingItems"
        :key="item.id"
        class="staging-item"
      >
        <div class="item-main">
          <span class="type-tag">{{ typeLabels[item.memory_type] || item.memory_type }}</span>
          <span class="item-title">{{ item.title }}</span>
          <span v-if="item.evidence" class="item-evidence">{{ item.evidence }}</span>
        </div>
        <div class="item-actions">
          <span v-if="item.status === 'CONFIRMED'" class="badge confirmed">已确认</span>
          <span v-else-if="item.status === 'REJECTED'" class="badge rejected">已拒绝</span>
          <template v-else>
            <button class="action-btn confirm" type="button" @click="confirmItem(item.id)">确认</button>
            <button class="action-btn reject" type="button" @click="rejectItem(item.id)">拒绝</button>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.memory-staging-panel {
  margin-top: 12px;
  padding: 8px 0;
}
.panel-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.section-label {
  font-size: 13px;
  color: var(--text-secondary, #888);
}
.refresh-btn {
  font-size: 12px;
  padding: 2px 8px;
  border: 1px solid var(--border-color, #ddd);
  border-radius: 4px;
  background: transparent;
  cursor: pointer;
}
.staging-item {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  padding: 6px 0;
  border-bottom: 1px solid var(--border-color-light, #f0f0f0);
}
.item-main {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.type-tag {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--bg-tag, #e8e8e8);
  color: var(--text-secondary, #666);
}
.item-title {
  font-size: 13px;
  font-weight: 500;
}
.item-evidence {
  font-size: 12px;
  color: var(--text-tertiary, #aaa);
}
.item-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}
.action-btn {
  font-size: 12px;
  padding: 2px 8px;
  border: 1px solid var(--border-color, #ddd);
  border-radius: 4px;
  background: transparent;
  cursor: pointer;
}
.action-btn.confirm {
  color: var(--color-success, #52c41a);
  border-color: var(--color-success, #52c41a);
}
.action-btn.reject {
  color: var(--color-danger, #ff4d4f);
  border-color: var(--color-danger, #ff4d4f);
}
.badge {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 3px;
}
.badge.confirmed {
  background: #f6ffed;
  color: #52c41a;
}
.badge.rejected {
  background: #f5f5f5;
  color: #999;
}
.loading-text, .empty-text {
  font-size: 13px;
  color: var(--text-tertiary, #aaa);
  padding: 8px 0;
}
</style>
