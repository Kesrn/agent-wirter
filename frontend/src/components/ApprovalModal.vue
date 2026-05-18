<script setup lang="ts">
const emit = defineEmits<{
  decision: [value: 'accept' | 'accept_with_mods' | 'reject']
}>()

defineProps<{ content: string }>()
</script>

<template>
  <div class="approval-overlay">
    <div class="approval-modal">
      <h3>人工审核</h3>
      <div class="approval-content">
        <pre>{{ content }}</pre>
      </div>
      <div class="approval-actions">
        <button class="btn-accept" @click="emit('decision', 'accept')">采纳</button>
        <button class="btn-mods" @click="emit('decision', 'accept_with_mods')">修改后采纳</button>
        <button class="btn-reject" @click="emit('decision', 'reject')">拒绝</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.approval-overlay {
  position: fixed;
  top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.35);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
.approval-modal {
  background: var(--bg-panel);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  padding: var(--sp-6);
  max-width: 560px;
  width: 90%;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
}
.approval-modal h3 {
  font-size: var(--text-lg);
  font-weight: 600;
  margin: 0 0 var(--sp-4);
}
.approval-content {
  flex: 1;
  overflow-y: auto;
  margin-bottom: var(--sp-4);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--sp-3);
  background: var(--bg);
}
.approval-content pre {
  font-size: var(--text-xs);
  line-height: 1.6;
  white-space: pre-wrap;
  color: var(--text);
}
.approval-actions {
  display: flex;
  gap: var(--sp-2);
  justify-content: flex-end;
}
.btn-accept, .btn-mods, .btn-reject {
  padding: var(--sp-2) var(--sp-4);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  font-weight: 500;
  transition: background var(--transition);
}
.btn-accept { background: var(--status-final-bg); color: var(--status-final); border-color: var(--status-final); }
.btn-accept:hover { background: #d1fae5; }
.btn-mods { background: var(--status-reviewing-bg); color: var(--status-reviewing); border-color: var(--status-reviewing); }
.btn-mods:hover { background: #fef3c7; }
.btn-reject { background: var(--sev-critical-bg); color: var(--status-error); border-color: var(--status-error); }
.btn-reject:hover { background: #fee2e2; }
</style>
