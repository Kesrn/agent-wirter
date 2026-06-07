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
      <span class="toast-message">{{ toast.message }}</span>
      <button
        class="toast-close"
        type="button"
        aria-label="关闭提示"
        @click="ui.dismissToast(toast.id)"
      >
        ×
      </button>
    </div>
  </div>
</template>

<style scoped>
.toast-container {
  position: fixed;
  top: calc(var(--desktop-status-bar-height, 0px) + max(var(--sp-4), env(safe-area-inset-top)));
  right: max(var(--sp-4), env(safe-area-inset-right));
  z-index: 5100;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  width: min(380px, calc(100vw - 32px));
  pointer-events: none;
}
.toast-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 24px;
  align-items: start;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  line-height: 1.45;
  box-shadow: var(--shadow);
  animation: toast-in 150ms ease;
  text-align: left;
  pointer-events: auto;
  max-height: min(34vh, 220px);
  overflow: auto;
  backdrop-filter: blur(12px);
}
.toast-message {
  min-width: 0;
  overflow-wrap: anywhere;
  word-break: break-word;
}
.toast-close {
  width: 24px;
  height: 24px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: currentColor;
  cursor: pointer;
  font-size: 18px;
  line-height: 1;
  opacity: 0.62;
}
.toast-close:hover {
  opacity: 1;
  background: color-mix(in srgb, currentColor 10%, transparent);
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
  from { opacity: 0; transform: translate(8px, -4px); }
  to { opacity: 1; transform: translateY(0); }
}

@media (max-width: 640px) {
  .toast-container {
    left: var(--sp-3);
    right: var(--sp-3);
    width: auto;
  }
}
</style>
