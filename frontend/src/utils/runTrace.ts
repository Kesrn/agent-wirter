export const QUALITY_RULE_CODE_LABELS: Record<string, string> = {
  RULE_MUST_INCLUDE_MISSING: '必含事件缺失',
  RULE_FORBIDDEN_PRESENT: '出现禁止内容',
  RULE_CHARACTER_FORBIDDEN: '禁出场人物出现',
  RULE_CHARACTER_ABSENT: '应出场人物缺席',
  RULE_NAME_VARIANT: '人名疑似错写',
  RULE_BANNED_PHRASE: 'AI 腔词汇偏多',
}

export function formatQualityRuleCode(code: string): string {
  return QUALITY_RULE_CODE_LABELS[code] || code
}

export function formatDurationMs(ms: number | null | undefined): string {
  if (ms == null) return '-'
  if (ms < 1000) return `${ms}ms`
  const sec = ms / 1000
  return Number.isInteger(sec) ? `${sec}s` : `${sec.toFixed(1)}s`
}

export function formatCost(cost: number | null | undefined): string {
  if (cost == null) return '未配置价格'
  return `$${cost.toFixed(4)}`
}
