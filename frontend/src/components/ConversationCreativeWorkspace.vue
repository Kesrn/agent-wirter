<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  useChapterStore,
  useDocumentStore,
  useConversationStore,
  useCharacterStore,
  useWorldEntryStore,
  useHiddenThreadStore,
  useOutlineStore,
  useChapterVersionStore,
  useDocumentRevisionStore,
} from '../stores'
import type { ProjectMode, WritingUnit } from '../api/types'
import type { ConversationTaskCard } from '../api/chat'
import { displayChapterNumber, stripChapterNumber } from '../utils/chapterTitle'
import ConversationAssistant, { type ConversationContext } from './ConversationAssistant.vue'
import WritingEditor from './WritingEditor.vue'
import CreativeContextPanel from './CreativeContextPanel.vue'
import AgentPanel from './AgentPanel.vue'
import ProjectLibraryPanel from './ProjectLibraryPanel.vue'

/**
 * 对话式创作新版视觉壳层（映射文档阶段一）。
 *
 * 布局：左章节栏 | 中 CreationSurface（对话创作 / 正文编辑）| 右 CreativeContextPanel。
 * 只负责视觉与入口编排，不复制生成业务逻辑：
 * - 对话/快捷能力/候选稿闭环全部转发到隐藏的 AgentPanel 执行宿主（同一 SSE / 任务卡 / 审核逻辑）；
 * - 章节创建 / 资料 / 提炼动作通过事件上抛给 Workspace 处理；
 * - 正文唯一编辑入口是中间的 WritingEditor。
 */

const props = defineProps<{ projectId: string; mode: ProjectMode }>()

const emit = defineEmits<{
  createUnit: [payload: { title: string; position: number; summary?: string }]
  openConfig: [unit: WritingUnit]
  extractMemory: [unit: WritingUnit]
}>()

const chapterStore = useChapterStore()
const documentStore = useDocumentStore()
const conversationStore = useConversationStore()
const characterStore = useCharacterStore()
const worldEntryStore = useWorldEntryStore()
const hiddenThreadStore = useHiddenThreadStore()
const outlineStore = useOutlineStore()
const versionStore = useChapterVersionStore()
const revisionStore = useDocumentRevisionStore()

// ─── 工作对象 ───

const isNovel = computed(() => props.mode === 'novel')
const unitLabel = computed(() => isNovel.value ? '章节' : '稿件')
const writingUnits = computed(() => isNovel.value
  ? chapterStore.chaptersForProject(props.projectId)
  : documentStore.documentsForProject(props.projectId))
const currentWritingUnit = computed(() => isNovel.value
  ? chapterStore.currentChapterForProject(props.projectId)
  : documentStore.currentDocumentForProject(props.projectId))
const currentUnitPosition = computed(() => isNovel.value
  ? chapterStore.currentChapterNum
  : documentStore.currentDocumentPosition)

function unitPosition(unit: WritingUnit): number {
  return 'position' in unit ? unit.position : unit.chapter_num
}

function unitDisplayNum(unit: WritingUnit): number {
  if (isNovel.value) return displayChapterNumber(unitPosition(unit), unit.title)
  return unitPosition(unit)
}

function unitDisplayTitle(unit: WritingUnit): string {
  if (isNovel.value) return stripChapterNumber(unit.title) || unit.title
  return unit.title
}

function unitStatusLabel(unit: WritingUnit): string {
  if (unit.status === 'final') return '终稿'
  if (unit.status === 'reviewing') return '审核'
  if (unit.status === 'revision') return '修订'
  return '草稿'
}

function selectUnit(num: number): void {
  if (isNovel.value) chapterStore.setCurrentChapter(num)
  else documentStore.setCurrentDocument(num)
}

const mainTitle = computed(() => {
  const unit = currentWritingUnit.value
  if (!unit) return `未选择${unitLabel.value}`
  return isNovel.value ? `第 ${currentUnitPosition.value} 章 · ${unitDisplayTitle(unit)}` : `第 ${currentUnitPosition.value} 篇 · ${unitDisplayTitle(unit)}`
})
const wordCount = computed(() => (currentWritingUnit.value?.draft ?? '').replace(/\s+/g, '').length)
const latestVersionNumber = computed(() => isNovel.value
  ? versionStore.versions[0]?.versionNumber ?? null
  : revisionStore.revisions[0]?.versionNumber ?? null)
