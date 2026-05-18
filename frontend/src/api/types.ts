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
}

/** 项目创作模式 */
export type ProjectMode = 'novel' | 'article'

/** 项目 (UI / store) */
export interface Project {
  id: string
  title: string
  /** Backend has no genre field; kept for UI backward-compat, always empty from API */
  genre: string
  /** Backend has no style field; kept for UI backward-compat, always empty from API */
  style: string
  status: string
  mode: ProjectMode
  description: string
  target_words: number
  created_at: string
  updated_at: string
}

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
  status: 'draft' | 'reviewing' | 'revision' | 'final'
  review_comment_ids: string[]
  review_round: number
}

/** 审核意见 */
export interface ReviewComment {
  id: string
  chapter_id: string
  expert_id: string
  comment: string
  severity: 'info' | 'warning' | 'critical'
}

/** 世界观条目 (UI / store) — mapped from ApiWorldEntry */
export interface WorldEntry {
  id: string
  project_id: string
  title: string
  category: string
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

/** 大纲条目 (UI / store) — mapped from ApiOutline via apiOutlineToOutlineItem */
export interface OutlineItem {
  id: string
  project_id: string
  chapter_num: number
  title: string
  summary: string
  turning_point: string | null
  hidden_thread_ids: string[]
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
  status: 'pending' | 'running' | 'success' | 'error'
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
}

export interface ApiExpert {
  id: string
  project_id: string
  name: string
  description: string
  role_type: RoleType
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
}

/** Backend WorldEntryResponse */
export interface ApiWorldEntry {
  id: string
  project_id: string
  title: string
  category: string
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

/** Backend OutlineResponse */
export interface ApiOutline {
  id: string
  project_id: string
  sequence_number: number
  title: string
  summary: string | null
  turning_point: string | null
  hidden_thread_ids: string[]
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
  target_words?: number
  mode?: ProjectMode
}

export interface ChapterCreatePayload {
  title: string
  outline?: string
  sequence_number?: number
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
  content: string
  rules?: Record<string, unknown> | null
  confidence?: 'low' | 'medium' | 'high'
}

export interface WorldEntryUpdatePayload {
  title?: string
  category?: string
  content?: string
  rules?: Record<string, unknown> | null
  confidence?: 'low' | 'medium' | 'high'
}

export interface CharacterCreatePayload {
  name: string
  role_type: 'protagonist' | 'antagonist' | 'supporting' | 'minor'
  profile?: string
  faction?: string
  appearance_count?: number
  metadata?: Record<string, unknown> | null
}

export interface CharacterUpdatePayload {
  name?: string
  role_type?: 'protagonist' | 'antagonist' | 'supporting' | 'minor'
  profile?: string
  faction?: string
  metadata?: Record<string, unknown> | null
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

export interface OutlineCreatePayload {
  sequence_number: number
  title: string
  summary?: string
  turning_point?: string
}

export interface OutlineUpdatePayload {
  sequence_number?: number
  title?: string
  summary?: string
  turning_point?: string
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

export type GenerateMode = 'continue' | 'full_pipeline' | 'enhance' | 'summarize'

export interface GenerateRequest {
  chapter_id?: string
  chapter_num?: number
  mode?: GenerateMode
  expert_id?: string
  selected_outline_ids?: string[]
  selected_character_ids?: string[]
  selected_world_entry_ids?: string[]
  target_words?: number
  enhance_direction?: string
  turn_direction?: string
  user_note?: string
}

// ─── SSE types ───

/** SSE event types emitted by the backend generate/test endpoints */
export type SSEEventType = 'progress' | 'agent_start' | 'agent_output' | 'agent_done' | 'writer_output' | 'critic_output' | 'consistency_check' | 'enhance_directions' | 'turn_suggestions' | 'done' | 'error'

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
export interface ConsistencyCheckPayload {
  report?: string
}

/** Payload for enhance_directions SSE event */
export interface EnhanceDirectionsPayload {
  directions: string[]
}

/** Payload for turn_suggestions SSE event */
export interface TurnSuggestionsPayload {
  suggestions: string[]
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
