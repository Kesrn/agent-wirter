<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api, ApiError } from '../api/client'
import type { EditStatsItem, EditStatsResponse } from '../api/types'

const props = defineProps<{
  projectId: string
}>()

const stats = ref<EditStatsResponse | null>(null)
const loading = ref(Boolean(props.projectId))
const error = ref('')

const hoveredItem = ref<EditStatsItem | null>(null)
const tooltipX = ref(0)
const tooltipY = ref(0)

const summary = computed(() => stats.value?.summary ?? null)
const items = computed(() => stats.value?.items ?? [])

function formatPercent(val: number | null | undefined): string {
  if (val == null) return '-'
  const pct = val * 100
  return Number.isInteger(pct) ? `${pct}%` : `${pct.toFixed(1)}%`
}

async function loadData() {
  if (!props.projectId) return
  loading.value = true
  error.value = ''
  try {
    stats.value = await api.getEditStats(props.projectId, { limit: 20 })
  } catch (e: unknown) {
    if (e instanceof ApiError) {
      error.value = e.message
    } else if (e instanceof Error) {
      error.value = e.message
    } else {
      error.value = '加载人工改动统计失败'
    }
  } finally {
    loading.value = false
  }
}

// SVG chart dimensions
const CHART_WIDTH = 320
const CHART_HEIGHT = 100
const PAD_LEFT = 24
const PAD_RIGHT = 20
const PAD_TOP = 14
const PAD_BOTTOM = 22

const plotWidth = CHART_WIDTH - PAD_LEFT - PAD_RIGHT
const plotHeight = CHART_HEIGHT - PAD_TOP - PAD_BOTTOM

// Reverse to show chronological trend left-to-right
const trendItems = computed(() => [...items.value].slice(0, 20).reverse())

function getPointX(index: number, total: number): number {
  if (total <= 1) return PAD_LEFT + plotWidth / 2
  return PAD_LEFT + (index * plotWidth) / (total - 1)
}

function getPointY(ratio: number): number {
  const boundedRatio = Math.min(1, Math.max(0, ratio))
  return PAD_TOP + (1 - boundedRatio) * plotHeight
}

const polylinePoints = computed(() => {
  const list = trendItems.value
  if (!list.length) return ''
  return list.map((item, i) => `${getPointX(i, list.length).toFixed(1)},${getPointY(item.edit_ratio).toFixed(1)}`).join(' ')
})

function onPointHover(item: EditStatsItem, index: number, total: number) {
  hoveredItem.value = item
  tooltipX.value = getPointX(index, total)
  tooltipY.value = getPointY(item.edit_ratio)
}

function onPointLeave() {
  hoveredItem.value = null
}

watch(() => props.projectId, () => {
  loadData()
})

onMounted(() => {
  loadData()
})
</script>

