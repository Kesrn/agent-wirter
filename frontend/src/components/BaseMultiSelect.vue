<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

type MultiSelectOption = {
  value: string
  label: string
}

const props = withDefaults(defineProps<{
  modelValue: string[]
  options: readonly MultiSelectOption[]
  placeholder?: string
  disabled?: boolean
}>(), {
  placeholder: '请选择',
  disabled: false,
})

const emit = defineEmits<{
  'update:modelValue': [value: string[]]
}>()

const rootRef = ref<HTMLElement | null>(null)
const open = ref(false)

const selectedOptions = computed(() => {
  const labels = new Map(props.options.map(option => [option.value, option.label]))
  return props.modelValue.map(value => ({ value, label: labels.get(value) ?? value }))
})

function toggleOpen() {
  if (props.disabled) return
  open.value = !open.value
}

function close() {
  open.value = false
}

function isSelected(value: string): boolean {
  return props.modelValue.includes(value)
}

function toggleOption(value: string) {
  if (props.disabled) return
  if (isSelected(value)) {
    emit('update:modelValue', props.modelValue.filter(item => item !== value))
    return
  }
  emit('update:modelValue', [...props.modelValue, value])
}

function removeOption(value: string) {
  emit('update:modelValue', props.modelValue.filter(item => item !== value))
}

function clearAll() {
  emit('update:modelValue', [])
}

function handleKeydown(event: KeyboardEvent) {
  if (props.disabled) return
  if (event.key === 'Escape') close()
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    toggleOpen()
  }
}

function handleDocumentPointerDown(event: PointerEvent) {
  if (!rootRef.value?.contains(event.target as Node)) close()
}

onMounted(() => document.addEventListener('pointerdown', handleDocumentPointerDown))
onBeforeUnmount(() => document.removeEventListener('pointerdown', handleDocumentPointerDown))
</script>

<template>
  <div ref="rootRef" class="base-multi-select" :class="{ 'is-open': open, 'is-disabled': disabled }">
    <button
      type="button"
      class="multi-select-trigger"
      :disabled="disabled"
      :aria-expanded="open"
      aria-haspopup="listbox"
      @click="toggleOpen"
      @keydown="handleKeydown"
    >
      <span v-if="!selectedOptions.length" class="multi-placeholder">{{ placeholder }}</span>
      <span v-else class="selected-tags">
        <span v-for="option in selectedOptions" :key="option.value" class="selected-tag">
          {{ option.label }}
          <span
            class="tag-remove"
            role="button"
            tabindex="-1"
            aria-label="移除"
            @click.stop="removeOption(option.value)"
          >×</span>
        </span>
      </span>
      <span class="multi-chevron" aria-hidden="true"></span>
    </button>

    <div v-if="open" class="multi-select-panel" role="listbox" aria-multiselectable="true">
      <button
        v-for="option in options"
        :key="option.value"
        type="button"
        class="multi-option"
        :class="{ 'is-selected': isSelected(option.value) }"
        role="option"
        :aria-selected="isSelected(option.value)"
        @click="toggleOption(option.value)"
      >
        <span class="option-check">{{ isSelected(option.value) ? '✓' : '' }}</span>
        <span>{{ option.label }}</span>
      </button>
      <div v-if="selectedOptions.length" class="multi-panel-footer">
        <button type="button" class="multi-clear" @click="clearAll">清空选择</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.base-multi-select {
  position: relative;
  width: 100%;
  min-width: 0;
}

.multi-select-trigger {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: 42px;
  padding: 6px 34px 6px var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-input);
  color: var(--text);
  text-align: left;
  box-shadow: 0 1px 0 rgba(17, 24, 39, 0.02);
  transition: border-color var(--transition), box-shadow var(--transition), background var(--transition);
}

.multi-select-trigger:hover:not(:disabled),
.is-open .multi-select-trigger {
  border-color: var(--border-focus);
  background: color-mix(in srgb, var(--bg-input) 82%, var(--accent));
}

.multi-select-trigger:focus-visible {
  outline: none;
  border-color: var(--border-focus);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 16%, transparent);
}

.multi-placeholder {
  color: var(--text-tertiary);
  font-size: var(--text-sm);
  font-weight: 500;
}

.selected-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
}

.selected-tag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 180px;
  padding: 3px 8px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--accent) 18%, transparent);
  color: var(--accent);
  font-size: var(--text-xs);
  font-weight: 700;
  line-height: 1.4;
}

.tag-remove {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 14px;
  border-radius: 999px;
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1;
}

.tag-remove:hover {
  background: color-mix(in srgb, var(--accent) 18%, transparent);
  color: var(--accent);
}

.multi-chevron {
  position: absolute;
  right: 13px;
  width: 8px;
  height: 8px;
  border-right: 2px solid currentColor;
  border-bottom: 2px solid currentColor;
  opacity: 0.58;
  transform: translateY(-2px) rotate(45deg);
  transition: transform var(--transition), opacity var(--transition);
}

.is-open .multi-chevron {
  opacity: 0.84;
  transform: translateY(2px) rotate(225deg);
}

.multi-select-panel {
  position: absolute;
  z-index: 85;
  top: calc(100% + 6px);
  left: 0;
  right: 0;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(112px, 1fr));
  gap: 4px;
  max-height: 260px;
  overflow: auto;
  padding: 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  box-shadow: var(--shadow-lg);
}

.multi-option {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  min-height: 32px;
  padding: var(--sp-2);
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--text-sm);
  font-weight: 600;
  text-align: left;
  transition: background var(--transition), border-color var(--transition), color var(--transition);
}

.multi-option:hover,
.multi-option.is-selected {
  border-color: color-mix(in srgb, var(--accent) 34%, transparent);
  background: color-mix(in srgb, var(--accent) 14%, transparent);
  color: var(--accent);
}

.option-check {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  width: 16px;
  height: 16px;
  border: 1px solid var(--border);
  border-radius: 5px;
  color: var(--accent);
  font-size: 11px;
  font-weight: 800;
}

.is-selected .option-check {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 18%, transparent);
}

.multi-panel-footer {
  grid-column: 1 / -1;
  display: flex;
  justify-content: flex-end;
  padding-top: 4px;
  border-top: 1px solid var(--border);
}

.multi-clear {
  border: 0;
  background: transparent;
  color: var(--text-tertiary);
  font-size: var(--text-xs);
  font-weight: 700;
}

.multi-clear:hover {
  color: var(--accent);
}

.is-disabled {
  opacity: 0.52;
}

.is-disabled .multi-select-trigger {
  cursor: not-allowed;
}
</style>
