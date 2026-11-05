import { describe, it, expect } from 'vitest'
import {
  formatQualityRuleCode,
  formatDurationMs,
  formatCost,
} from './runTrace'

describe('runTrace utils', () => {
  it('maps known quality rule codes to Chinese descriptions', () => {
    expect(formatQualityRuleCode('RULE_MUST_INCLUDE_MISSING')).toBe('必含事件缺失')
    expect(formatQualityRuleCode('RULE_FORBIDDEN_PRESENT')).toBe('出现禁止内容')
    expect(formatQualityRuleCode('RULE_CHARACTER_FORBIDDEN')).toBe('禁出场人物出现')
    expect(formatQualityRuleCode('RULE_CHARACTER_ABSENT')).toBe('应出场人物缺席')
    expect(formatQualityRuleCode('RULE_NAME_VARIANT')).toBe('人名疑似错写')
    expect(formatQualityRuleCode('RULE_BANNED_PHRASE')).toBe('AI 腔词汇偏多')
  })

  it('keeps unknown quality rule codes as-is', () => {
    expect(formatQualityRuleCode('RULE_CUSTOM_UNKNOWN')).toBe('RULE_CUSTOM_UNKNOWN')
    expect(formatQualityRuleCode('SOME_OTHER_ERROR')).toBe('SOME_OTHER_ERROR')
  })

  it('formats duration correctly', () => {
    expect(formatDurationMs(null)).toBe('-')
    expect(formatDurationMs(undefined)).toBe('-')
    expect(formatDurationMs(450)).toBe('450ms')
    expect(formatDurationMs(12000)).toBe('12s')
    expect(formatDurationMs(12500)).toBe('12.5s')
  })

  it('formats cost correctly', () => {
    expect(formatCost(null)).toBe('未配置价格')
    expect(formatCost(undefined)).toBe('未配置价格')
    expect(formatCost(0.031)).toBe('$0.0310')
    expect(formatCost(0)).toBe('$0.0000')
  })
})
