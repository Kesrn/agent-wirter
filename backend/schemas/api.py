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
    system_prompt: str
    temperature: float
    max_tokens: int
    workflow_position: str
    context_scope: dict
    trigger: str
    is_builtin: bool
    is_enabled: bool
    color: str
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
    mode: str = Field(default="continue", pattern=r"^(continue|full_pipeline|enhance|summarize)$")
    expert_id: str | None = None
    selected_outline_ids: list[str] | None = None
    selected_character_ids: list[str] | None = None
    selected_world_entry_ids: list[str] | None = None
    selected_hidden_thread_ids: list[str] | None = None
    target_words: int | None = None
    enhance_direction: str | None = None
    turn_direction: str | None = None
    user_note: str | None = None
    content_type: str | None = Field(default=None, max_length=80)
    platform: str | None = Field(default=None, max_length=80)
    audience: str | None = Field(default=None, max_length=200)
    content_goal: str | None = Field(default=None, max_length=200)
    tone: str | None = Field(default=None, max_length=120)
    key_points: str | None = Field(default=None, max_length=2000)


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


# --- 大纲 ---
class OutlineCreate(BaseModel):
    sequence_number: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    summary: str | None = Field(default=None, max_length=10000)
    turning_point: str | None = Field(default=None, max_length=5000)


class OutlineUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    summary: str | None = Field(default=None, max_length=10000)
    turning_point: str | None = Field(default=None, max_length=5000)
    hidden_thread_ids: list[str] | None = None


class OutlineResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    sequence_number: int
    title: str
    summary: str | None
    turning_point: str | None
    hidden_thread_ids: list[str] | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- 暗线 ---
class HiddenThreadCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    chapter_nums: list[int] | None = None


class HiddenThreadUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    chapter_nums: list[int] | None = None


class HiddenThreadResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: str | None
    chapter_nums: list[int] | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


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
    created_at: datetime

    model_config = {"from_attributes": True}


class ChapterVersionResponse(BaseModel):
    id: uuid.UUID
    chapter_id: uuid.UUID
    content: str | None
    word_count: int
    version_number: int
    source: str
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