const mainSub = computed(() => {
  const version = latestVersionNumber.value
  const parts: string[] = []
  if (version != null) parts.push(isNovel.value ? `正式版本 v${version}` : `修订 v${version}`)
  parts.push(`${wordCount.value.toLocaleString('zh-CN')} 字`)
  parts.push('所有 AI 修改先进入候选稿')
  return parts.join(' · ')
})

// 补锁 3：编辑器选中文本 / 光标所在段落 → 对话上下文（"改这段/改选中部分"）。
// 必须声明在下方 immediate watcher 之前（TDZ：watcher 在 setup 阶段立即执行）。
const editorSelection = ref<{ selection: string | null; currentParagraph: string | null }>({
  selection: null,
  currentParagraph: null,
})

// 切换章节时刷新版本号（复用现有版本 store）
watch([() => props.projectId, currentUnitPosition], () => {
  // P1：切换章节/项目时清空上一章的选区上下文（无论目标单元是否存在），
  // 防止"修改选中部分"把第一章的 selection/currentParagraph 带入第二章。
  editorSelection.value = { selection: null, currentParagraph: null }
  const unit = currentWritingUnit.value
  if (!unit) return
  if (isNovel.value) {
    void versionStore.loadVersions(props.projectId, unitPosition(unit))
  } else {
    void revisionStore.loadRevisions(props.projectId, unit.id)
  }
}, { immediate: true })

// 左侧资料库计数（纯 store 读取，仅展示）
const libraryCounts = computed(() => ({
  characters: characterStore.charactersForProject(props.projectId).length,
  worldEntries: worldEntryStore.entriesForProject(props.projectId).length,
  outlines: outlineStore.entriesForProject(props.projectId).length,
  threads: hiddenThreadStore.threadsForProject(props.projectId).length,
}))

// ─── 左侧资料库弹窗：点击入口打开 ProjectLibraryPanel（完整管理） ───

const activeLibrary = ref<'characters' | 'world' | 'outlines' | 'threads' | null>(null)

function openLibrary(kind: NonNullable<typeof activeLibrary.value>): void {
  activeLibrary.value = kind
}

function closeLibrary(): void {
  activeLibrary.value = null
}

// ─── 中栏 CreationSurface ───

const surface = ref<'conversation' | 'editor'>('conversation')

const conversationContext = computed<ConversationContext>(() => ({
  projectId: props.projectId,
  mode: props.mode,
  chapterId: currentWritingUnit.value?.id ?? null,
  chapterNum: currentUnitPosition.value ?? null,
  chapterTitle: currentWritingUnit.value?.title?.trim() || null,
  currentVersionId: conversationStore.baseVersionId,
  currentDraft: currentWritingUnit.value?.draft ?? '',
  selection: editorSelection.value.selection,
  currentParagraph: editorSelection.value.currentParagraph,
}))

// ─── 右侧快捷能力与对话事件 → 转发到 AgentPanel 执行宿主 ───

const executionHostRef = ref<InstanceType<typeof AgentPanel> | null>(null)

function handleConversationConfirm(payload: {
  messageId: string
  taskCard: ConversationTaskCard
  overrides?: Record<string, unknown>
  /** P1-3：重新生成时基于的最新正式版本（覆盖任务卡旧 base_version_id） */
  baseVersionId?: string | null
  /** P1-3：重新生成时必须使用新幂等键，否则后端幂等短路返回首次结果 */
  idempotencyKey?: string
}): void {
  executionHostRef.value?.handleConversationConfirm(payload)
}

function handleConversationAcceptCandidate(payload: { content: string; executionId: string | null }): void {
  executionHostRef.value?.handleConversationAcceptCandidate(payload)
}

function handleConversationRevertSession(): void {
  executionHostRef.value?.handleConversationRevertSession()
}

function handleQuickAction(key: 'generate' | 'turn' | 'enhance' | 'summarize'): void {
  const host = executionHostRef.value
  if (!host) return
  if (key === 'generate') host.handleGenerate()
  else host.handleQuickAction(key)
}

function handleCancelGeneration(): void {
  executionHostRef.value?.cancelStream()
}

defineExpose({ cancelGeneration: handleCancelGeneration })

// ─── 新增单元（表单数据本地收集，创建动作上抛 Workspace）───

const showNewUnitForm = ref(false)
const newUnitTitle = ref('')
const newUnitPosition = ref(1)
const newUnitSummary = ref('')

