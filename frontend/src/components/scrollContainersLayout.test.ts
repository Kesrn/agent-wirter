import { describe, expect, it } from 'vitest'

import articleParamsPickerSource from './ArticleParamsPicker.vue?raw'
import approvalModalSource from './ApprovalModal.vue?raw'
import contextPickerSource from './ContextPicker.vue?raw'
import directionPickerSource from './DirectionPicker.vue?raw'
import documentRevisionPanelSource from './DocumentRevisionPanel.vue?raw'
import generationHistoryPanelSource from './GenerationHistoryPanel.vue?raw'
import novelConfigSource from './NovelConfig.vue?raw'
import projectWorldLibrarySource from './ProjectWorldLibrary.vue?raw'
import structureExtractPreviewSource from './StructureExtractPreview.vue?raw'
import versionDiffViewerSource from './VersionDiffViewer.vue?raw'
import versionHistoryPanelSource from './VersionHistoryPanel.vue?raw'

function cssBlock(source: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = source.match(new RegExp(`${escaped}\\s*\\{([\\s\\S]*?)\\n\\}`))
  return match?.[1] ?? ''
}

function expectScrollable(block: string) {
  expect(block).toContain('min-height: 0')
  expect(block).toMatch(/overflow(?:-y)?: auto/)
  expect(block).toContain('-webkit-overflow-scrolling: touch')
  expect(block).toContain('overscroll-behavior: contain')
}

describe('scroll container layout contracts', () => {
  it('keeps history side panels scrollable inside the editor shell', () => {
    const generationRoot = cssBlock(generationHistoryPanelSource, '.generation-panel')
    const generationList = cssBlock(generationHistoryPanelSource, '.generation-list')
    const versionRoot = cssBlock(versionHistoryPanelSource, '.version-panel')
    const versionList = cssBlock(versionHistoryPanelSource, '.version-list')
    const documentRevisionRoot = cssBlock(documentRevisionPanelSource, '.version-panel')
    const documentRevisionList = cssBlock(documentRevisionPanelSource, '.version-list')

    expect(generationRoot).toContain('min-height: 0')
    expect(versionRoot).toContain('min-height: 0')
    expect(documentRevisionRoot).toContain('min-height: 0')
    expectScrollable(generationList)
    expectScrollable(versionList)
    expectScrollable(documentRevisionList)
  })

  it('keeps full-height configuration pages scrollable', () => {
    const novelRoot = cssBlock(novelConfigSource, '.novel-config')
    const novelBody = cssBlock(novelConfigSource, '.config-body')
    const libraryRoot = cssBlock(projectWorldLibrarySource, '.project-world-library')
    const libraryBody = cssBlock(projectWorldLibrarySource, '.library-body')

    expect(novelRoot).toContain('min-height: 0')
    expect(libraryRoot).toContain('min-height: 0')
    expectScrollable(novelBody)
    expectScrollable(libraryBody)
  })

  it('keeps modal bodies scrollable without dismissing or clipping long content', () => {
    expectScrollable(cssBlock(contextPickerSource, '.picker-body'))
    expectScrollable(cssBlock(directionPickerSource, '.picker-body'))
    expectScrollable(cssBlock(articleParamsPickerSource, '.picker-body'))
    expectScrollable(cssBlock(approvalModalSource, '.modal-body'))
    expectScrollable(cssBlock(structureExtractPreviewSource, '.structure-body'))
    expectScrollable(cssBlock(versionDiffViewerSource, '.diff-desk'))
    expectScrollable(cssBlock(versionDiffViewerSource, '.page-body'))
    expect(cssBlock(versionDiffViewerSource, '.diff-page')).toContain('min-height: 0')
  })
})
