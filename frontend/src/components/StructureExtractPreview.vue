<script setup lang="ts">
import { computed, watch } from 'vue'
import { reactive } from 'vue'
import type { StructureExtractPayload } from '../api/types'

type ExtractGroupKey = 'outlines' | 'characters' | 'world_entries' | 'hidden_threads' | 'character_relations'
type ExtractRecord = Record<string, unknown>

interface EditableField {
  key: string
  label: string
  type: 'text' | 'number' | 'textarea' | 'csv'
}

interface EditableItem {
  id: string
  checked: boolean
  data: Record<string, string | number>
  original: ExtractRecord
}

interface EditableGroup {
  key: ExtractGroupKey
  label: string
  emptyText: string
  fields: EditableField[]
  items: EditableItem[]
  single?: boolean
}

const props = withDefaults(defineProps<{
  show: boolean
  payload: Record<string, unknown> | null
  title?: string
  confirmText?: string
}>(), {
  title: '结构提炼预览',
  confirmText: '确认写入',
})

const emit = defineEmits<{
  (e: 'confirm', payload: StructureExtractPayload): void
  (e: 'close'): void
}>()

const GROUP_DEFINITIONS: Array<Omit<EditableGroup, 'items'>> = [
  {
    key: 'outlines',
    label: '大纲',
    emptyText: '未提炼到大纲信息',
    fields: [
      { key: 'title', label: '标题', type: 'text' },
      { key: 'sequence_number', label: '序号', type: 'number' },
      { key: 'summary', label: '摘要', type: 'textarea' },
      { key: 'turning_point', label: '转折点', type: 'textarea' },
    ],
  },
  {
    key: 'characters',
    label: '角色',
    emptyText: '未提炼到角色',
    fields: [
      { key: 'name', label: '姓名', type: 'text' },
      { key: 'role_type', label: '角色类型', type: 'text' },
      { key: 'faction', label: '阵营', type: 'text' },
      { key: 'profile', label: '档案', type: 'textarea' },
    ],
  },
  {
    key: 'world_entries',
    label: '世界观',
    emptyText: '未提炼到世界观条目',
    fields: [
      { key: 'title', label: '标题', type: 'text' },
      { key: 'category', label: '分类', type: 'text' },
      { key: 'confidence', label: '置信度', type: 'text' },
      { key: 'content', label: '内容', type: 'textarea' },
    ],
  },
  {
    key: 'hidden_threads',
    label: '暗线',
    emptyText: '未提炼到暗线',
    fields: [
      { key: 'name', label: '名称', type: 'text' },
      { key: 'chapter_nums', label: '关联章节', type: 'csv' },
      { key: 'description', label: '描述', type: 'textarea' },
    ],
  },
  {
    key: 'character_relations',
    label: '角色关系',
    emptyText: '未提炼到角色关系',
    fields: [
      { key: 'source', label: '来源角色', type: 'text' },
      { key: 'target', label: '目标角色', type: 'text' },
      { key: 'description', label: '描述', type: 'textarea' },
    ],
  },
]

const groups = reactive<EditableGroup[]>(GROUP_DEFINITIONS.map((group) => ({ ...group, items: [] })))

const selectedCount = computed(() => groups.reduce((total, group) => {
  return total + group.items.filter((item) => item.checked).length
}, 0))

watch(
  () => [props.payload, props.show],
  () => {
    resetGroups()
  },
  { immediate: true, deep: true },
)

function resetGroups() {
  for (const group of groups) {
    const value = props.payload?.[group.key]
    const records = normalizeRecords(value)
    group.items.splice(0, group.items.length, ...records.map((record, index) => ({
      id: String(record.id ?? `${group.key}-${index}`),
      checked: true,
      data: buildEditableData(record, group.fields),
      original: cloneRecord(record),
    })))
  }
}

function normalizeRecords(value: unknown): ExtractRecord[] {
  if (!value) return []
  if (Array.isArray(value)) return value.map((record) => cloneRecord(record as ExtractRecord))
  return [cloneRecord(value as ExtractRecord)]
}

