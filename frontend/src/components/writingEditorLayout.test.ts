import { describe, expect, it } from 'vitest'
import workspaceSource from '../views/Workspace.vue?raw'
import writingEditorSource from './WritingEditor.vue?raw'

function cssBlock(source: string, selector: string): string {
  const match = source.match(new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{([^}]*)\\}`))
  return match?.[1] ?? ''
}

function scriptBlock(source: string): string {
  return source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)?.[1] ?? ''
}

function watchCurrentUnitBody(source: string): string {
  const script = scriptBlock(source)
  const match = script.match(/watch\(currentUnit,\s*\(unit\)\s*=>\s*\{([\s\S]*?)\n\}, \{ immediate: true \}\)/)
  return match?.[1] ?? ''
}

function functionBody(source: string, name: string): string {
  const script = scriptBlock(source)
  const match = script.match(new RegExp(`function ${name}\\(event: KeyboardEvent\\) \\{([\\s\\S]*?)\\n\\}`))
  return match?.[1] ?? ''
}

describe('writing editor layout scroll contract', () => {
  it('keeps Workspace editor area as a non-scrolling flex shell', () => {
    const editorArea = cssBlock(workspaceSource, '.editor-area')
    const mobileEditor = cssBlock(workspaceSource, '.mobile-editor')

    expect(editorArea).toContain('display: flex')
    expect(editorArea).toContain('overflow: hidden')
    expect(editorArea).toContain('min-height: 0')
    expect(mobileEditor).toContain('overflow: hidden')
  })

  it('keeps WritingEditor content as the only scroll layer', () => {
    const rootBlock = cssBlock(writingEditorSource, '.writing-editor')
    const bodyBlock = cssBlock(writingEditorSource, '.editor-body')
    const contentBlock = cssBlock(writingEditorSource, '.editor-content')
    const textareaBlock = cssBlock(writingEditorSource, '.editor-textarea')

    expect(rootBlock).toContain('min-height: 0')
    expect(bodyBlock).toContain('overflow: hidden')
    expect(bodyBlock).toContain('min-height: 0')
    expect(contentBlock).toContain('overflow-y: auto')
    expect(contentBlock).toContain('-webkit-overflow-scrolling: touch')
    expect(contentBlock).toContain('overscroll-behavior: contain')
    expect(contentBlock).toContain('min-height: 0')
    expect(textareaBlock).toContain('overflow-y: hidden')
    expect(textareaBlock).not.toContain('overflow-y: auto')
  })

  it('resizes the textarea to content height instead of creating nested scroll after save', () => {
    expect(writingEditorSource).toContain("el.style.height = 'auto'")
    expect(writingEditorSource).toContain('el.offsetHeight - el.clientHeight')
    expect(writingEditorSource).toContain('Math.ceil(el.scrollHeight + borderBoxAdjustment)')
    expect(writingEditorSource).toContain('Math.max(minHeight, contentHeight)')
    expect(writingEditorSource).toContain('new ResizeObserver')
    expect(writingEditorSource).toContain('watch(currentDraftText, () =>')
    expect(writingEditorSource).toContain("{ flush: 'post' }")
    expect(writingEditorSource).toContain('即使正文未变化，也要恢复')
    expect(writingEditorSource).toContain('preserveScroll = true')
    expect(writingEditorSource).toContain('浏览器可能因此')
    expect(writingEditorSource).toContain('preservedScroller.scrollTop = scrollTop')
    expect(writingEditorSource).toContain('requestAnimationFrame(restoreScroll)')
    expect(writingEditorSource).toContain('snapshotEditorScroll(textareaRef.value)')
    expect(writingEditorSource).toContain('syncTextareaHeight(true, scrollSnapshot ?? undefined)')
    expect(writingEditorSource).toContain('pendingEditorScroll')
    expect(writingEditorSource).toContain('holdEditorScroll')
    expect(writingEditorSource).toContain('if (scrollSnapshot) pendingEditorScroll = scrollSnapshot')
  })

  it('keeps enough scroll clearance below the last novel line', () => {
    const novelContentBlock = cssBlock(writingEditorSource, '.editor-content.novel-content')
    expect(novelContentBlock).toContain('padding: 28px 42px 96px')
    expect(novelContentBlock).toContain('scroll-padding-bottom: 80px')
    expect(writingEditorSource).toContain('padding: 36px 56px 104px')
    expect(writingEditorSource).toContain('document.fonts.ready')
  })
})

describe('writing editor save scroll contract', () => {
  const watchBody = watchCurrentUnitBody(writingEditorSource)

  it('preserves the scroll position when the store writes back the same chapter after save', () => {
    // 保存后 store 会用后端返回对象替换当前章节（id 不变、对象引用变化），
    // watch(currentUnit) 的“同一章节”分支只能重新测量高度，不得重置滚动位置。
    expect(watchBody).toContain('if (unit.id !== lastUnitId)')
    expect(watchBody).toContain('nextTick(() => syncTextareaHeight())')
    expect(watchBody).toContain('即使正文未变化，也要恢复')
    expect(watchBody).toContain('保留当前滚动位置')
    // 同一章节回写不得出现在“切换章节”分支之外的重置调用
    expect(watchBody.match(/syncTextareaHeight\(false\)/g) ?? []).toHaveLength(1)
  })

  it('resets the scroll position only when switching to another chapter', () => {
    // syncTextareaHeight(false) 必须位于切换分支内部、且在 return 之前，
    // 保证切章时回到新章节顶部，不继承上一章的滚动位置。
    expect(watchBody).toMatch(
      /if \(unit\.id !== lastUnitId\) \{[\s\S]*?syncTextareaHeight\(false\)[\s\S]*?\n\s*return/,
    )
  })

  it('routes manual and auto saves through the same preserve-scroll path', () => {
    // 手动保存与 30 秒防抖自动保存都汇入 handleSave，回写后统一由
    // watch(currentUnit) 的同一章节分支处理，因此两种保存都保留滚动位置。
    expect(writingEditorSource).toContain("async function handleSave(source: 'manual' | 'auto' = 'manual')")
    expect(writingEditorSource).toContain('handleManualSave()')
    expect(writingEditorSource).toContain("handleSave('manual')")
    expect(writingEditorSource).toContain("handleSave('auto')")
    expect(writingEditorSource).toContain('setTimeout(() => {')
    expect(writingEditorSource).toContain('}, 30000)')
  })
})

describe('writing editor save keyboard shortcut', () => {
  const script = scriptBlock(writingEditorSource)
  const keydownBody = functionBody(writingEditorSource, 'handleGlobalKeydown')

  it('binds Cmd/Ctrl+S to save on the window so it works outside the textarea', () => {
    expect(script).toContain("window.addEventListener('keydown', handleGlobalKeydown)")
    expect(script).toContain("window.removeEventListener('keydown', handleGlobalKeydown)")
    // mac 用 metaKey（⌘），Windows/Linux 用 ctrlKey（Ctrl），都映射到保存
    expect(keydownBody).toContain('const mod = event.metaKey || event.ctrlKey')
    expect(keydownBody).toContain("event.key.toLowerCase() !== 's'")
    // 拦截浏览器默认的“保存网页”行为，避免与编辑器保存冲突
    expect(keydownBody).toContain('event.preventDefault()')
    expect(keydownBody).toContain('handleManualSave()')
  })

  it('ignores Cmd+S during IME composition or when no unit is selected / save in flight', () => {
    expect(keydownBody).toContain('if (event.isComposing) return')
    expect(keydownBody).toContain('if (!currentUnit.value || saving.value) return')
  })

  it('advertises the shortcut on the save button', () => {
    expect(writingEditorSource).toContain('title="保存 (⌘S / Ctrl+S)"')
  })

  it('subscribes to the desktop save IPC while mounted and disposes on unmount', () => {
    // Electron 下主进程在浏览器进程层捕获快捷键后经 IPC 通知渲染进程；
    // 浏览器环境 desktopApp 不存在时自动跳过，由 window keydown 兜底。
    expect(script).toContain('saveRequestDisposer = window.desktopApp?.onSaveRequested?.(handleManualSave)')
    expect(script).toContain('saveRequestDisposer?.()')
    expect(script).toContain('saveRequestDisposer = undefined')
  })

  it('preserves relative /media/... path format when inserting and saving images without backend origin', () => {
    // Inserting image writes markdown string `![${alt}](${uploaded.url})` directly
    expect(script).toContain('const replacement = `${prefix}![${alt}](${uploaded.url})${suffix}`')
    // Saving routes through chapterStore / documentStore without rewriting markdown storage
    expect(script).toContain('await chapterStore.saveCurrentChapter(props.projectId)')
    expect(script).toContain('await documentStore.saveCurrentDocument(props.projectId)')
  })
})
