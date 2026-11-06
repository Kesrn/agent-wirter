"""Harness 核心状态枚举。(str, Enum) 使其与裸字符串直接等价，便于 JSON 序列化与 DB 字符串列兼容。
兼容 Python 3.10（项目 venv），不使用 3.11 才有的 StrEnum。"""

from enum import Enum


class RunStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class RunStepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    WAITING_HUMAN = "WAITING_HUMAN"
    RETRYING = "RETRYING"


class InterruptStatus(str, Enum):
    WAITING = "WAITING"
    APPROVED = "APPROVED"
    EDITED = "EDITED"
    REJECTED = "REJECTED"
    REGENERATE = "REGENERATE"
    EXPIRED = "EXPIRED"
    ANSWERED = "ANSWERED"          # Clarification Loop: 用户提交了回答
    SKIPPED = "SKIPPED"            # Clarification Loop: 用户跳过澄清


class InterruptDecision(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    EDIT = "EDIT"
    REGENERATE = "REGENERATE"
    SUBMIT_CLARIFICATION = "SUBMIT_CLARIFICATION"   # 提交澄清回答
    SKIP_CLARIFICATION = "SKIP_CLARIFICATION"       # 跳过澄清


class MemoryStagingStatus(str, Enum):
    GENERATED = "GENERATED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class MemoryType(str, Enum):
    CHARACTER = "CHARACTER"
    WORLD_RULE = "WORLD_RULE"
    PLOT_FACT = "PLOT_FACT"
    EVENT = "EVENT"
    FORESHADOWING = "FORESHADOWING"
