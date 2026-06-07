<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

type SelectOption = {
  value: string
  label: string
  disabled?: boolean
}

const props = withDefaults(defineProps<{
  modelValue: string
  options: readonly SelectOption[]
  placeholder?: string
  disabled?: boolean
  title?: string
  compact?: boolean
}>(), {
  placeholder: '请选择',
  disabled: false,
  title: '',
  compact: false,
})

const emit = defineEmits(['update:modelValue', 'change'])

const rootRef = ref<HTMLElement | null>(null)
const open = ref(false)
const activeIndex = ref(-1)

const selectedOption = computed(() => props.options.find(option => option.value === props.modelValue) ?? null)

watch(open, (isOpen) => {
  if (!isOpen) {
    activeIndex.value = -1
    return
  }

  const selectedIndex = props.options.findIndex(option => option.value === props.modelValue && !option.disabled)
  activeIndex.value = selectedIndex >= 0 ? selectedIndex : findEnabledIndex(0, 1)
})

function findEnabledIndex(start: number, direction: 1 | -1): number {
  const length = props.options.length
  if (!length) return -1

  for (let step = 0; step < length; step += 1) {
    const index = (start + step * direction + length * 2) % length
    if (!props.options[index]?.disabled) return index
  }
  return -1
}

function toggleOpen() {
  if (props.disabled) return
  open.value = !open.value
}

function close() {
  open.value = false
}

function selectOption(option: SelectOption) {
  if (option.disabled) return
  emit('update:modelValue', option.value)
  emit('change', option.value)
  close()
}

function moveActive(direction: 1 | -1) {
  const start = activeIndex.value >= 0
    ? activeIndex.value + direction
    : direction > 0 ? 0 : props.options.length - 1
  activeIndex.value = findEnabledIndex(start, direction)
}

function handleKeydown(event: KeyboardEvent) {
  if (props.disabled) return

  if (!open.value && ['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) {
    event.preventDefault()
    open.value = true
    return
  }

  if (!open.value) return

  if (event.key === 'Escape') {
    event.preventDefault()
    close()
  } else if (event.key === 'ArrowDown') {
    event.preventDefault()
    moveActive(1)
  } else if (event.key === 'ArrowUp') {
    event.preventDefault()
    moveActive(-1)
  } else if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    const option = props.options[activeIndex.value]
    if (option) selectOption(option)
  }
}

function handleDocumentPointerDown(event: PointerEvent) {
  if (!rootRef.value?.contains(event.target as Node)) {
    close()
  }
}

onMounted(() => document.addEventListener('pointerdown', handleDocumentPointerDown))
onBeforeUnmount(() => document.removeEventListener('pointerdown', handleDocumentPointerDown))
</script>

<template>
  <div
    ref="rootRef"
    class="base-select"
    :class="{ 'is-open': open, 'is-disabled': disabled, 'is-compact': compact }"
  >
    <button
      type="button"
      class="base-select-trigger"
      :disabled="disabled"
      :title="title"
      :aria-expanded="open"
      aria-haspopup="listbox"
      @click="toggleOpen"
      @keydown="handleKeydown"
    >
      <span class="base-select-label" :class="{ 'is-placeholder': !selectedOption }">
        {{ selectedOption?.label ?? placeholder }}
      </span>
      <span class="base-select-chevron" aria-hidden="true"></span>
    </button>

    <div v-if="open" class="base-select-panel" role="listbox">
      <button
        v-for="(option, index) in options"
        :key="option.value"
        type="button"
        class="base-select-option"
        :class="{ 'is-selected': option.value === modelValue, 'is-active': index === activeIndex }"
        :disabled="option.disabled"
        role="option"
        :aria-selected="option.value === modelValue"
        @mouseenter="activeIndex = index"
        @click="selectOption(option)"
      >
        {{ option.label }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.base-select {
  position: relative;
  width: 100%;
  min-width: 0;
}

.base-select-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  min-height: 38px;
  padding: var(--sp-2) 34px var(--sp-2) var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-input);
  color: var(--text);
  font-size: var(--text-sm);
  font-weight: 550;
  line-height: 1.2;
  text-align: left;
  box-shadow: 0 1px 0 rgba(17, 24, 39, 0.02);
  transition: border-color var(--transition), box-shadow var(--transition), background var(--transition), color var(--transition);
}

.base-select-trigger:hover:not(:disabled),
.is-open .base-select-trigger {
  border-color: var(--border-focus);
  background: color-mix(in srgb, var(--bg-input) 82%, var(--accent));
}

.base-select-trigger:focus-visible {
  outline: none;
  border-color: var(--border-focus);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 16%, transparent);
}

.base-select-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.base-select-label.is-placeholder {
  color: var(--text-tertiary);
}

.base-select-chevron {
  position: absolute;
  right: 12px;
  width: 8px;
  height: 8px;
  border-right: 2px solid currentColor;
  border-bottom: 2px solid currentColor;
  opacity: 0.58;
  transform: translateY(-2px) rotate(45deg);
  transition: transform var(--transition), opacity var(--transition);
}

.is-open .base-select-chevron {
  opacity: 0.84;
  transform: translateY(2px) rotate(225deg);
}

.base-select-panel {
  position: absolute;
  z-index: 80;
  top: calc(100% + 6px);
  left: 0;
  right: 0;
  max-height: 220px;
  overflow: auto;
  padding: 4px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  box-shadow: var(--shadow-lg);
}

.base-select-option {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: 32px;
  padding: var(--sp-2) var(--sp-3);
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--text-sm);
  font-weight: 550;
  text-align: left;
  transition: background var(--transition), color var(--transition);
}

.base-select-option:hover:not(:disabled),
.base-select-option.is-active {
  background: var(--bg-hover);
  color: var(--accent);
}

.base-select-option.is-selected {
  background: color-mix(in srgb, var(--accent) 18%, transparent);
  color: var(--accent);
}

.base-select-option:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.is-disabled {
  opacity: 0.52;
}

.is-disabled .base-select-trigger {
  cursor: not-allowed;
}

.is-compact .base-select-trigger {
  min-height: 30px;
  padding: 0 28px 0 var(--sp-2);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  font-weight: 650;
}

.is-compact .base-select-chevron {
  right: 10px;
  width: 7px;
  height: 7px;
}

.is-compact .base-select-option {
  min-height: 28px;
  padding: var(--sp-1) var(--sp-2);
  font-size: var(--text-xs);
}
</style>