<template>
  <div class="edit-stats-panel">
    <div class="stats-header">
      <div class="stats-header-left">
        <span class="stats-title">人工改动统计</span>
        <div class="stats-subtitle">统计的是 AI 稿被采用后你又改了多少；还没改的章节按 0 计，不代表满意。</div>
      </div>
      <button class="stats-refresh-btn stats-reload-btn" type="button" :disabled="loading" @click="loadData">
        重新加载
      </button>
    </div>

    <!-- Loading State -->
    <div v-if="loading" class="stats-state stats-loading">
      <span class="spinner" />
      <span>加载统计数据中...</span>
    </div>

    <!-- Error State -->
    <div v-else-if="error" class="stats-state stats-error" role="alert">
      <span class="error-msg">{{ error }}</span>
      <button class="btn-retry" type="button" @click="loadData">重试</button>
    </div>

    <!-- Empty State -->
    <div v-else-if="!summary || summary.count === 0" class="stats-state stats-empty">
      还没有已采用的生成记录
    </div>

    <!-- Loaded Content -->
    <div v-else class="stats-content">
      <div class="summary-grid">
        <div class="summary-card">
          <span class="card-label">采用次数</span>
          <span class="card-value">{{ summary.count }}</span>
        </div>
        <div class="summary-card">
          <span class="card-label">改动过的次数</span>
          <span class="card-value">{{ summary.edited_count }}</span>
        </div>
        <div class="summary-card">
          <span class="card-label">平均改动比例</span>
          <span class="card-value highlight">{{ formatPercent(summary.mean_edit_ratio) }}</span>
        </div>
        <div class="summary-card">
          <span class="card-label">中位数改动比例</span>
          <span class="card-value">{{ formatPercent(summary.median_edit_ratio) }}</span>
        </div>
        <div class="summary-card">
          <span class="card-label">P90 改动比例</span>
          <span class="card-value">{{ formatPercent(summary.p90_edit_ratio) }}</span>
        </div>
        <div class="summary-card">
          <span class="card-label">改动 / 原稿字数</span>
          <span class="card-value">{{ summary.total_changed_chars }} / {{ summary.total_original_chars }}</span>
        </div>
      </div>

      <!-- Trend Chart -->
      <div v-if="trendItems.length" class="trend-section">
        <div class="trend-title">最近改动比例走势</div>
        <div class="chart-container">
          <svg
            class="trend-svg"
            :viewBox="`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`"
            preserveAspectRatio="none"
          >
            <!-- Baseline 0% and 100% -->
            <line
              :x1="PAD_LEFT"
              :y1="PAD_TOP"
              :x2="CHART_WIDTH - PAD_RIGHT"
              :y2="PAD_TOP"
              class="chart-gridline"
            />
            <line
              :x1="PAD_LEFT"
              :y1="PAD_TOP + plotHeight"
              :x2="CHART_WIDTH - PAD_RIGHT"
              :y2="PAD_TOP + plotHeight"
              class="chart-gridline"
            />

            <!-- Y Axis labels -->
            <text :x="PAD_LEFT - 4" :y="PAD_TOP + 4" class="chart-axis-label">100%</text>
            <text :x="PAD_LEFT - 4" :y="PAD_TOP + plotHeight + 3" class="chart-axis-label">0%</text>

            <!-- Line -->
            <polyline
              v-if="polylinePoints && trendItems.length > 1"
              :points="polylinePoints"
              class="chart-line"
            />

            <!-- Data Points -->
            <circle
              v-for="(item, idx) in trendItems"
              :key="item.generation_record_id"
              :cx="getPointX(idx, trendItems.length)"
              :cy="getPointY(item.edit_ratio)"
              r="3.5"
              class="chart-dot"
              :class="{ active: hoveredItem?.generation_record_id === item.generation_record_id }"
              @mouseenter="onPointHover(item, idx, trendItems.length)"
              @mouseleave="onPointLeave"
            />
          </svg>

          <!-- Hover Tooltip -->
          <div
            v-if="hoveredItem"
            class="chart-tooltip"
            :style="{ left: `${tooltipX}px`, top: `${Math.max(0, tooltipY - 28)}px` }"
            :title="hoveredItem.approximate ? '改动较大，按保守估算' : undefined"
          >
            <span v-if="hoveredItem.chapter_sequence_number">第 {{ hoveredItem.chapter_sequence_number }} 章 · </span>
            <span v-if="hoveredItem.edited === false || hoveredItem.edit_ratio === 0">未改动</span>
            <span v-else>改动{{ hoveredItem.approximate ? '约 ' : ' ' }}{{ formatPercent(hoveredItem.edit_ratio) }}</span>
            <span v-if="hoveredItem.approximate">（改动较大，按保守估算）</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.edit-stats-panel {
  background: var(--bg-surface, var(--bg-panel));
  border: 1px solid var(--border);
  border-radius: var(--radius-md, 8px);
  padding: var(--sp-3, 12px);
  margin-bottom: var(--sp-3, 12px);
  font-size: var(--text-xs, 12px);
}

.stats-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--sp-2, 8px);
  margin-bottom: var(--sp-2, 8px);
}

.stats-header-left {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.stats-subtitle {
  font-size: 11px;
  color: var(--text-tertiary);
  line-height: 1.4;
}

.stats-title {
  font-weight: 700;
  color: var(--text);
  font-size: var(--text-sm, 13px);
}

.stats-refresh-btn {
  font-size: 11px;
  padding: 2px 8px;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  color: var(--text-secondary);
  cursor: pointer;
}

.stats-refresh-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--accent);
}

.stats-state {
  padding: var(--sp-4);
  text-align: center;
  color: var(--text-tertiary);
}

.stats-error {
  color: var(--status-error);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  align-items: center;
}

.btn-retry {
  padding: 3px 10px;
  font-size: 11px;
  background: var(--accent);
  color: var(--text-inverse);
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
}

.summary-card {
  display: flex;
  flex-direction: column;
  padding: 8px 10px;
  background: var(--bg-hover);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.card-label {
  font-size: 10px;
  color: var(--text-tertiary);
  margin-bottom: 2px;
}

.card-value {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  font-weight: 650;
  color: var(--text);
}

.card-value.highlight {
  color: var(--accent);
}

.trend-section {
  position: relative;
  background: var(--bg-hover);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px;
}

.trend-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 4px;
}

.chart-container {
  position: relative;
  width: 100%;
}

.trend-svg {
  width: 100%;
  height: 90px;
  display: block;
  overflow: visible;
}

.chart-gridline {
  stroke: color-mix(in srgb, var(--border) 40%, transparent);
  stroke-dasharray: 2 2;
  stroke-width: 1;
}

.chart-axis-label {
  font-size: 8px;
  fill: var(--text-tertiary);
  text-anchor: end;
}

.chart-line {
  fill: none;
  stroke: var(--accent);
  stroke-width: 2;
  stroke-linejoin: round;
  stroke-linecap: round;
}

.chart-dot {
  fill: var(--bg-panel);
  stroke: var(--accent);
  stroke-width: 2;
  cursor: pointer;
  transition: transform 0.1s ease;
}

.chart-dot:hover,
.chart-dot.active {
  fill: var(--accent);
  r: 4.5;
}

.chart-tooltip {
  position: absolute;
  transform: translate(-50%, -100%);
  background: var(--bg-panel);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 3px 6px;
  border-radius: var(--radius-sm);
  font-size: 10px;
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  pointer-events: none;
  z-index: 10;
  box-shadow: var(--shadow-sm);
}
</style>