function cloneRecord(record: ExtractRecord): ExtractRecord {
  return JSON.parse(JSON.stringify(record ?? {})) as ExtractRecord
}

function buildEditableData(record: ExtractRecord, fields: EditableField[]): Record<string, string | number> {
  return fields.reduce<Record<string, string | number>>((data, field) => {
    const value = record[field.key]
    if (field.type === 'csv') {
      data[field.key] = Array.isArray(value) ? value.join(', ') : stringifyValue(value)
    } else if (field.type === 'number') {
      data[field.key] = typeof value === 'number' ? value : Number(value ?? 0)
    } else {
      data[field.key] = stringifyValue(value)
    }
    return data
  }, {})
}

function stringifyValue(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value)
}

function fieldValue(item: EditableItem, field: EditableField): string | number {
  return item.data[field.key] ?? ''
}

function updateField(item: EditableItem, field: EditableField, value: string) {
  item.data[field.key] = field.type === 'number' ? Number(value) : value
}

function confirmSelection() {
  const nextPayload = groups.reduce<Record<string, unknown>>((payload, group) => {
    const selectedItems = group.items
      .filter((item) => item.checked)
      .map((item) => buildOutputRecord(item, group.fields))

    payload[group.key] = selectedItems

    return payload
  }, {})

  emit('confirm', nextPayload as StructureExtractPayload)
}

function buildOutputRecord(item: EditableItem, fields: EditableField[]): ExtractRecord {
  const output: ExtractRecord = cloneRecord(item.original)

  for (const field of fields) {
    const value = item.data[field.key]
    if (field.type === 'csv') {
      output[field.key] = parseCsvNumbers(String(value ?? ''))
    } else if (field.type === 'number') {
      output[field.key] = Number(value ?? 0)
    } else {
      output[field.key] = String(value ?? '').trim()
    }
  }

  return output
}

function parseCsvNumbers(value: string): number[] {
  return value
    .split(',')
    .map((part) => Number(part.trim()))
    .filter((number) => Number.isFinite(number))
}

function itemTitle(item: EditableItem, group: EditableGroup): string {
  const preferredKey = group.key === 'hidden_threads' ? 'name' : 'title'
  const fallbackKey = group.key === 'characters' || group.key === 'character_relations' ? 'name' : 'description'
  return String(item.data[preferredKey] || item.data[fallbackKey] || '未命名条目')
}
</script>

<template>
  <div v-if="show" class="structure-overlay" @click.self="emit('close')">
    <section class="structure-modal" role="dialog" aria-modal="true" :aria-label="title">
      <header class="structure-header">
        <div>
          <h3>{{ title }}</h3>
          <p>勾选要写入的条目，并在确认前修正基础字段。</p>
        </div>
        <button class="icon-close" type="button" aria-label="关闭" @click="emit('close')">×</button>
      </header>

      <div class="structure-body">
        <section v-for="group in groups" :key="group.key" class="extract-group">
          <div class="group-heading">
            <h4>{{ group.label }}</h4>
            <span>{{ group.items.length }} 项</span>
          </div>

          <p v-if="group.items.length === 0" class="empty-state">{{ group.emptyText }}</p>

          <article v-for="item in group.items" :key="item.id" class="extract-item" :class="{ muted: !item.checked }">
            <label class="item-check">
              <input v-model="item.checked" type="checkbox">
              <span>{{ itemTitle(item, group) }}</span>
            </label>

            <div class="field-grid">
              <label v-for="field in group.fields" :key="field.key" class="field" :class="{ wide: field.type === 'textarea' }">
                <span>{{ field.label }}</span>
                <textarea
                  v-if="field.type === 'textarea'"
                  :value="fieldValue(item, field)"
                  rows="3"
                  @input="updateField(item, field, ($event.target as HTMLTextAreaElement).value)"
                />
                <input
                  v-else
                  :value="fieldValue(item, field)"
                  :type="field.type === 'number' ? 'number' : 'text'"
                  :placeholder="field.type === 'csv' ? '例如：1, 3, 5' : undefined"
                  @input="updateField(item, field, ($event.target as HTMLInputElement).value)"
                >
              </label>
            </div>
          </article>
        </section>
      </div>

      <footer class="structure-actions">
        <span>已选择 {{ selectedCount }} 项</span>
        <div class="action-buttons">
          <button class="btn-secondary" type="button" @click="emit('close')">取消</button>
          <button class="btn-primary" type="button" @click="confirmSelection">{{ confirmText }}</button>
        </div>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.structure-overlay {
  position: fixed;
  inset: 0;
  z-index: 2200;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-4);
  background: rgba(0, 0, 0, 0.56);
}

