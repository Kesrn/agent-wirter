/** 环境配置 */
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api'

// ─── Auth ───

export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  access_token: string
  token_type: string
  user: {
    id: string
    username: string
    display_name: string
  }
}

export interface MeResponse {
  username: string
  display_name?: string
}

export interface RegisterRequest {
  username: string
  password: string
  email?: string
}

export interface RegisterResponse {
  id: string
  username: string
}

export interface ProjectImageUploadResponse {
  url: string
  filename: string
  content_type: string
  size: number
}

// ─── Shared domain types (used by UI, stores, mock) ───

/** Agent 角色类型 */
export type RoleType = 'writer' | 'critic' | 'editor' | 'researcher' | 'custom'

/** Agent 在工作流中的位置 */
export type WorkflowPosition =
  | 'pre_writer'
  | 'post_writer'
  | 'replace_writer'
  | 'pre_critic'
  | 'replace_critic'
  | 'post_critic'
  | 'standalone'

/** Agent 上下文范围 */
export interface ContextScope {
  include_world: boolean
  include_characters: boolean
  include_outline: boolean
  /** Number of previous chapters to include (0 = none) */
  include_previous_chapters: number
}

/** Agent 触发方式 */
export type TriggerMode = 'manual' | 'auto_on_draft' | 'auto_on_save' | 'auto_on_chapter_complete'

/** 专家角色 (UI / store) — mapped from ApiExpert via apiExpertToExpert */
export interface Expert {
  id: string
  /** Short role identifier, derived from role_type for UI convenience */
  role: string
  name: string
  role_type: RoleType
  skill_dir?: string | null
  description: string
  system_prompt: string
  temperature: number
  max_tokens: number
  workflow_position: WorkflowPosition
  context_scope: ContextScope
  trigger: TriggerMode
  is_builtin: boolean
  is_enabled: boolean
  color: string
  /** Expert System v2 */
  expert_key?: string | null
  version: number
  deprecated: boolean
}

/** 项目创作模式 */
export type ProjectMode = 'novel' | 'article'

/** 项目 (UI / store) */
export interface Project {
  id: string
  title: string
  genre: string
  style: string
  overall_outline: string
  status: string
  mode: ProjectMode
  description: string
  target_words: number
  created_at: string
  updated_at: string
}

export type DraftStatus = 'draft' | 'reviewing' | 'revision' | 'final'

/** 章节 (UI / store) — mapped from ApiChapter via apiChapterToChapter */
export interface Chapter {
  id: string
  project_id: string
  /** Mapped from ApiChapter.sequence_number */
  chapter_num: number
  title: string
  /** Mapped from ApiChapter.outline (null → '') */
  summary: string
  /** Current draft content, mapped from ApiChapter.content (null → '') */
  draft: string
  /** Final approved text — not returned by backend; populated client-side when status is 'final' */
  final_text: string
  /** Mapped via CHAPTER_STATUS_MAP from backend status values */
  status: DraftStatus
  review_comment_ids: string[]
  review_round: number
}

/** 稿件 (UI / store) — mapped from ApiDocument via apiDocumentToDocumentUnit */
export interface DocumentUnit {
  id: string
  project_id: string
  /** Mapped from ApiDocument.position */
  position: number
  title: string
  /** Current draft content, mapped from ApiDocument.content (null -> '') */
  draft: string
  /** Final approved text — not returned by backend; populated client-side when status is 'final' */
  final_text: string
  /** Mapped via DRAFT_STATUS_MAP from backend status values */
  status: DraftStatus
  word_count: number
  created_at: string
  updated_at: string
}

export type WritingUnit = Chapter | DocumentUnit

/** 审核意见 */
export interface ReviewComment {
  id: string
  chapter_id: string
  expert_id: string
  comment: string
  severity: 'info' | 'warning' | 'critical'
  resolved?: boolean
  source_type?: string
  created_at?: string
  updated_at?: string
  metadata?: Record<string, unknown> | null
}

export type ReviewNoteSeverity = 'info' | 'warning' | 'critical'

