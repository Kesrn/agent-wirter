import { describe, expect, it } from 'vitest'

import articleParamsPickerSource from './ArticleParamsPicker.vue?raw'
import approvalModalSource from './ApprovalModal.vue?raw'
import confirmModalSource from './ConfirmModal.vue?raw'
import contextPickerSource from './ContextPicker.vue?raw'
import directionPickerSource from './DirectionPicker.vue?raw'
import enhancePickerSource from './EnhancePicker.vue?raw'
import novelExtractionSource from './NovelExtraction.vue?raw'
import revisionSuggestionPickerSource from './RevisionSuggestionPicker.vue?raw'
import structureExtractPreviewSource from './StructureExtractPreview.vue?raw'
import turnPickerSource from './TurnPicker.vue?raw'
import versionDiffViewerSource from './VersionDiffViewer.vue?raw'
import writingEditorSource from './WritingEditor.vue?raw'
import projectListSource from '../views/ProjectList.vue?raw'

const modalSources = [
  ['ArticleParamsPicker', articleParamsPickerSource],
  ['ApprovalModal', approvalModalSource],
  ['ConfirmModal', confirmModalSource],
  ['ContextPicker', contextPickerSource],
  ['DirectionPicker', directionPickerSource],
  ['EnhancePicker', enhancePickerSource],
  ['NovelExtraction', novelExtractionSource],
  ['RevisionSuggestionPicker', revisionSuggestionPickerSource],
  ['StructureExtractPreview', structureExtractPreviewSource],
  ['TurnPicker', turnPickerSource],
  ['VersionDiffViewer', versionDiffViewerSource],
  ['WritingEditor', writingEditorSource],
  ['ProjectList', projectListSource],
] as const

describe('modal dismissal contract', () => {
  it.each(modalSources)('%s does not close from backdrop clicks', (_name, source) => {
    expect(source).not.toContain('@click.self')
  })

  it('approval modal only rejects from the explicit reject button', () => {
    expect(approvalModalSource).not.toContain('class="modal-overlay" @click')
    expect(approvalModalSource).toContain('@click="emit(\'decision\', \'reject\')"')
  })
})