.structure-modal {
  width: min(960px, 100%);
  max-height: min(86vh, 860px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-lg);
}

.structure-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-4) var(--sp-5);
  border-bottom: 1px solid var(--border);
  background: var(--bg-panel);
}

.structure-header h3 {
  margin: 0;
  color: var(--text);
  font-size: var(--text-lg);
  font-weight: 700;
}

.structure-header p {
  margin: var(--sp-1) 0 0;
  color: var(--text-secondary);
  font-size: var(--text-sm);
}

.icon-close {
  width: 32px;
  height: 32px;
  flex: 0 0 32px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--text-secondary);
  font-size: 20px;
  line-height: 1;
}

.icon-close:hover {
  background: var(--bg-hover);
  color: var(--text);
}

.structure-body {
  flex: 1;
  overflow: auto;
  padding: var(--sp-4) var(--sp-5);
}

.extract-group + .extract-group {
  margin-top: var(--sp-5);
}

.group-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  margin-bottom: var(--sp-3);
}

.group-heading h4 {
  margin: 0;
  color: var(--text);
  font-size: var(--text-base);
  font-weight: 700;
}

.group-heading span {
  color: var(--text-tertiary);
  font-size: var(--text-xs);
}

.empty-state {
  margin: 0;
  padding: var(--sp-3);
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  color: var(--text-tertiary);
  font-size: var(--text-sm);
}

.extract-item {
  padding: var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-sidebar);
  transition: border-color var(--transition), opacity var(--transition);
}

.extract-item + .extract-item {
  margin-top: var(--sp-3);
}

.extract-item.muted {
  opacity: 0.58;
}

.item-check {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
  color: var(--text);
  font-size: var(--text-sm);
  font-weight: 700;
}

.item-check input {
  width: 16px;
  height: 16px;
  padding: 0;
  accent-color: var(--accent);
}

.field-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--sp-3);
}

.field {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: var(--sp-1);
}

.field.wide {
  grid-column: 1 / -1;
}

.field span {
  color: var(--text-secondary);
  font-size: var(--text-xs);
  font-weight: 600;
}

.field input,
.field textarea {
  width: 100%;
  border-radius: var(--radius);
  background: var(--bg-input);
}

.field textarea {
  resize: vertical;
  line-height: 1.6;
}

.structure-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-5);
  border-top: 1px solid var(--border);
  background: var(--bg-sidebar);
  color: var(--text-secondary);
  font-size: var(--text-sm);
}

.action-buttons {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--sp-2);
}

.btn-secondary,
.btn-primary {
  min-height: 36px;
  padding: 0 var(--sp-4);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  font-weight: 600;
  transition: background var(--transition), border-color var(--transition), color var(--transition);
}

.btn-secondary {
  border: 1px solid var(--border);
  background: var(--bg-panel);
  color: var(--text-secondary);
}

.btn-secondary:hover {
  border-color: var(--border-focus);
  background: var(--bg-hover);
  color: var(--text);
}

.btn-primary {
  border: 1px solid var(--accent);
  background: var(--accent);
  color: var(--text-inverse);
}

.btn-primary:hover {
  border-color: var(--accent-hover);
  background: var(--accent-hover);
}

@media (max-width: 640px) {
  .structure-overlay {
    align-items: stretch;
    padding: var(--sp-2);
  }

  .structure-modal {
    max-height: 100%;
  }

  .structure-header,
  .structure-body,
  .structure-actions {
    padding-left: var(--sp-3);
    padding-right: var(--sp-3);
  }

  .field-grid {
    grid-template-columns: 1fr;
  }

  .structure-actions,
  .action-buttons {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
