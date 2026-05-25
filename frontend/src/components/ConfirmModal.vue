<script setup lang="ts">
const props = defineProps<{
  message: string
  confirmText?: string
  danger?: boolean
}>()

const emit = defineEmits<{
  confirm: []
  cancel: []
}>()
</script>

<template>
  <div class="confirm-overlay" @click.self="emit('cancel')">
    <div class="confirm-modal">
      <p class="confirm-message">{{ message }}</p>
      <div class="confirm-actions">
        <button class="btn-cancel" @click="emit('cancel')">取消</button>
        <button class="btn-confirm" :class="{ danger }" @click="emit('confirm')">{{ confirmText || '确定' }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.confirm-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.35);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 3000;
}
.confirm-modal {
  background: var(--bg-panel);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  padding: var(--sp-6);
  max-width: 400px;
  width: 90%;
}
.confirm-message {
  font-size: var(--text-base);
  color: var(--text);
  line-height: 1.6;
  margin: 0 0 var(--sp-5);
}
.confirm-actions {
  display: flex;
  gap: var(--sp-2);
  justify-content: flex-end;
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
.btn-confirm.danger {
  background: var(--status-error);
}
.btn-confirm.danger:hover { background: #b91c1c; }
</style>