/** Backend chapter_review_notes response */
export interface ChapterReviewNote {
  id: string
  project_id: string
  chapter_sequence_number: number
  source_type: string
  severity: ReviewNoteSeverity
  content: string
  resolved: boolean
  metadata: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

/** 世界观条目 (UI / store) — mapped from ApiWorldEntry */
export interface WorldEntry {
  id: string
  project_id: string
  title: string
  category: string
  scope_type: 'global' | 'chapter'
  content: string
  rules: Record<string, unknown> | null
  confidence: 'low' | 'medium' | 'high'
  created_at: string
  updated_at: string
}

/** 角色 (UI / store) — mapped from ApiCharacter via apiCharacterToCharacter */
export interface Character {
  id: string
  project_id: string
  name: string
  role_type: 'protagonist' | 'antagonist' | 'supporting' | 'minor'
  scope_type: 'core' | 'recurring' | 'chapter' | 'cameo'
  profile: string
  faction: string
  appearance_count: number
  metadata: Record<string, unknown> | null
}

/** 角色关系 */
export interface CharacterRelation {
  id: string
  project_id: string
  source_character_id: string
  target_character_id: string
  description: string
}

export type CharacterAppearanceType = 'appeared' | 'mentioned' | 'absent'

export interface CharacterEvent {
  id: string
  project_id: string
  character_id: string
  chapter_sequence_number: number
  appearance_type: CharacterAppearanceType
  appeared: boolean
  event_summary: string | null
  actions: string[] | null
  state_change: string | null
  location: string | null
  emotion: string | null
  importance: number
  created_at: string
  updated_at: string
}

/** 大纲条目 (UI / store) — mapped from ApiOutline via apiOutlineToOutlineItem */
export interface OutlineItem {
  id: string
  project_id: string
  chapter_num: number
  title: string
  summary: string
  turning_point: string | null
  hidden_thread_ids: string[]
  story_arc_id: string | null
  arc_position: string | null
}

/** 长线结构 (UI / store) — mapped from ApiStoryArc via apiStoryArcToStoryArc */
export interface StoryArc {
  id: string
  project_id: string
  parent_arc_id: string | null
  arc_type: 'VOLUME' | 'ACT' | 'ARC'
  name: string
  summary: string
  goal: string
  main_conflict: string
  start_chapter: number | null
  end_chapter: number | null
  order_index: number
  status: string
}

/** 暗线 (UI / store) — mapped from ApiHiddenThread via apiHiddenThreadToHiddenThread */
export interface HiddenThread {
  id: string
  project_id: string
  name: string
  description: string
  chapter_nums: number[]
  created_at: string
  updated_at: string
}

/** Agent 工作流步骤 */
export interface WorkflowStep {
  id: string
  name: string
  status: 'pending' | 'running' | 'success' | 'error' | 'cancelled'
  expert_id: string
  output?: string
  duration_ms?: number
}

/** 采纳决策 */
export type ApprovalDecision = 'accept' | 'accept_with_mods' | 'reject'

/** 常量限制 */
export const MAX_SYSTEM_PROMPT_LENGTH = 2000
export const MAX_TOKENS_LIMIT = 8192

// ─── API response types (mirror backend schemas/api.py) ───

export interface ApiProject {
  id: string
  title: string
  description: string | null
  overall_outline: string | null
  genre: string | null
  style: string | null
  target_words: number
  status: string
  mode: ProjectMode
  created_at: string
  updated_at: string
}

export interface ApiChapter {
  id: string
  project_id: string
  title: string
  content: string | null
  outline: string | null
  sequence_number: number
  word_count: number
  status: string
  created_at: string
  updated_at: string
}

export interface ApiDocument {
  id: string
  project_id: string
  title: string
  content: string | null
  position: number
  word_count: number
  status: string
  created_at: string
  updated_at: string
}

export interface ApiExpert {
  id: string
  project_id: string
  name: string
  description: string
  role_type: RoleType
  skill_dir?: string | null
  system_prompt: string
  temperature: number
  max_tokens: number
  workflow_position: WorkflowPosition
  context_scope: ContextScope
  trigger: TriggerMode
  is_builtin: boolean
  is_enabled: boolean
  color: string
  created_at: string
  /** Expert System v2 */
  expert_key?: string | null
  version: number
  deprecated: boolean
}

/** Backend WorldEntryResponse */
export interface ApiWorldEntry {
  id: string
  project_id: string
  title: string
  category: string
  scope_type: 'global' | 'chapter'
  content: string
  rules: Record<string, unknown> | null
  confidence: string
  created_at: string
  updated_at: string
}

/** Backend CharacterResponse */
export interface ApiCharacter {
  id: string
  project_id: string
  name: string
  role_type: string
  scope_type: 'core' | 'recurring' | 'chapter' | 'cameo'
  profile: string
  faction: string
  appearance_count: number
  metadata: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

/** Backend CharacterRelationResponse */
export interface ApiCharacterRelation {
  id: string
  project_id: string
  source_character_id: string
  target_character_id: string
  description: string
  created_at: string
  updated_at: string
}

export interface ApiCharacterEvent {
  id: string
  project_id: string
  character_id: string
  chapter_sequence_number: number
  appearance_type: CharacterAppearanceType
  appeared: boolean
  event_summary: string | null
  actions: string[] | null
  state_change: string | null
  location: string | null
  emotion: string | null
  importance: number
  created_at: string
  updated_at: string
}

/** Backend OutlineResponse */
export interface ApiOutline {
  id: string
  project_id: string
  sequence_number: number
  title: string
  summary: string | null
  turning_point: string | null
  hidden_thread_ids: string[]
  story_arc_id: string | null
  arc_position: string | null
  created_at: string
  updated_at: string
}

/** Backend StoryArcResponse */
export interface ApiStoryArc {
  id: string
  project_id: string
  parent_arc_id: string | null
  arc_type: string
  name: string
  summary: string | null
  goal: string | null
  main_conflict: string | null
  start_chapter: number | null
  end_chapter: number | null
  order_index: number
  status: string
  metadata: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

/** Backend HiddenThreadResponse */
export interface ApiHiddenThread {
  id: string
  project_id: string
  name: string
  description: string | null
  chapter_nums: number[]
  created_at: string
  updated_at: string
}

// ─── API request payloads ───

export interface ProjectCreatePayload {
  title: string
  description?: string
  overall_outline?: string
  genre?: string
  style?: string
  target_words?: number
  mode?: ProjectMode
}

export interface TxtImportChapterPreview {
  sequence_number: number
  title: string
  word_count: number
  preview: string
}

export interface TxtImportMeta {
  filename: string | null
  encoding: string | null
  had_bom: boolean | null
  bytes: number | null
  chars: number
  word_count: number
  chapter_count: number
  chapters: TxtImportChapterPreview[]
}

export interface TxtImportResponse {
  project: ApiProject
  chapters: TxtImportChapterPreview[]
  import_meta: TxtImportMeta
}

export interface ChapterCreatePayload {
  title: string
  outline?: string
  sequence_number?: number
}

export interface ChapterReviewNoteCreatePayload {
  source_type: string
  severity?: ReviewNoteSeverity
  content: string
  resolved?: boolean
  metadata?: Record<string, unknown> | null
}

export interface ChapterReviewNoteUpdatePayload {
  source_type?: string
  severity?: ReviewNoteSeverity
  content?: string
  resolved?: boolean
  metadata?: Record<string, unknown> | null
}

export interface ProjectUpdatePayload {
  title?: string
  description?: string | null
  overall_outline?: string | null
  genre?: string | null
  style?: string | null
  target_words?: number
  status?: string
}

export interface DocumentCreatePayload {
  title: string
  content?: string | null
  position?: number | null
}

export interface DocumentUpdatePayload {
  title?: string
  content?: string | null
  status?: DraftStatus | 'approved'
}

export interface ExpertUpdatePayload {
  is_enabled?: boolean
  name?: string
  description?: string
  system_prompt?: string
  temperature?: number
  max_tokens?: number
  workflow_position?: string
  context_scope?: Record<string, unknown> | null
}

export interface ExpertCreatePayload {
  name: string
  description: string
  role_type: RoleType
  skill_dir?: string | null
  system_prompt: string
  temperature: number
  max_tokens: number
  workflow_position: WorkflowPosition
  context_scope: ContextScope
  trigger: TriggerMode
  color: string
}

export interface WorldEntryCreatePayload {
  title: string
  category?: string
  scope_type?: 'global' | 'chapter'
  content: string
  rules?: Record<string, unknown> | null
  confidence?: 'low' | 'medium' | 'high'
}

export interface WorldEntryUpdatePayload {
  title?: string
  category?: string
  scope_type?: 'global' | 'chapter'
  content?: string
  rules?: Record<string, unknown> | null
  confidence?: 'low' | 'medium' | 'high'
}

export interface CharacterCreatePayload {
  name: string
  role_type: 'protagonist' | 'antagonist' | 'supporting' | 'minor'
  scope_type?: 'core' | 'recurring' | 'chapter' | 'cameo'
  profile?: string
  faction?: string
  appearance_count?: number
  metadata?: Record<string, unknown> | null
}

export interface CharacterUpdatePayload {
  name?: string
  role_type?: 'protagonist' | 'antagonist' | 'supporting' | 'minor'
  scope_type?: 'core' | 'recurring' | 'chapter' | 'cameo'
  profile?: string
  faction?: string
  metadata?: Record<string, unknown> | null
}

export interface CharacterMergePayload {
  target_character_id: string
}

export interface CharacterRelationCreatePayload {
  source_character_id: string
  target_character_id: string
  description: string
}

export interface CharacterRelationUpdatePayload {
  source_character_id?: string
  target_character_id?: string
  description?: string
}

export interface CharacterEventUpsertPayload {
  appearance_type?: CharacterAppearanceType
  event_summary?: string | null
  actions?: string[] | null
  state_change?: string | null
  location?: string | null
  emotion?: string | null
  importance?: number
}

export interface OutlineCreatePayload {
  sequence_number: number
  title: string
  summary?: string
  turning_point?: string
  story_arc_id?: string | null
  arc_position?: string | null
}

export interface OutlineUpdatePayload {
  sequence_number?: number
  title?: string
  summary?: string
  turning_point?: string
  story_arc_id?: string | null
  arc_position?: string | null
}

export interface StoryArcCreatePayload {
  arc_type: string
  name: string
  summary?: string
  goal?: string
  main_conflict?: string
  parent_arc_id?: string | null
  start_chapter?: number | null
  end_chapter?: number | null
  order_index?: number
  status?: string
}

export interface StoryArcUpdatePayload {
  arc_type?: string
  name?: string
  summary?: string
  goal?: string
  main_conflict?: string
  parent_arc_id?: string | null
  start_chapter?: number | null
  end_chapter?: number | null
  order_index?: number
  status?: string
}

export interface HiddenThreadCreatePayload {
  name: string
  description?: string
  chapter_nums?: number[]
}

export interface HiddenThreadUpdatePayload {
  name?: string
  description?: string
  chapter_nums?: number[]
}

export type StructureExtractMode = 'preview' | 'apply'
export type StructureConfidence = 'low' | 'medium' | 'high'
export type StructureRoleType = 'protagonist' | 'antagonist' | 'supporting' | 'minor'

export interface ExtractedOutline {
  sequence_number: number
  title: string
  summary?: string | null
  turning_point?: string | null
}

export interface ExtractedCharacter {
  name: string
  role_type: StructureRoleType
  scope_type?: 'core' | 'recurring' | 'chapter' | 'cameo'
  profile?: string | null
  faction?: string | null
  appearance_count?: number
  metadata?: Record<string, unknown> | null
}

export interface ExtractedWorldEntry {
  title: string
  category?: string
  scope_type?: 'global' | 'chapter'
  content: string
  rules?: Record<string, unknown> | null
  confidence?: StructureConfidence
}

export interface ExtractedHiddenThread {
  name: string
  description?: string | null
  chapter_nums?: number[] | null
}

export interface ExtractedCharacterRelation {
  source: string
  target: string
  description: string
}

export interface ExtractedCharacterEvent {
  character_name: string
  sequence_number: number
  appearance_type?: CharacterAppearanceType
  event_summary?: string | null
  actions?: string[] | null
  state_change?: string | null
  location?: string | null
  emotion?: string | null
  importance?: number
}

export interface StructureExtractPayload {
  outlines?: ExtractedOutline[]
  characters?: ExtractedCharacter[]
  world_entries?: ExtractedWorldEntry[]
  hidden_threads?: ExtractedHiddenThread[]
  character_relations?: ExtractedCharacterRelation[]
  character_events?: ExtractedCharacterEvent[]
}

export interface ChapterStructureExtractRequest {
  targets?: string[]
  mode?: StructureExtractMode
  merge_strategy?: 'upsert' | 'create_only'
  include_existing_context?: boolean
  extraction?: StructureExtractPayload
}

export interface ChapterStructureExtractResponse {
  extraction: StructureExtractPayload
  preview: Record<string, unknown>
  applied?: {
    counts: Record<string, number>
  } | null
}

export type GenerateMode = 'continue' | 'full_pipeline' | 'enhance' | 'summarize'

export interface GenerateRequest {
  chapter_id?: string
  document_id?: string
  chapter_num?: number
  mode?: GenerateMode
  expert_id?: string
  selected_outline_ids?: string[]
  selected_character_ids?: string[]
  selected_world_entry_ids?: string[]
  selected_hidden_thread_ids?: string[]
  include_knowledge_sources?: boolean
  target_words?: number
  selected_direction?: string
  direction_option_id?: string
  enhance_direction?: string
  turn_direction?: string
  user_note?: string
  content_type?: string
  platform?: string
  audience?: string
  content_goal?: string
  tone?: string
  key_points?: string
  planning_review?: boolean
}

export interface ArticleGenerateParams {
  content_type: string
  platform: string
  audience: string
  content_goal: string
  tone: string
  key_points: string
  target_words: number
}

// ─── SSE types ───

/** SSE event types emitted by the backend generate/test endpoints */
export type SSEEventType = 'progress' | 'agent_start' | 'agent_output' | 'agent_done' | 'writer_output' | 'content_output' | 'editor_output' | 'architect_output' | 'critic_output' | 'consistency_check' | 'enhance_directions' | 'turn_suggestions' | 'content_suggestions' | 'article_review' | 'revision_suggestions' | 'skill_pack' | 'generation_record' | 'clarification_required' | 'task_card_review_required' | 'done' | 'error' | 'run_created' | 'run_status' | 'run_step'

/** SSE envelope parsed from the backend stream */
export interface SSEEnvelope {
  event: SSEEventType
  data: Record<string, unknown> | string
}

/** Payload for agent_start SSE event */
export interface AgentStartPayload {
  agent: string
  step: string
}

/** Payload for agent_output SSE event */
export interface AgentOutputPayload {
  token: string
}

/** Payload for agent_done SSE event */
export interface AgentDonePayload {
  agent: string
  step: string
}

/** Payload for progress SSE event */
export interface ProgressPayload {
  message: string
  mode?: string
  chapter_num?: number
}

/** Payload for done SSE event */
export interface DonePayload {
  message: string
}

/** Payload for error SSE event */
export interface ErrorPayload {
  message: string
}

/** Payload for writer_output SSE event (full_pipeline mode) */
export interface WriterOutputPayload {
  content?: string
  token?: string
}

/** Payload for critic_output SSE event (full_pipeline mode) */
export interface CriticOutputPayload {
  critiques?: string[]
}

/** Payload for consistency_check SSE event (full_pipeline mode) */
export interface GuardrailIssue {
  type?: string
  description?: string
  severity?: 'info' | 'low' | 'medium' | 'high' | string
}

export interface GuardrailResult {
  issues?: GuardrailIssue[]
  summary?: string
  overall_severity?: 'info' | 'low' | 'medium' | 'high' | string
  parse_error?: boolean
  raw?: string
}

export interface ConsistencyCheckPayload {
  report?: string
  guardrail_result?: GuardrailResult
}

// --- 写作记忆 staging ---
export type MemoryStagingStatus = 'GENERATED' | 'CONFIRMED' | 'REJECTED'
export type MemoryType = 'CHARACTER' | 'WORLD_RULE' | 'PLOT_FACT' | 'EVENT' | 'FORESHADOWING'

export interface ApiWritingMemoryStaging {
  id: string
  project_id: string
  run_id: string | null
  chapter_id: string | null
  chapter_version_id: string | null
  chapter_sequence_number: number | null
  memory_type: MemoryType | string
  title: string
  payload: Record<string, unknown>
  evidence: string | null
  status: MemoryStagingStatus | string
  confirmed_target_type: string | null
  confirmed_target_id: string | null
  reviewed_by: string | null
  reviewed_at: string | null
  created_at: string
}

/** Payload for enhance_directions SSE event */
export interface EnhanceDirectionsPayload {
  directions: string[]
}

/** Payload for turn_suggestions SSE event */
export interface TurnSuggestionsPayload {
  suggestions: string[]
}

/** Payload for article_review SSE event */
export interface ArticleReviewPayload {
  review_type: 'structure' | 'audience' | 'platform' | 'risk' | string
  result: Record<string, unknown>
}

/** Payload for revision_suggestions SSE event */
export interface RevisionSuggestionsPayload {
  directions: string[]
  revision_count: number
  max_revisions: number
}

/** Payload for skill_pack SSE event — 当前专家使用了哪个 skill */
export interface SkillPackPayload {
  expert: string
  skill: string
  skill_dir: string
  planner?: string
  planner_reason?: string
  sources?: Array<Record<string, unknown>>
  warnings?: string[]
  token_estimate?: number
  truncated?: boolean
  has_content?: boolean
}

/** Payload for generation_record SSE event */
export interface GenerationRecordPayload {
  id: string
  status: GenerationRecordStatus
  langfuse_trace_id?: string | null
}

// ─── Clarification Loop (生成前澄清) ───

/** Clarification option */
export interface ClarificationOption {
  value: string
  label: string
  description?: string
}

/** Clarification question */
export interface ClarificationQuestion {
  id: string
  type: 'single_choice' | 'multi_choice' | 'free_text' | 'number'
  question: string
  options?: ClarificationOption[]
  required?: boolean
  reason?: string
}

/** Clarification 状态响应 (GET /ai-runs/{run_id}/clarification) */
export interface ClarificationState {
  run_id: string
  interrupt_id: string | null
  status: string
  round: number
  max_rounds: number
  questions: ClarificationQuestion[]
  assumptions_if_skipped: string[]
  existing_answers: Record<string, string>
  clarification_summary: string
  resolved: boolean
}

/** Clarification 提交请求 (POST /ai-runs/{run_id}/clarification-answers) */
export interface ClarificationAnswerRequest {
  action: 'submit' | 'skip'
  answers: Record<string, string>
}

/** Payload for clarification_required SSE event */
export interface ClarificationRequiredPayload {
  run_id: string
  interrupt_id: string | null
  round: number
  max_rounds: number
  questions: ClarificationQuestion[]
  assumptions_if_skipped: string[]
}

// ─── L-1: Task Card Review ───

export interface TaskCardScene {
  title?: string
  location?: string
  characters?: string[]
  scene_goal?: string
  conflict?: string
  must_include?: string[]
  must_not_include?: string[]
  word_budget?: number
}

export interface TaskCardInformationRules {
  may_reveal?: string[]
  hint_only?: string[]
  forbidden?: string[]
}

export interface TaskCardPayload {
  chapter_number?: number
  chapter_title?: string
  core_task?: string
  opening_anchor?: string
  scenes?: TaskCardScene[]
  character_goals?: string[]
  information_rules?: TaskCardInformationRules
  tension_design?: string[]
  word_budget?: number
  forbidden?: string[]
}

export interface ClarificationEmbedded {
  needs_clarification: boolean
  questions: ClarificationQuestion[]
  assumptions_if_skipped: string[]
}

export interface TaskCardReviewRequiredPayload {
  task_card: TaskCardPayload
  thread_id: string
  clarification?: ClarificationEmbedded | null
}

// ─── Chapter Version History ───

export interface ApiChapterVersion {
  id: string
  chapter_id: string
  content?: string | null
  word_count: number
  version_number: number
  source: string
  created_at: string
}

export interface ApiDocumentVersion {
  id: string
  document_id: string
  content?: string | null
  word_count: number
  version_number: number
  source: string
  created_at: string
}

export interface ChapterVersion {
  id: string
  chapterId: string
  versionNumber: number
  wordCount: number
  source: string
  createdAt: string
  content: string | null
}

export interface DocumentRevision {
  id: string
  documentId: string
  versionNumber: number
  wordCount: number
  source: string
  createdAt: string
  content: string | null
}

export interface DiffHunk {
  tag: 'equal' | 'insert' | 'delete' | 'replace'
  lines?: string[]
  lines_a?: string[]
  lines_b?: string[]
}

export interface ChapterVersionDiffRequest {
  version_id_a: string
  version_id_b?: string
  current_content?: string
}

export interface ChapterVersionDiffResponse {
  version_a: number
  version_b: number
  diff: DiffHunk[]
}

export type DocumentVersionDiffRequest = ChapterVersionDiffRequest
export type DocumentVersionDiffResponse = ChapterVersionDiffResponse

// ─── AI Generation History ───

export type GenerationRecordStatus = 'candidate' | 'applied' | 'discarded'

export interface ApiGenerationRecordListItem {
  id: string
  project_id: string
  chapter_id: string | null
  document_id: string | null
  run_id?: string | null
  mode: GenerateMode
  expert_id: string | null
  direction: string | null
  word_count: number
  status: GenerationRecordStatus
  langfuse_trace_id?: string | null
  created_at: string
}

export interface ApiGenerationRecord extends ApiGenerationRecordListItem {
  content: string
  user_note: string | null
  target_words: number | null
  accepted_version_id: string | null
  review_results: Record<string, unknown> | null
  request_params: Record<string, unknown> | null
  updated_at: string
}

// ==================== AI Run ====================

export interface ApiRunListItem {
  id: string
  run_type: string
  mode: string
  status: string
  current_step: string | null
  generation_record_id: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string
}

export interface ApiRun extends ApiRunListItem {
  project_id: string
  chapter_id: string | null
  document_id: string | null
  user_goal: string | null
  thread_id: string | null
  token_usage: Record<string, unknown> | null
  cost_usage: Record<string, unknown> | null
  error_message: string | null
  updated_at: string
}

export interface ApiRunStep {
  id: string
  run_id: string
  step_order: number
  step_name: string
  agent_name: string | null
  status: string
  error_message: string | null
  started_at: string | null
  ended_at: string | null
  llm_call_count: number
}

export interface ApiRunContextCall {
  id: string
  run_id: string | null
  step_id: string | null
  step_name: string | null
  agent_name: string | null
  provider: string | null
  model: string | null
  context_snapshot: Record<string, unknown> | null
  context_text: string | null
  prompt_snapshot: string | null
  prompt_truncated: boolean
  error_message: string | null
  created_at: string
}

export interface ApiRunContext {
  run_id: string
  project_id: string
  mode: string
  status: string
  workflow_key: string | null
  calls: ApiRunContextCall[]
}

export interface RunCreatedPayload {
  run_id: string
  status: string
}

export interface GenerationRecord {
  id: string
  projectId: string
  chapterId: string | null
  documentId: string | null
  mode: GenerateMode
  expertId: string | null
  direction: string | null
  wordCount: number
  status: GenerationRecordStatus
  langfuseTraceId: string | null
  runId: string | null
  createdAt: string
  content: string | null
  skillPacks: SkillPackPayload[]
}

export interface GenerationRecordUpdatePayload {
  status: GenerationRecordStatus
}

export interface GenerationRecordDiffRequest {
  current_content: string
}

export interface GenerationRecordDiffResponse {
  generation_id: string
  diff: DiffHunk[]
}

// ─── Evaluation datasets ───

export type EvaluationDatasetMode = 'creative' | 'regression' | 'prompt' | 'model'
export type EvaluationCaseStatus = 'active' | 'disabled' | 'archived'
export type EvaluationGenerationMode = 'generate_and_judge' | 'judge_only'
export type EvaluationRunStatus = 'running' | 'completed' | 'partial' | 'failed'

export interface ApiEvaluationDataset {
  id: string
  project_id: string
  name: string
  description: string | null
  mode: EvaluationDatasetMode
  status: 'active' | 'archived'
  case_count: number
  run_count: number
  created_at: string
  updated_at: string
}

export interface EvaluationDatasetCreatePayload {
  name: string
  description?: string | null
  mode?: EvaluationDatasetMode
}

export interface EvaluationDatasetUpdatePayload {
  name?: string
  description?: string | null
  status?: 'active' | 'archived'
}

export interface ApiEvaluationCase {
  id: string
  dataset_id: string
  project_id: string
  name: string
  task_type: string
  input_text: string
  actual_output: string | null
  reference_output: string | null
  expected_properties: string[] | null
  rubric: Record<string, string> | null
  tags: string[] | null
  status: EvaluationCaseStatus
  created_at: string
  updated_at: string
}

export interface EvaluationCaseCreatePayload {
  name: string
  task_type?: string
  input_text?: string
  actual_output?: string | null
  reference_output?: string | null
  expected_properties?: string[] | null
  rubric?: Record<string, string> | null
  tags?: string[] | null
}

export interface EvaluationCaseUpdatePayload extends Partial<EvaluationCaseCreatePayload> {
  status?: EvaluationCaseStatus
}

export interface ApiEvaluationResult {
  id: string
  run_id: string
  case_id: string
  project_id: string
  generated_output: string | null
  scores: Record<string, number> | null
  score: number | null
  passed: boolean | null
  feedback: string | null
  error: string | null
  latency_ms: number | null
  langfuse_trace_id: string | null
  created_at: string
  updated_at: string
}

export interface ApiEvaluationRun {
  id: string
  dataset_id: string
  project_id: string
  name: string
  generation_mode: EvaluationGenerationMode
  status: EvaluationRunStatus
  model_provider: string | null
  model_id: string | null
  total_cases: number
  completed_cases: number
  failed_cases: number
  average_score: number | null
  summary: string | null
  results: ApiEvaluationResult[]
  created_at: string
  updated_at: string
}

export interface EvaluationRunCreatePayload {
  name?: string | null
  generation_mode?: EvaluationGenerationMode
  case_ids?: string[] | null
}

// ─── LLM Settings types ───

export type LLMProviderName = 'mock' | 'openai' | 'deepseek' | 'siliconflow' | 'zhipu' | 'moonshot' | 'qwen' | 'yi' | 'minimax' | 'custom'

export interface LLMConfigResponse {
  id: string
  provider: LLMProviderName
  api_key_set: boolean
  base_url: string | null
  model_id: string | null
  created_at: string
  updated_at: string
}

export interface LLMConfigCreatePayload {
  provider: LLMProviderName
  api_key?: string
  base_url?: string | null
  model_id?: string | null
}

export interface LLMConfigUpdatePayload {
  provider?: LLMProviderName
  api_key?: string | null
  base_url?: string | null
  model_id?: string | null
}

export interface ModelListRequest {
  provider: LLMProviderName
  api_key: string
  base_url?: string | null
}

export interface ModelInfo {
  id: string
  owned_by: string | null
}

export interface LLMStatusResponse {
  has_config: boolean
  provider: string
  model_id: string | null
  has_api_key: boolean
}
