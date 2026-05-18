"""安全边界校验"""

from config.settings import settings
from schemas.api import ExpertCreate


def validate_expert_safety(expert: ExpertCreate) -> list[str]:
    """校验自定义 Agent 是否符合安全边界，返回错误列表（空=通过）"""
    errors = []

    # 1. system_prompt 长度
    if len(expert.system_prompt) > settings.MAX_SYSTEM_PROMPT_LENGTH:
        errors.append(f"system_prompt 不能超过 {settings.MAX_SYSTEM_PROMPT_LENGTH} 字符")

    # 2. system_prompt 内容审核
    forbidden_patterns = [
        "api_key", "api key", "apikey",
        "database", "数据库查询", "sql",
        "file system", "文件操作", "rm -rf",
        "系统管理员", "system administrator", "root",
        "ignore previous", "忽略之前的指令",
    ]
    prompt_lower = expert.system_prompt.lower()
    for pattern in forbidden_patterns:
        if pattern in prompt_lower:
            errors.append(f"system_prompt 包含禁止内容: '{pattern}'")

    # 3. max_tokens 上限
    if expert.max_tokens > settings.MAX_TOKENS_LIMIT:
        errors.append(f"max_tokens 不能超过 {settings.MAX_TOKENS_LIMIT}")

    # 4. 受保护的工作流位置
    if expert.workflow_position in settings.PROTECTED_WORKFLOW_POSITIONS:
        errors.append(f"workflow_position '{expert.workflow_position}' 是受保护节点，不可被自定义 Agent 替换")

    # 5. 温度范围
    if not (0.0 <= expert.temperature <= 1.0):
        errors.append("temperature 必须在 0.0-1.0 之间")

    return errors
