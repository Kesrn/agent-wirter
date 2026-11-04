import { describe, expect, it } from 'vitest'

import ccwSource from './ConversationCreativeWorkspace.vue?raw'
import panelSource from './ProjectLibraryPanel.vue?raw'

describe('对话创作工作区：左侧资料库入口', () => {
  it('人物档案/世界设定/故事大纲/创作记忆四个按钮都绑定了点击事件', () => {
    // 修复回归：四个按钮此前是无 @click 的纯展示按钮，点击无反应
    expect(ccwSource).toContain("openLibrary('characters')")
    expect(ccwSource).toContain("openLibrary('world')")
    expect(ccwSource).toContain("openLibrary('outlines')")
    expect(ccwSource).toContain("openLibrary('threads')")
    // 每个按钮都带 @click（不是裸 button）
    const linkButtons = ccwSource.match(/class="ccw-rail-link"[^>]*>/g) ?? []
    expect(linkButtons).toHaveLength(4)
    expect(linkButtons.every(b => b.includes('@click'))).toBe(true)
  })

  it('点击入口打开 ProjectLibraryPanel（完整管理弹窗）', () => {
    expect(ccwSource).toContain("const activeLibrary = ref<")
    expect(ccwSource).toContain('<ProjectLibraryPanel')
    expect(ccwSource).toContain(':active="activeLibrary"')
    expect(ccwSource).toContain('@close="closeLibrary"')
  })

  it('P1：切换章节/项目时清空上一章的选区上下文（editorSelection 重置）', () => {
    // 修复回归：切换章节/项目只刷新版本，未清空 selection/currentParagraph，
    // 会把第一章的选中文本带入第二章的对话请求。
    expect(ccwSource).toContain('editorSelection.value = { selection: null, currentParagraph: null }')
    // 清空发生在版本刷新 watcher 内、且不依赖目标单元存在（位于 return 之前）
    const resetIdx = ccwSource.indexOf('editorSelection.value = { selection: null')
    const unitGuardIdx = ccwSource.indexOf('if (!unit) return', resetIdx)
    expect(resetIdx).toBeGreaterThanOrEqual(0)
    expect(unitGuardIdx).toBeGreaterThan(resetIdx)
    // 选区上下文确实进入对话上下文
    expect(ccwSource).toContain('selection: editorSelection.value.selection')
    expect(ccwSource).toContain('currentParagraph: editorSelection.value.currentParagraph')
  })

  it('P0：editorSelection 声明早于 immediate watcher（TDZ 防护）', () => {
    // 修复回归：watcher 配置 immediate:true，setup 阶段立即执行回调；
    // 若 editorSelection 声明在 watcher 之后，会触发 TDZ ReferenceError，
    // 导致创作工作区无法渲染（字符串测试与构建均发现不了）。
    const declIdx = ccwSource.indexOf('const editorSelection = ref<')
    const watchIdx = ccwSource.indexOf('watch([() => props.projectId, currentUnitPosition]')
    expect(declIdx).toBeGreaterThanOrEqual(0)
    expect(watchIdx).toBeGreaterThan(declIdx)
    // watcher 内引用发生在声明之后才安全
    const resetIdx = ccwSource.indexOf('editorSelection.value = { selection: null', watchIdx)
    expect(resetIdx).toBeGreaterThan(watchIdx)
  })
})

describe('ProjectLibraryPanel：完整资料库管理', () => {
  it('弹窗 Teleport 到 body，带 aria-modal 与遮罩/关闭', () => {
    expect(panelSource).toContain('<Teleport to="body">')
    expect(panelSource).toContain('aria-modal="true"')
    expect(panelSource).toContain('@click.self="emit(\'close\')"')
    expect(panelSource).toContain('plp-close')
  })

  it('四个分类 tab 齐全，创作记忆内含「暗线 / 写作记忆」双子 tab', () => {
    expect(panelSource).toContain("kind === 'characters'")
    expect(panelSource).toContain("kind === 'world'")
    expect(panelSource).toContain("kind === 'outlines'")
    expect(panelSource).toContain("kind === 'threads'")
    expect(panelSource).toContain("threadTab === 'threads'")
    expect(panelSource).toContain("threadTab === 'memory'")
    expect(panelSource).toContain('暗线')
    expect(panelSource).toContain('写作记忆')
  })

  it('世界设定嵌入 ProjectWorldLibrary、写作记忆嵌入 MemoryStagingPanel（完整管理）', () => {
    expect(panelSource).toContain("import ProjectWorldLibrary from './ProjectWorldLibrary.vue'")
    expect(panelSource).toContain("import MemoryStagingPanel from './MemoryStagingPanel.vue'")
    expect(panelSource).toContain('<ProjectWorldLibrary')
    expect(panelSource).toContain('<MemoryStagingPanel')
  })

  it('人物/大纲/暗线提供完整 CRUD（新增/编辑/删除）', () => {
    // 人物档案
    expect(panelSource).toContain('characterStore.addCharacter')
    expect(panelSource).toContain('characterStore.updateCharacter')
    expect(panelSource).toContain('characterStore.removeCharacter')
    // 故事大纲
    expect(panelSource).toContain('outlineStore.createOutlineItem')
    expect(panelSource).toContain('outlineStore.updateOutlineItem')
    expect(panelSource).toContain('outlineStore.deleteOutlineItem')
    // 暗线
    expect(panelSource).toContain('hiddenThreadStore.createHiddenThreadItem')
    expect(panelSource).toContain('hiddenThreadStore.updateHiddenThreadItem')
    expect(panelSource).toContain('hiddenThreadStore.deleteHiddenThreadItem')
  })

  it('加载失败明确提示，不静默回退旧数据/Mock（loading + loadError 态）', () => {
    expect(panelSource).toContain('characterStore.loading')
    expect(panelSource).toContain('characterStore.loadError')
    expect(panelSource).toContain('worldEntryStore.loadError')
    expect(panelSource).toContain('outlineStore.loadError')
    expect(panelSource).toContain('hiddenThreadStore.loadError')
    expect(panelSource).toContain('加载失败')
    expect(panelSource).toContain('plp-error')
    // 各 tab 都有空态
    expect(panelSource).toContain('暂无人物档案')
    expect(panelSource).toContain('暂无世界设定')
    expect(panelSource).toContain('暂无章节大纲')
    expect(panelSource).toContain('暂无暗线')
  })
})
