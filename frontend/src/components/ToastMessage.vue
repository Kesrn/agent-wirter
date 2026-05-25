<script setup lang="ts">
import { useUiStore } from '../stores'

const ui = useUiStore()
</script>

<template>
  <div class="toast-container">
    <div
      v-for="toast in ui.toasts"
      :key="toast.id"
      class="toast-item"
      :class="toast.type"
    >
      {{ toast.message }}
    </div>
  </div>
</template>

<style scoped>
.toast-container {
  position: fixed;
  top: var(--sp-4);
  left: 50%;
  transform: translateX(-50%);
  z-index: 2000;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  width: min(420px, calc(100vw - 32px));
  pointer-events: none;
}
.toast-item {
  padding: var(--sp-3) var(--sp-4);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  line-height: 1.4;
  box-shadow: var(--shadow);
  animation: toast-in 150ms ease;
  text-align: center;
  pointer-events: auto;
}
.toast-item.error {
  background: var(--sev-critical-bg);
  color: var(--sev-critical);
  border: 1px solid var(--sev-critical);
}
.toast-item.success {
  background: var(--status-final-bg);
  color: var(--status-final);
  border: 1px solid var(--status-final);
}
.toast-item.info {
  background: var(--sev-info-bg);
  color: var(--sev-info);
  border: 1px solid var(--sev-info);
}
@keyframes toast-in {
  from { opacity: 0; transform: translateY(-8px); }
  to { opacity: 1; transform: translateY(0); }
}
</style>