function openNewUnitForm(): void {
  newUnitTitle.value = ''
  newUnitSummary.value = ''
  newUnitPosition.value = isNovel.value
    ? chapterStore.nextChapterNum(props.projectId)
    : documentStore.nextDocumentPosition(props.projectId)
  showNewUnitForm.value = true
}

function submitNewUnit(): void {
  const title = newUnitTitle.value.trim()
  if (!title) return
  const position = newUnitPosition.value
  emit('createUnit', { title, position, summary: newUnitSummary.value.trim() || undefined })
  showNewUnitForm.value = false
}
</script>

<template>
  <div class="ccw-window">
    <div class="ccw-layout">
      <!-- 左：章节栏 -->
      <aside class="ccw-rail" aria-label="章节列表">
        <div class="ccw-rail-title">
          <span>{{ unitLabel }}</span>
          <button class="ccw-add-chapter" type="button" :aria-label="`新增${unitLabel}`" @click="openNewUnitForm">＋</button>
        </div>

        <div v-if="showNewUnitForm" class="ccw-new-unit">
          <label>标题 <span class="ccw-required">*</span></label>
          <input v-model="newUnitTitle" type="text" class="ccw-input" :placeholder="`输入${unitLabel}标题`" />
          <label>{{ unitLabel }}序号</label>
          <input v-model.number="newUnitPosition" type="number" min="1" class="ccw-input" />
          <label>摘要</label>
          <textarea v-model="newUnitSummary" rows="2" class="ccw-input" placeholder="可选"></textarea>
          <div class="ccw-new-unit-actions">
            <button type="button" class="ccw-btn ccw-btn-primary" :disabled="!newUnitTitle.trim()" @click="submitNewUnit">确定</button>
            <button type="button" class="ccw-btn" @click="showNewUnitForm = false">取消</button>
          </div>
        </div>

        <div class="ccw-rail-list">
          <button
            v-for="unit in writingUnits"
            :key="unit.id"
            type="button"
            class="ccw-chapter"
            :class="{ active: unitPosition(unit) === currentUnitPosition }"
            @click="selectUnit(unitPosition(unit))"
          >
            <span class="ccw-chapter-num">{{ String(unitDisplayNum(unit)).padStart(2, '0') }}</span>
            <span class="ccw-chapter-copy">
              <span class="ccw-chapter-title">{{ unitDisplayTitle(unit) }}</span>
              <span class="ccw-chapter-meta">
                <span>{{ unitStatusLabel(unit) }}</span>
                <span v-if="isNovel" class="ccw-chapter-action" @click.stop="emit('openConfig', unit)">资料</span>
                <span v-if="isNovel" class="ccw-chapter-action" @click.stop="emit('extractMemory', unit)">提炼</span>
              </span>
            </span>
          </button>
        </div>

        <div class="ccw-rail-links">
          <button type="button" class="ccw-rail-link" @click="openLibrary('characters')"><span>人物档案</span><span>{{ libraryCounts.characters }}</span></button>
          <button type="button" class="ccw-rail-link" @click="openLibrary('world')"><span>世界设定</span><span>{{ libraryCounts.worldEntries }}</span></button>
          <button type="button" class="ccw-rail-link" @click="openLibrary('outlines')"><span>故事大纲</span><span>{{ libraryCounts.outlines }}</span></button>
          <button type="button" class="ccw-rail-link" @click="openLibrary('threads')"><span>创作记忆</span><span>{{ libraryCounts.threads }}</span></button>
        </div>
      </aside>

      <!-- 中：对话创作 / 正文编辑 -->
      <main class="ccw-main">
        <div class="ccw-main-head">
          <div class="ccw-main-title">
            <strong>{{ mainTitle }}</strong>
            <span>{{ mainSub }}</span>
          </div>
          <div class="ccw-view-switch" role="tablist" aria-label="创作模式">
            <button
              type="button"
              role="tab"
              :aria-selected="surface === 'conversation'"
              :class="{ active: surface === 'conversation' }"
              @click="surface = 'conversation'"
            >对话创作</button>
            <button
              type="button"
              role="tab"
              :aria-selected="surface === 'editor'"
              :class="{ active: surface === 'editor' }"
              @click="surface = 'editor'"
            >正文编辑</button>
          </div>
        </div>

        <section v-show="surface === 'conversation'" class="ccw-view ccw-view-conversation">
          <div class="ccw-thread">
            <ConversationAssistant
              :context="conversationContext"
              @confirm="handleConversationConfirm"
              @accept-candidate="handleConversationAcceptCandidate"
              @revert-session="handleConversationRevertSession"
            />
          </div>
        </section>

        <section v-show="surface === 'editor'" class="ccw-view ccw-view-editor">
          <WritingEditor
            v-if="currentWritingUnit"
            :project-id="projectId"
            :mode="mode"
            @selection-change="editorSelection = $event"
          />
          <div v-else class="ccw-editor-empty">选择一个{{ unitLabel }}开始写作</div>
        </section>
      </main>

      <!-- 右：任务上下文 / 快捷能力 / 执行进度 -->
      <CreativeContextPanel
        :project-id="projectId"
        :mode="mode"
        :latest-version-number="latestVersionNumber"
        @quick-action="handleQuickAction"
        @cancel="handleCancelGeneration"
      />
    </div>

    <!-- 隐藏的执行宿主：唯一持有现有 SSE / 任务卡 / 人工审核闭环。
         容器保持 0 尺寸且不创建 containing block（无 transform/filter），
         因此宿主内 position:fixed 的审核弹窗 / 任务卡确认仍按视口正常弹出。 -->
    <div class="ccw-execution-host" aria-hidden="true">
      <AgentPanel ref="executionHostRef" :project-id="projectId" :mode="mode" hide-workflow />
    </div>

    <!-- 左侧资料库弹窗：完整管理（人物 / 世界设定 / 大纲 / 创作记忆=暗线+写作记忆） -->
    <ProjectLibraryPanel
      v-if="activeLibrary"
      :project-id="projectId"
      :project-mode="mode"
      :active="activeLibrary"
      @close="closeLibrary"
    />
  </div>
