"""API 请求/响应 Schema"""

import uuid
from datetime import datetime
from pydantic import BaseModel, Field, field_validator, model_validator


# --- 项目 ---
class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    overall_outline: str | None = Field(default=None, max_length=50000)
    genre: str | None = Field(default=None, max_length=200)
    style: str | None = Field(default=None, max_length=200)
    target_words: int = 200000
    mode: str = Field(default="novel", pattern=r"^(novel|article)$")


class ProjectUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    overall_outline: str | None = Field(default=None, max_length=50000)
    genre: str | None = Field(default=None, max_length=200)
    style: str | None = Field(default=None, max_length=200)
    target_words: int | None = Field(default=None, ge=1)
    status: str | None = Field(default=None, pattern=r"^(active|archived|completed)$")


class ProjectResponse(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    overall_outline: str | None
    genre: str | None
    style: str | None
    target_words: int
    status: str
    mode: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TxtImportChapterPreview(BaseModel):
    sequence_number: int
    title: str
    word_count: int
    preview: str


class TxtImportMeta(BaseModel):
    filename: str | None = None
    encoding: str | None = None
    had_bom: bool | None = None
    bytes: int | None = None
    chars: int
    word_count: int
    chapter_count: int
    chapters: list[TxtImportChapterPreview]


# --- 专家 ---
class ExpertCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    role_type: str = Field(pattern=r"^(writer|critic|editor|researcher|custom)$")
    skill_dir: str | None = Field(default=None, max_length=100)
    system_prompt: str = Field(default="", max_length=2000)
    temperature: float = Field(default=0.7, ge=0.0, le=1.0)
    max_tokens: int = Field(default=4096, ge=100, le=8192)
    workflow_position: str = Field(
        default="standalone",
        pattern=r"^(pre_writer|post_writer|replace_writer|pre_critic|replace_critic|post_critic|standalone)$",
    )
    context_scope: dict = Field(default_factory=lambda: {
        "include_world": True,
        "include_characters": True,
        "include_previous_chapters": 3,
        "include_outline": True,
    })
    trigger: str = Field(default="manual", pattern=r"^(manual|auto_on_draft|auto_on_save|auto_on_chapter_complete)$")
    color: str = Field(default="blue", max_length=20)

    @field_validator("system_prompt")
    @classmethod
    def validate_prompt_length(cls, v: str) -> str:
        if len(v) > 2000:
            raise ValueError("system_prompt 不能超过 2000 字符")
        return v

    @field_validator("workflow_position")
    @classmethod
    def validate_protected_positions(cls, v: str) -> str:
        protected = ("consistency_checker", "human_review")
        if v in protected:
            raise ValueError(f"workflow_position '{v}' 是受保护节点，不可被自定义 Agent 替换")
        return v


class ExpertUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    skill_dir: str | None = Field(default=None, max_length=100)
    system_prompt: str | None = Field(default=None, max_length=2000)
    temperature: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, ge=100, le=8192)
    workflow_position: str | None = Field(
        default=None,
        pattern=r"^(pre_writer|post_writer|replace_writer|pre_critic|replace_critic|post_critic|standalone)$",
    )
    context_scope: dict | None = None
    trigger: str | None = Field(default=None, pattern=r"^(manual|auto_on_draft|auto_on_save|auto_on_chapter_complete)$")
    is_enabled: bool | None = None
    color: str | None = Field(default=None, max_length=20)
    deprecated: bool | None = None  # Expert System v2: 标记旧大师

    @field_validator("system_prompt")
    @classmethod
    def validate_prompt_length(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 2000:
            raise ValueError("system_prompt 不能超过 2000 字符")
        return v

    @field_validator("workflow_position")
    @classmethod
    def validate_protected_positions(cls, v: str | None) -> str | None:
        if v is not None:
            protected = ("consistency_checker", "human_review")
            if v in protected:
                raise ValueError(f"workflow_position '{v}' 是受保护节点，不可被自定义 Agent 替换")
        return v


class ExpertResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: str
    role_type: str
    skill_dir: str | None
    system_prompt: str
    temperature: float
    max_tokens: int
    workflow_position: str
    context_scope: dict
    trigger: str
    is_builtin: bool
    is_enabled: bool
    color: str
    # Expert System v2
    expert_key: str | None = None
    version: int = 1
    deprecated: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


# --- 章节 ---
class ChapterCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    outline: str | None = None
    sequence_number: int = Field(default=0, ge=0)


class ChapterUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    outline: str | None = None
    content: str | None = None
    status: str | None = Field(default=None, pattern=r"^(draft|reviewing|revision|final|approved)$")


class ChapterResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    content: str | None
    outline: str | None
    sequence_number: int
    word_count: int
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ChapterReviewNoteCreate(BaseModel):
    source_type: str = Field(default="manual", pattern=r"^[a-z][a-z0-9_]{0,29}$")
    severity: str = Field(default="info", pattern=r"^(info|warning|critical)$")
    content: str = Field(min_length=1, max_length=20000)
    resolved: bool = False
    metadata_: dict | None = Field(default=None, alias="metadata")


class ChapterReviewNoteUpdate(BaseModel):
    source_type: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,29}$")
    severity: str | None = Field(default=None, pattern=r"^(info|warning|critical)$")
    content: str | None = Field(default=None, min_length=1, max_length=20000)
    resolved: bool | None = None
    metadata_: dict | None = Field(default=None, alias="metadata")


class ChapterReviewNoteResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    chapter_id: uuid.UUID
    chapter_sequence_number: int
    source_type: str
    severity: str
    content: str
    resolved: bool
    metadata_: dict | None = Field(None, serialization_alias="metadata")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class TxtImportResponse(BaseModel):
    project: ProjectResponse
    chapters: list[TxtImportChapterPreview]
    import_meta: TxtImportMeta


class ChapterStructureExtractRequest(BaseModel):
    targets: list[str] = Field(default_factory=lambda: ["outlines", "characters", "world_entries", "hidden_threads", "character_relations", "character_events"])
    mode: str = Field(default="preview", pattern=r"^(preview|apply)$")
    merge_strategy: str = Field(default="upsert", pattern=r"^(upsert|create_only)$")
    include_existing_context: bool = True
    extraction: dict | None = None


class ChapterStructureApplyResult(BaseModel):
    counts: dict[str, int]


class ChapterStructureExtractResponse(BaseModel):
    extraction: dict
    preview: dict
    applied: ChapterStructureApplyResult | None = None


# --- 生成请求 ---
class GenerateRequest(BaseModel):
    chapter_id: str | None = None
    document_id: str | None = None
    chapter_num: int | None = Field(default=None, ge=1)
    mode: str = Field(default="full_pipeline", pattern=r"^(continue|full_pipeline|enhance|summarize)$")
    expert_id: str | None = None
    selected_outline_ids: list[str] | None = None
    selected_character_ids: list[str] | None = None
    selected_world_entry_ids: list[str] | None = None
    selected_hidden_thread_ids: list[str] | None = None
    include_knowledge_sources: bool = False
    target_words: int | None = None
    selected_direction: str | None = None
    direction_option_id: str | None = None
    enhance_direction: str | None = None
    turn_direction: str | None = None
    user_note: str | None = None
    content_type: str | None = Field(default=None, max_length=80)
    platform: str | None = Field(default=None, max_length=80)
    audience: str | None = Field(default=None, max_length=200)
    content_goal: str | None = Field(default=None, max_length=200)
    tone: str | None = Field(default=None, max_length=120)
    key_points: str | None = Field(default=None, max_length=2000)
    planning_review: bool = False  # L-1: 是否启用任务卡预览


# --- 专家测试请求 ---
class ExpertTestRequest(BaseModel):
    test_text: str = Field(min_length=1, max_length=2000)


# --- SSE 事件 ---
class SSEEvent(BaseModel):
    event: str  # progress | agent_start | agent_output | agent_done | critique | approval | done | error
    data: dict


# --- 世界观条目 ---
class WorldEntryCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    category: str = Field(default="general", max_length=50)
    scope_type: str = Field(default="global", pattern=r"^(global|chapter)$")
    content: str = Field(default="", max_length=10000)
    rules: dict | None = None
    confidence: str = Field(default="medium", pattern=r"^(low|medium|high)$")


class WorldEntryUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=50)
    scope_type: str | None = Field(default=None, pattern=r"^(global|chapter)$")
    content: str | None = Field(default=None, max_length=10000)
    rules: dict | None = None
    confidence: str | None = Field(default=None, pattern=r"^(low|medium|high)$")


class WorldEntryResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    category: str
    scope_type: str
    content: str
    rules: dict | None
    confidence: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- 角色 ---
class CharacterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role_type: str = Field(default="supporting", pattern=r"^(protagonist|antagonist|supporting|minor)$")
    scope_type: str = Field(default="recurring", pattern=r"^(core|recurring|chapter|cameo)$")
    profile: str | None = Field(default=None, max_length=5000)
    faction: str | None = Field(default=None, max_length=100)
    metadata_: dict | None = Field(default=None, alias="metadata")


class CharacterUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    role_type: str | None = Field(default=None, pattern=r"^(protagonist|antagonist|supporting|minor)$")
    scope_type: str | None = Field(default=None, pattern=r"^(core|recurring|chapter|cameo)$")
    profile: str | None = Field(default=None, max_length=5000)
    faction: str | None = Field(default=None, max_length=100)
    metadata_: dict | None = Field(default=None, alias="metadata")


class CharacterMergeRequest(BaseModel):
    target_character_id: uuid.UUID


class CharacterResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    role_type: str
    scope_type: str
    profile: str | None
    faction: str | None
    appearance_count: int
    metadata_: dict | None = Field(None, serialization_alias="metadata")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class CharacterEventUpsert(BaseModel):
    appearance_type: str = Field(default="appeared", pattern=r"^(appeared|mentioned|absent)$")
    event_summary: str | None = Field(default=None, max_length=10000)
    actions: list[str] | None = None
    state_change: str | None = Field(default=None, max_length=10000)
    location: str | None = Field(default=None, max_length=200)
    emotion: str | None = Field(default=None, max_length=100)
    importance: int = Field(default=3, ge=1, le=5)


class CharacterEventResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    character_id: uuid.UUID
    chapter_sequence_number: int
    appearance_type: str
    appeared: bool
    event_summary: str | None
    actions: list[str] | None
    state_change: str | None
    location: str | None
    emotion: str | None
    importance: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- 角色弧线 (K-5) ---

class CharacterArcItem(BaseModel):
    """角色弧线中的单个事件"""
    chapter_sequence_number: int | None
    source_type: str  # CharacterEvent / WritingMemory
    title: str
    summary: str = ""
    state_change: str | None = None
    emotion: str | None = None
    importance: int = 3
    confidence: str = "confirmed"  # confirmed / ai_extracted


class CharacterArcResponse(BaseModel):
    """角色弧线聚合视图"""
    character_id: uuid.UUID
    character_name: str
    role_type: str
    items: list[CharacterArcItem]
    chapter_range: str = ""


# --- 大纲 ---
class OutlineCreate(BaseModel):
    sequence_number: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    summary: str | None = Field(default=None, max_length=10000)
    turning_point: str | None = Field(default=None, max_length=5000)
    story_arc_id: str | None = None
    arc_position: str | None = Field(default=None, pattern=r"^(SETUP|BUILDUP|TURNING_POINT|CLIMAX|AFTERMATH)$")


class OutlineUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    summary: str | None = Field(default=None, max_length=10000)
    turning_point: str | None = Field(default=None, max_length=5000)
    hidden_thread_ids: list[str] | None = None
    story_arc_id: str | None = None
    arc_position: str | None = Field(default=None, pattern=r"^(SETUP|BUILDUP|TURNING_POINT|CLIMAX|AFTERMATH)$")


class OutlineResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    sequence_number: int
    title: str
    summary: str | None
    turning_point: str | None
    hidden_thread_ids: list[str] | None
    story_arc_id: uuid.UUID | None = None
    arc_position: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- 暗线 ---
class HiddenThreadCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    chapter_nums: list[int] | None = None
    status: str = Field(default="PLANNED", pattern=r"^(PLANNED|PLANTED|ACTIVE|REVEALED|RESOLVED|DROPPED)$")
    thread_type: str | None = Field(default=None, pattern=r"^(FORESHADOWING|SECRET|RELATIONSHIP|WORLD_RULE)$")
    planted_chapter: int | None = Field(default=None, ge=1)
    reveal_chapter: int | None = Field(default=None, ge=1)


class HiddenThreadUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    chapter_nums: list[int] | None = None
    status: str | None = Field(default=None, pattern=r"^(PLANNED|PLANTED|ACTIVE|REVEALED|RESOLVED|DROPPED)$")
    thread_type: str | None = Field(default=None, pattern=r"^(FORESHADOWING|SECRET|RELATIONSHIP|WORLD_RULE)$")
    planted_chapter: int | None = Field(default=None, ge=1)
    reveal_chapter: int | None = Field(default=None, ge=1)
    resolved_chapter: int | None = Field(default=None, ge=1)
    payoff_summary: str | None = None
    risk_level: str | None = Field(default=None, pattern=r"^(LOW|MEDIUM|HIGH)$")


class HiddenThreadResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: str | None
    chapter_nums: list[int] | None
    status: str
    thread_type: str | None = None
    planted_chapter: int | None = None
    reveal_chapter: int | None = None
    resolved_chapter: int | None = None
    payoff_summary: str | None = None
    risk_level: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- 长线结构 (Story Arc) ---
class StoryArcCreate(BaseModel):
    arc_type: str = Field(pattern=r"^(VOLUME|ACT|ARC)$")
    name: str = Field(min_length=1, max_length=200)
    summary: str | None = Field(default=None, max_length=10000)
    goal: str | None = Field(default=None, max_length=10000)
    main_conflict: str | None = Field(default=None, max_length=10000)
    parent_arc_id: str | None = None
    start_chapter: int | None = Field(default=None, ge=1)
    end_chapter: int | None = Field(default=None, ge=1)
    order_index: int = Field(default=0, ge=0)
    status: str = Field(default="PLANNED", pattern=r"^(PLANNED|ACTIVE|COMPLETED|PAUSED|ARCHIVED)$")


class StoryArcUpdate(BaseModel):
    arc_type: str | None = Field(default=None, pattern=r"^(VOLUME|ACT|ARC)$")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    summary: str | None = Field(default=None, max_length=10000)
    goal: str | None = Field(default=None, max_length=10000)
    main_conflict: str | None = Field(default=None, max_length=10000)
    parent_arc_id: str | None = None
    start_chapter: int | None = Field(default=None, ge=1)
    end_chapter: int | None = Field(default=None, ge=1)
    order_index: int | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, pattern=r"^(PLANNED|ACTIVE|COMPLETED|PAUSED|ARCHIVED)$")


class StoryArcResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    parent_arc_id: uuid.UUID | None = None
    arc_type: str
    name: str
    summary: str | None
    goal: str | None
    main_conflict: str | None
    start_chapter: int | None
    end_chapter: int | None
    order_index: int
    status: str
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


# --- 角色关系 ---
class CharacterRelationCreate(BaseModel):
    source_character_id: uuid.UUID
    target_character_id: uuid.UUID
    description: str = Field(min_length=1, max_length=500)


class CharacterRelationUpdate(BaseModel):
    description: str | None = Field(default=None, min_length=1, max_length=500)


class CharacterRelationResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    source_character_id: uuid.UUID
    target_character_id: uuid.UUID
    description: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- LLM 配置 ---
class LLMConfigCreate(BaseModel):
    provider: str = Field(pattern=r"^(mock|openai|deepseek|siliconflow|zhipu|moonshot|qwen|yi|minimax|custom)$")
    api_key: str = Field(default="", max_length=500)
    base_url: str | None = Field(default=None, max_length=500)
    model_id: str | None = Field(default=None, max_length=100)


class LLMConfigUpdate(BaseModel):
    provider: str | None = Field(default=None, pattern=r"^(mock|openai|deepseek|siliconflow|zhipu|moonshot|qwen|yi|minimax|custom)$")
    api_key: str | None = Field(default=None, max_length=500)  # null=keep existing, ""=clear
    base_url: str | None = Field(default=None, max_length=500)
    model_id: str | None = Field(default=None, max_length=100)


