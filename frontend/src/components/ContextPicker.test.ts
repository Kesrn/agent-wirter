import { describe, expect, it } from 'vitest'
import source from './ContextPicker.vue?raw'
import agentPanelSource from './AgentPanel.vue?raw'

// 由于现有测试环境无 DOM（无 @vue/test-utils / happy-dom），这里沿用源码断言
// 的方式（与 writingEditorLayout.test.ts 一致），验证默认选择逻辑存在且正确。
const scriptBlock = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)?.[1] ?? ''

describe('ContextPicker default outline selection', () => {
  it('scopes default outline selection to the current chapter in novel mode', () => {
    // 小说模式下默认只勾选本章大纲，而不是全选所有章节大纲
    expect(scriptBlock).toContain('currentChapterOutlineIds')
    expect(scriptBlock).toContain('o.chapter_num === props.currentChapterNum')
    expect(scriptBlock).toContain('selectedOutlines = ref<string[]>(currentChapterOutlineIds())')
  })

  it('toggleAllOutlines stays within the current chapter in novel mode', () => {
    // “全选”在小说模式下只在本章范围内切换，不再带入其他章节大纲
    expect(scriptBlock).toContain('isNovel.value ? currentChapterOutlineIds() : props.outlines.map(o => o.id)')
  })

  it('shows “本章” label for outline select-all in novel mode', () => {
    const templateBlock = source.match(/<template>([\s\S]*?)<\/template>/)?.[1] ?? ''
    expect(templateBlock).toContain('outlineSelectAllLabel')
    expect(scriptBlock).toContain("isNovel.value ? '本章' : '全选'")
  })
})

describe('ContextPicker generation requirements', () => {
  it('renders a user note textarea for this generation', () => {
    const templateBlock = source.match(/<template>([\s\S]*?)<\/template>/)?.[1] ?? ''
    expect(templateBlock).toContain('本轮写作要求')
    expect(templateBlock).toContain('v-model="userNote"')
    expect(templateBlock).toContain('requirements-input')
    expect(templateBlock).toContain('更强调主角心理变化')
  })

  it('emits trimmed userNote when confirming generation', () => {
    expect(scriptBlock).toContain("const userNote = ref('')")
    expect(scriptBlock).toContain('userNote.value.trim()')
    expect(scriptBlock).toContain('targetWords: number, userNote: string, includeKnowledgeSources: boolean')
  })
})

describe('ContextPicker knowledge source option', () => {
  it('renders a default-off knowledge source checkbox', () => {
    const templateBlock = source.match(/<template>([\s\S]*?)<\/template>/)?.[1] ?? ''
    expect(scriptBlock).toContain('const includeKnowledgeSources = ref(false)')
    expect(templateBlock).toContain('加入资料库内容')
    expect(templateBlock).toContain('默认不加入资料库')
    expect(templateBlock).toContain('v-model="includeKnowledgeSources"')
  })

  it('emits includeKnowledgeSources when confirming generation', () => {
    expect(scriptBlock).toContain('includeKnowledgeSources.value')
  })
})


describe('ContextPicker current-chapter scope', () => {
  it('receives only current chapter materials from AgentPanel', async () => {
    expect(agentPanelSource).toContain('const currentChapterOutlines = computed')
    expect(agentPanelSource).toContain('const currentChapterCharacters = computed')
    expect(agentPanelSource).toContain('const currentChapterWorldEntries = computed')
    expect(agentPanelSource).toContain('const currentChapterHiddenThreads = computed')
    expect(agentPanelSource).toContain(':outlines="currentChapterOutlines"')
    expect(agentPanelSource).toContain(':characters="currentChapterCharacters"')
    expect(agentPanelSource).toContain(':world-entries="currentChapterWorldEntries"')
    expect(agentPanelSource).toContain(':hidden-threads="currentChapterHiddenThreads"')
  })

  it('filters characters by appeared events in the current chapter', async () => {
    expect(agentPanelSource).toContain('event.chapter_sequence_number === currentUnitPosition.value')
    expect(agentPanelSource).toContain('&& event.appeared')
  })
})