</template>

<style scoped>
.ccw-window {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  border: 1px solid #252b38;
  border-radius: 20px;
  background:
    radial-gradient(circle at 70% -12%, rgba(87, 107, 255, 0.22), transparent 32%),
    radial-gradient(circle at 16% 18%, rgba(132, 72, 220, 0.1), transparent 28%),
    #0a0d13;
  box-shadow: 0 34px 90px rgba(0, 0, 0, 0.46);
}

.ccw-layout {
  display: grid;
  grid-template-columns: 220px minmax(470px, 1fr) 300px;
  flex: 1;
  min-height: 0;
}

/* ─── 左：章节栏 ─── */

.ccw-rail {
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  padding: 17px 12px;
  border-right: 1px solid #242a35;
  background: rgba(10, 13, 19, 0.83);
  overscroll-behavior: contain;
}
.ccw-rail-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 8px 12px;
  color: #7e899b;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.12em;
}
.ccw-add-chapter {
  width: 27px;
  height: 27px;
  border: 1px solid #303848;
  border-radius: 8px;
  color: #8d99ae;
  background: #141922;
  font-size: 12px;
  cursor: pointer;
}
.ccw-add-chapter:hover {
  border-color: #566dde;
  color: #eef1ff;
}
.ccw-new-unit {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 10px;
  padding: 11px;
  border: 1px solid #2b3341;
  border-radius: 12px;
  background: #12161e;
}
.ccw-new-unit label {
  color: #68758a;
  font-size: 9px;
  font-weight: 650;
}
.ccw-required {
  color: #d998a2;
}
.ccw-input {
  width: 100%;
  padding: 6px 8px;
  border: 1px solid #303a49;
  border-radius: 8px;
  background: #171c25;
  color: #e8edf5;
  font-size: 10px;
  outline: none;
}
.ccw-input:focus {
  border-color: #566dde;
}
.ccw-new-unit-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
  margin-top: 4px;
}
.ccw-btn {
  min-height: 26px;
  padding: 0 10px;
  border: 1px solid #394353;
  border-radius: 8px;
  background: #191e27;
  color: #b7c0cf;
  font-size: 10px;
  cursor: pointer;
}
.ccw-btn:hover:not(:disabled) {
  border-color: #566dde;
  color: #eef1ff;
}
.ccw-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.ccw-btn-primary {
  border-color: #7388ff;
  color: #fff;
  background: linear-gradient(135deg, #6d83ff, #5568eb);
}
.ccw-rail-list {
  display: flex;
  flex-direction: column;
}
.ccw-chapter {
  width: 100%;
  display: grid;
  grid-template-columns: 25px minmax(0, 1fr);
  gap: 8px;
  padding: 12px 10px;
  border: 1px solid transparent;
  border-radius: 13px;
  color: #8792a4;
  background: transparent;
  text-align: left;
  cursor: pointer;
  transition: border-color 120ms ease, background 120ms ease, color 120ms ease;
}
.ccw-chapter + .ccw-chapter {
  margin-top: 3px;
}
.ccw-chapter:hover {
  border-color: #303848;
  background: rgba(255, 255, 255, 0.025);
}
.ccw-chapter.active {
  border-color: rgba(106, 131, 255, 0.44);
  color: #f3f5fb;
  background: linear-gradient(100deg, rgba(71, 91, 196, 0.26), rgba(27, 33, 47, 0.66));
  box-shadow: inset 3px 0 0 #7388ff, 0 9px 22px rgba(0, 0, 0, 0.14);
}
.ccw-chapter-num {
  padding-top: 1px;
  color: #7388ff;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.ccw-chapter-copy {
  min-width: 0;
  display: block;
  font-size: 12px;
}
.ccw-chapter-title {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ccw-chapter-meta {
  display: flex;
  gap: 5px;
  margin-top: 6px;
}
.ccw-chapter-meta > span {
  padding: 3px 5px;
  border-radius: 5px;
  background: rgba(255, 255, 255, 0.035);
  color: #5f6a7e;
  font-size: 9px;
}
.ccw-chapter-action {
  cursor: pointer;
  transition: color 120ms ease, background 120ms ease;
}
.ccw-chapter-action:hover {
  color: #aab8ff;
  background: rgba(106, 130, 255, 0.14);
}
.ccw-rail-links {
  margin-top: 20px;
  padding-top: 14px;
  border-top: 1px solid #222934;
}
.ccw-rail-link {
  width: 100%;
  display: flex;
  justify-content: space-between;
  padding: 9px 10px;
  border: 0;
  color: #7d889b;
  background: transparent;
  text-align: left;
  font-size: 10px;
  cursor: pointer;
}
.ccw-rail-link:hover {
  color: #cbd3e1;
}

/* ─── 中：CreationSurface ─── */

.ccw-main {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: rgba(15, 18, 26, 0.72);
}
.ccw-main-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  min-height: 66px;
  padding: 0 22px;
  border-bottom: 1px solid #252c38;
  flex-shrink: 0;
}
.ccw-main-title {
  min-width: 0;
}
.ccw-main-title strong {
  display: block;
  overflow: hidden;
  font-size: 14px;
  font-weight: 750;
  color: #eef1f7;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ccw-main-title span {
  display: block;
  margin-top: 4px;
  color: #69758a;
  font-size: 9px;
}
.ccw-view-switch {
  display: flex;
  flex: 0 0 auto;
  gap: 4px;
  padding: 4px;
  border: 1px solid #2b3341;
  border-radius: 11px;
  background: #11151c;
}
.ccw-view-switch button {
  min-height: 28px;
  padding: 7px 12px;
  border: 0;
  border-radius: 7px;
  color: #778296;
  background: transparent;
  font-size: 10px;
  font-weight: 650;
  cursor: pointer;
  transition: color 120ms ease, background 120ms ease, box-shadow 120ms ease;
}
.ccw-view-switch button.active {
  color: #f5f7fc;
  background: #272e3b;
  box-shadow: 0 4px 10px rgba(0, 0, 0, 0.25);
}
.ccw-view {
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1;
}
.ccw-view-conversation {
  min-height: 0;
}
.ccw-thread {
  flex: 1;
  min-height: 0;
  display: flex;
  padding: 20px clamp(20px, 4vw, 46px) 14px;
}
.ccw-thread :deep(.conversation-assistant) {
  flex: 1;
  min-height: 0;
  width: 100%;
}
.ccw-thread :deep(.ca-messages) {
  flex: 1;
  max-height: none;
  min-height: 0;
}
.ccw-thread :deep(.ca-message) {
  max-width: 700px;
}
.ccw-thread :deep(.ca-message.user) {
  margin-left: auto;
}
.ccw-view-editor {
  min-height: 0;
}
.ccw-editor-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 1;
  color: #69758a;
  font-size: 12px;
}

/* ─── 隐藏执行宿主 ─── */

.ccw-execution-host {
  position: fixed;
  top: 0;
  left: 0;
  width: 0;
  height: 0;
  overflow: hidden;
}


@media (max-width: 1080px) {
  .ccw-layout {
    grid-template-columns: 180px minmax(420px, 1fr);
    grid-template-rows: minmax(0, 1fr) auto;
  }
  .ccw-layout :deep(.ccw-context) {
    grid-column: 1 / -1;
    border-top: 1px solid #242a35;
    border-left: 0;
  }
}
</style>