class LLMConfigResponse(BaseModel):
    id: uuid.UUID
    provider: str
    api_key_set: bool
    base_url: str | None
    model_id: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ModelListRequest(BaseModel):
    provider: str = Field(pattern=r"^(openai|deepseek|siliconflow|zhipu|moonshot|qwen|yi|minimax|custom)$")
    api_key: str = Field(min_length=1, max_length=500)
    base_url: str | None = Field(default=None, max_length=500)


class ModelInfo(BaseModel):
    id: str
    owned_by: str | None = None


# --- 认证 ---
class LoginRequest(BaseModel):
    username: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=100)
    password: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def check_identity(self):
        if not self.username and not self.email:
            raise ValueError("username 或 email 至少提供一个")
        return self


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=6, max_length=100)
    email: str | None = Field(default=None, max_length=200)


class RegisterResponse(BaseModel):
    id: str
    username: str
    created_at: datetime

    model_config = {"from_attributes": True}


class AuthUser(BaseModel):
    id: str
    username: str
    display_name: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: AuthUser


# --- 章节版本 ---
class ChapterVersionListItemResponse(BaseModel):
    id: uuid.UUID
    chapter_id: uuid.UUID
    word_count: int
    version_number: int
    source: str
    run_id: uuid.UUID | None = None
    parent_version_id: uuid.UUID | None = None
    rollback_from_version_id: uuid.UUID | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChapterVersionResponse(BaseModel):
    id: uuid.UUID
    chapter_id: uuid.UUID
    content: str | None
    word_count: int
    version_number: int
    source: str
    run_id: uuid.UUID | None = None
    parent_version_id: uuid.UUID | None = None
    rollback_from_version_id: uuid.UUID | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChapterVersionDiffRequest(BaseModel):
    version_id_a: uuid.UUID
    version_id_b: uuid.UUID | None = None
    current_content: str | None = None

    @model_validator(mode="after")
    def validate_compare_target(self):
        if self.version_id_b is None and self.current_content is None:
            raise ValueError("version_id_b 或 current_content 必须提供一个")
        return self


class ChapterVersionDiffResponse(BaseModel):
    version_a: int
    version_b: int
    diff: list[dict]


# --- 文档（文章/文案模式） ---
class DocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str | None = None
    position: int | None = Field(default=None, ge=0)


class DocumentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = None
    status: str | None = Field(default=None, pattern=r"^(draft|reviewing|revision|final|approved)$")


class DocumentResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    content: str | None
    position: int
    word_count: int
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- 文档版本 ---
class DocumentVersionListItemResponse(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    word_count: int
    version_number: int
    source: str
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentVersionResponse(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    content: str | None
    word_count: int
    version_number: int
    source: str
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentVersionDiffRequest(BaseModel):
    version_id_a: uuid.UUID
    version_id_b: uuid.UUID | None = None
    current_content: str | None = None

    @model_validator(mode="after")
    def validate_compare_target(self):
        if self.version_id_b is None and self.current_content is None:
            raise ValueError("version_id_b 或 current_content 必须提供一个")
        return self


class DocumentVersionDiffResponse(BaseModel):
    version_a: int
    version_b: int
    diff: list[dict]


# --- AI 生成历史 ---
class GenerationRecordListItemResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    chapter_id: uuid.UUID | None
    document_id: uuid.UUID | None
    run_id: uuid.UUID | None = None
    mode: str
    expert_id: uuid.UUID | None
    direction: str | None
    word_count: int
    status: str
    langfuse_trace_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class GenerationRecordResponse(GenerationRecordListItemResponse):
    content: str
    user_note: str | None
    target_words: int | None
    accepted_version_id: uuid.UUID | None
    review_results: dict | None
    request_params: dict | None
    updated_at: datetime

    model_config = {"from_attributes": True}


class GenerationRecordUpdate(BaseModel):
    status: str = Field(pattern=r"^(candidate|applied|discarded)$")


class GenerationRecordDiffRequest(BaseModel):
    current_content: str


class GenerationRecordDiffResponse(BaseModel):
    generation_id: uuid.UUID
    diff: list[dict]


# --- 评测集 ---
class EvaluationDatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    mode: str = Field(default="creative", pattern=r"^(creative|regression|prompt|model)$")


class EvaluationDatasetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    status: str | None = Field(default=None, pattern=r"^(active|archived)$")


# --- 写作记忆 staging ---

class WritingMemoryStagingResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID | None = None
    chapter_id: uuid.UUID | None = None
    chapter_version_id: uuid.UUID | None = None
    chapter_sequence_number: int | None = None
    memory_type: str
    title: str
    payload: dict = Field(default_factory=dict)
    evidence: str | None = None
    status: str
    confirmed_target_type: str | None = None
    confirmed_target_id: uuid.UUID | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class EvaluationDatasetResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: str | None
    mode: str
    status: str
    case_count: int = 0
    run_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class EvaluationCaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    task_type: str = Field(default="creative_generation", max_length=50)
    input_text: str = Field(default="", max_length=20000)
    actual_output: str | None = Field(default=None, max_length=30000)
    reference_output: str | None = Field(default=None, max_length=30000)
    expected_properties: list[str] | None = None
    rubric: dict | None = None
    tags: list[str] | None = None


class EvaluationCaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    task_type: str | None = Field(default=None, max_length=50)
    input_text: str | None = Field(default=None, max_length=20000)
    actual_output: str | None = Field(default=None, max_length=30000)
    reference_output: str | None = Field(default=None, max_length=30000)
    expected_properties: list[str] | None = None
    rubric: dict | None = None
    tags: list[str] | None = None
    status: str | None = Field(default=None, pattern=r"^(active|disabled|archived)$")


class EvaluationCaseResponse(BaseModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    project_id: uuid.UUID
    name: str
    task_type: str
    input_text: str
    actual_output: str | None
    reference_output: str | None
    expected_properties: list[str] | None
    rubric: dict | None
    tags: list[str] | None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class EvaluationRunCreate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    generation_mode: str = Field(default="generate_and_judge", pattern=r"^(generate_and_judge|judge_only)$")
    case_ids: list[uuid.UUID] | None = None


class EvaluationResultResponse(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    case_id: uuid.UUID
    project_id: uuid.UUID
    generated_output: str | None
    scores: dict | None
    score: float | None
    passed: bool | None
    feedback: str | None
    error: str | None
    latency_ms: int | None
    langfuse_trace_id: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class EvaluationRunResponse(BaseModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    project_id: uuid.UUID
    name: str
    generation_mode: str
    status: str
    model_provider: str | None
    model_id: str | None
    total_cases: int
    completed_cases: int
    failed_cases: int
    average_score: float | None
    summary: str | None
    results: list[EvaluationResultResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── 资料库 (Project Knowledge) ─────────────────────────────────────

class ProjectSourceCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    source_type: str = Field(default="upload", pattern=r"^(upload|fanfic_rule|timeline|note|reference|novel)$")
    content: str = Field(default="", max_length=50_000_000)
    tags: list[str] | None = None
    always_inject: bool = False
    metadata_: dict | None = None
    # 小说抽取相关（仅 source_type=novel 时有意义，存入 metadata_）
    genre: str | None = Field(default=None, pattern=r"^(magic_fantasy|historical)$")
    canon_level: str | None = Field(default=None, pattern=r"^(manual|fanfic|original)$")


class ProjectSourceUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    source_type: str | None = Field(default=None, pattern=r"^(upload|fanfic_rule|timeline|note|reference|novel)$")
    content: str | None = Field(default=None, max_length=50_000_000)
    tags: list[str] | None = None
    always_inject: bool | None = None


class ProjectSourceResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    source_type: str
    content: str
    summary: str | None = None
    key_facts: list | None = None
    constraints: list | None = None
    characters: list | None = None
    keywords: list | None = None
    tags: list | None = None
    always_inject: bool
    chunk_count: int
    token_count: int
    metadata_: dict | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectSourceListResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    source_type: str
    content_preview: str = ""
    content_truncated: bool = False
    summary: str | None = None
    key_facts: list | None = None
    constraints: list | None = None
    characters: list | None = None
    keywords: list | None = None
    tags: list | None = None
    always_inject: bool
    chunk_count: int
    token_count: int
    created_at: datetime
    updated_at: datetime


class ProjectSourceChunkResponse(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    chunk_index: int
    content: str
    summary: str | None = None
    facts: list | None = None
    constraints: list | None = None
    keywords: list | None = None
    token_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class KnowledgeQaSessionResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    summary: str | None = None
    message_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class KnowledgeQaSessionUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)


class KnowledgeQaMessageResponse(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str
    citations: list | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class KnowledgeAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None
    chapter_num: int | None = None
    include_structured: bool = True
    include_web: bool = False
    web_provider: str | None = Field(default=None, max_length=50)
    web_api_key: str | None = Field(default=None, max_length=500)
    web_base_url: str | None = Field(default=None, max_length=500)


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    source_type: str | None = None
    chapter_num: int | None = None
    limit: int = Field(default=10, ge=1, le=50)


class Citation(BaseModel):
    source_kind: str  # project_source_chunk | character | character_event | outline | world_entry | hidden_thread | chapter | web_search
    source_id: str
    chunk_id: str | None = None
    title: str
    snippet: str
    url: str | None = None
    evidence_type: str | None = None
    matched_query: str | None = None
    score: float | None = None


class KnowledgeAskResponse(BaseModel):
    answer: str
    citations: list[Citation]
    conversation_id: str
    conversation_summary_updated: bool = False
    query_plan: dict | None = None
    retrieval_stats: dict | None = None


# ==================== AI Run ====================

class AiRunResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    chapter_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    generation_record_id: uuid.UUID | None = None
    run_type: str
    mode: str
    status: str
    current_step: str | None = None
    user_goal: str | None = None
    thread_id: str | None = None
    token_usage: dict | None = None
    cost_usage: dict | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AiRunListItemResponse(BaseModel):
    id: uuid.UUID
    run_type: str
    mode: str
    status: str
    current_step: str | None = None
    generation_record_id: uuid.UUID | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AiRunStepResponse(BaseModel):
    """step 摘要——不含 input/output 等大字段，避免 API 响应变重。"""
    id: uuid.UUID
    run_id: uuid.UUID
    step_order: int
    step_name: str
    agent_name: str | None = None
    status: str
    error_message: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    llm_call_count: int = 0

    model_config = {"from_attributes": True}


class AiRunContextCallResponse(BaseModel):
    """一次 LLM 调用的可解释上下文快照。

    只暴露用户需要复盘的上下文与调用摘要，不返回模型配置密钥等敏感信息。
    """
    id: uuid.UUID
    run_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None
    step_name: str | None = None
    agent_name: str | None = None
    provider: str | None = None
    model: str | None = None
    context_snapshot: dict | None = None
    context_text: str | None = None
    prompt_snapshot: str | None = None
    prompt_truncated: bool = False
    error_message: str | None = None
    created_at: datetime


class AiRunContextResponse(BaseModel):
    run_id: uuid.UUID
    project_id: uuid.UUID
    mode: str
    status: str
    workflow_key: str | None = None
    calls: list[AiRunContextCallResponse]


# ==================== Human Interrupt ====================

class HumanDecisionRequest(BaseModel):
    """人工审核决策请求"""
    decision: str = Field(..., pattern=r"^(APPROVE|REJECT|EDIT|REGENERATE)$")
    feedback: str | None = None


class HumanDecisionResponse(BaseModel):
    """人工审核决策响应"""
    run_id: str
    interrupt_id: str
    status: str
    message: str


# ── Clarification Loop ──


class ClarificationAnswerRequest(BaseModel):
    """提交澄清回答请求"""
    action: str = Field(..., pattern=r"^(submit|skip)$")
    answers: dict[str, str] = Field(default_factory=dict)


class ClarificationResponse(BaseModel):
    """澄清状态响应"""
    run_id: str
    interrupt_id: str | None = None
    status: str
    round: int = 1
    max_rounds: int = 3
    questions: list[dict] = Field(default_factory=list)
    assumptions_if_skipped: list[str] = Field(default_factory=list)
    existing_answers: dict[str, str] = Field(default_factory=dict)
    clarification_summary: str = ""
    resolved: bool = False

    model_config = {"from_attributes": True}
