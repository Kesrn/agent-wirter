# Workflow V2 错误处理修复

## 问题描述

**用户反馈**：点击"按此任务卡生成"后，进度卡在30%，显示"创作 chapter_... 进行中"，一直不动。

## 根本原因

`workflow_v2.py` 中的多个关键节点**缺少异常捕获**，当 LLM API 调用失败时（超时、网络错误、API key 无效等），异常未被捕获，导致：

1. **后端workflow卡住**：异常向上抛出但没有被处理
2. **前端一直显示"进行中"**：因为后端没有返回错误或完成信号
3. **用户无法获知失败原因**：没有错误提示

## 对比其他节点

**已有错误处理的节点**（工作正常）：
- `clarification_planner`（line 347-355）：有 try-except，失败时 fallback
- `story_recorder`（line 217-225）：有 try-except，失败时返回空记录

**缺少错误处理的节点**（会卡住）：
- ❌ `chapter_architect_node` (line 192)
- ❌ `chapter_writer_node` (line 262)
- ❌ `structural_critic_node` (line 304)
- ❌ `narrative_editor_node` (line 350)

## 修复内容

### 1. chapter_architect_node (line 190-206)

**修复前**：
```python
raw = await llm.generate(system_prompt, user_prompt, temperature=0.6, max_tokens=4096)
```

**修复后**：
```python
try:
    raw = await llm.generate(system_prompt, user_prompt, temperature=0.6, max_tokens=4096)
except Exception as e:
    logger.exception("chapter_architect LLM 调用失败: %s", e)
    # 返回最小可用任务卡
    raw = ""

card = _parse_json_response(raw, {
    "chapter_number": chapter_num,
    "chapter_title": f"第{chapter_num}章" if chapter_num else "",
    "core_task": "[生成失败] 请检查 API 配置",  # 让用户知道失败
    "scenes": [],
    "word_budget": target_words,
    "forbidden": [],
})
```

### 2. chapter_writer_node (line 259-268)

**修复前**：
```python
result = await llm.generate(system_prompt, user_prompt, temperature=0.8)
```

**修复后**：
```python
try:
    result = await llm.generate(system_prompt, user_prompt, temperature=0.8)
except Exception as e:
    logger.exception("chapter_writer LLM 调用失败: %s", e)
    # 返回错误提示，让用户知道失败原因
    result = f"[生成失败] {str(e)}\n\n请检查：\n1. API Key 是否有效\n2. 网络连接是否正常\n3. LLM 服务是否可用"
```

### 3. structural_critic_node (line 304-320)

**修复前**：
```python
raw = await llm.generate(system_prompt, user_prompt, temperature=0.3, max_tokens=2048)
```

**修复后**：
```python
try:
    raw = await llm.generate(system_prompt, user_prompt, temperature=0.3, max_tokens=2048)
except Exception as e:
    logger.exception("structural_critic LLM 调用失败: %s", e)
    raw = ""

critique = _parse_json_response(raw, {
    "summary": raw[:500] if raw else "[审校失败] API 调用错误",
    "p0": [],
    "p1": [],
    "p2": [],
    "edit_instructions": {},
})
```

### 4. narrative_editor_node (line 350-359)

**修复前**：
```python
result = await llm.generate(system_prompt, user_prompt, temperature=0.3, max_tokens=4096)
```

**修复后**：
```python
try:
    result = await llm.generate(system_prompt, user_prompt, temperature=0.3, max_tokens=4096)
except Exception as e:
    logger.exception("narrative_editor LLM 调用失败: %s", e)
    result = draft  # 保留原稿，不修改
```

## 修复效果

### 修复前：
- ❌ LLM API 失败 → workflow 卡住 → 前端一直显示"进行中"
- ❌ 用户不知道什么原因
- ❌ 需要重启后端或刷新页面

### 修复后：
- ✅ LLM API 失败 → 捕获异常 → 记录日志
- ✅ 返回带错误提示的内容 → workflow 继续完成
- ✅ 用户看到明确的错误信息（"[生成失败] 请检查 API 配置"）
- ✅ 可以点击"取消"或重新尝试

## 未来改进方向

### 1. 添加超时保护
```python
# 在 OpenAIProvider.__init__ 中
self.client = AsyncOpenAI(
    api_key=api_key,
    base_url=base_url,
    timeout=120.0  # 2分钟超时
)
```

### 2. 添加重试机制
```python
for attempt in range(3):
    try:
        return await llm.generate(...)
    except Exception as e:
        if attempt == 2:
            raise
        await asyncio.sleep(2 ** attempt)
```

### 3. 前端超时提示
```javascript
const timeout = setTimeout(() => {
  ui.showToast('生成超时，请检查网络或API配置', 'error')
}, 120000)
```

## 验证方法

### 1. 模拟 API 失败
- 使用无效的 API key
- 断开网络连接
- 使用不存在的 base_url

### 2. 预期行为
- 后端日志记录异常
- 前端显示错误提示
- workflow 完成而不是卡住

## 总结

这次修复解决了**workflow 卡住的根本原因**：缺少异常处理。

**关键教训**：
- ✅ 所有 LLM 调用都必须有 try-except
- ✅ 异常必须记录到日志（logger.exception）
- ✅ 必须返回有意义的错误信息给用户
- ✅ 不能让异常导致 workflow 无限等待

**文件修改**：
- `/Users/zcx/ai-creative-platform/backend/agents/workflow_v2.py`（4处修复）

**修复日期**：2026-07-07
