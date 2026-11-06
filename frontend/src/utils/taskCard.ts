import type {
  TaskCardInformationRules,
  TaskCardPayload,
  TaskCardScene,
} from '../api/types'

function stringList(value: unknown): string[] {
  if (typeof value === 'string') {
    return value
      .split(/[,，;；\n]+/)
      .map(item => item.trim())
      .filter(Boolean)
  }
  if (Array.isArray(value)) {
    return value
      .filter(item => ['string', 'number'].includes(typeof item))
      .map(item => String(item).trim())
      .filter(Boolean)
  }
  return []
}

/**
 * 对模型返回和原始 JSON 编辑后的任务卡做运行时标准化。
 *
 * TypeScript 类型不能约束接口真实数据；这里确保页面模板和恢复接口拿到的
 * information_rules/scenes/forbidden 始终是可安全编辑的对象或数组。
 */
export function normalizeTaskCardPayload(value: unknown): TaskCardPayload {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return {
      scenes: [],
      information_rules: { may_reveal: [], hint_only: [], forbidden: [] },
      character_goals: [],
      tension_design: [],
      forbidden: [],
    }
  }

  const outer = value as Record<string, unknown>
  const nested = outer.ChapterTaskCard ?? outer.chapter_task_card
  const source = (
    nested && typeof nested === 'object' && !Array.isArray(nested)
      ? nested
      : outer
  ) as Record<string, unknown>

  const rawScenes = source.scenes
  const scenes: TaskCardScene[] = []
  if (Array.isArray(rawScenes)) {
    rawScenes.forEach((scene, index) => {
      if (scene && typeof scene === 'object' && !Array.isArray(scene)) {
        const sceneSource = scene as Record<string, unknown>
        scenes.push({
          ...(sceneSource as TaskCardScene),
          characters: stringList(sceneSource.characters),
          must_include: stringList(sceneSource.must_include),
          must_not_include: stringList(sceneSource.must_not_include),
        })
      } else if (typeof scene === 'string' && scene.trim()) {
        scenes.push({ title: `场景${index + 1}`, scene_goal: scene.trim() })
      }
    })
  } else if (typeof rawScenes === 'string') {
    stringList(rawScenes).forEach((sceneGoal, index) => {
      scenes.push({ title: `场景${index + 1}`, scene_goal: sceneGoal })
    })
  }

  const rawRules = source.information_rules
  let informationRules: TaskCardInformationRules
  if (rawRules && typeof rawRules === 'object' && !Array.isArray(rawRules)) {
    const rules = rawRules as Record<string, unknown>
    informationRules = {
      may_reveal: stringList(rules.may_reveal),
      hint_only: stringList(rules.hint_only),
      forbidden: stringList(rules.forbidden),
    }
  } else {
    informationRules = {
      may_reveal: [],
      hint_only: [],
      // 无法识别其精确分类时采用更保守的“禁止揭示”语义。
      forbidden: stringList(rawRules),
    }
  }

  const normalized = {
    ...source,
    scenes,
    information_rules: informationRules,
    character_goals: stringList(source.character_goals),
    tension_design: stringList(source.tension_design),
    forbidden: stringList(source.forbidden),
  } as TaskCardPayload

  if (typeof source.word_budget === 'string') {
    const match = source.word_budget.match(/\d+/)
    if (match) normalized.word_budget = Number(match[0])
  }

  return normalized
}
