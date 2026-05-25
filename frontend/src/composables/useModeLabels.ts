import { computed } from 'vue'
import type { ProjectMode } from '../api/types'

/** Mode-aware naming helpers for chapter/chapter_num concepts */
export function useModeLabels(mode: ProjectMode | undefined) {
  const isNovel = computed(() => mode !== 'article')

  const unitLabel = computed(() => isNovel.value ? '章节' : '稿件')
  const unitNumLabel = computed(() => isNovel.value ? '第{n}章' : '第{n}篇')
  const outlineLabel = computed(() => isNovel.value ? '大纲' : '内容结构')
  const characterLabel = computed(() => isNovel.value ? '角色' : '受众画像')
  const worldLabel = computed(() => isNovel.value ? '世界观' : '品牌/产品资料')
  const hiddenThreadLabel = computed(() => isNovel.value ? '暗线' : '内容策略')
  const draftLabel = computed(() => isNovel.value ? '草稿' : '初稿')
  const finalLabel = computed(() => isNovel.value ? '终稿' : '定稿')

  function formatUnitNum(n: number): string {
    return unitNumLabel.value.replace('{n}', String(n))
  }

  return {
    isNovel,
    unitLabel,
    unitNumLabel,
    outlineLabel,
    characterLabel,
    worldLabel,
    hiddenThreadLabel,
    draftLabel,
    finalLabel,
    formatUnitNum,
  }
}
