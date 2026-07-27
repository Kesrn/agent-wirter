# Workflow 30% 卡住问题修复

## 问题描述

用户报告章节生成卡在30%进度，界面显示"创作 chapter_... 进行中"，但一直没有响应，也没有错误提示。

## 问题分析

### 现象
- 前端进度条显示30%（对应 writer 节点正在运行）
- 工作流节点状态显示"进行中"
- 后端没有异常日志输出
- SSE连接保持打开，但没有新的事件发送

### 根本原因

**OpenAIProvider 没有设置超时时间**

在 `backend/agents/llm_provider.py` 第552行：
```python
self.client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)
```

当创建 AsyncOpenAI client 时，没有指定 `timeout` 参数，导致：
1. LLM API 调用可以**无限期等待**
2. 如果 API 响应缓慢、网络问题或服务端挂起，`await self.client.chat.completions.create()` 会一直阻塞
3. 没有超时异常抛出，所以 workflow_v2.py 中的 try-except 块无法捕获
4. 前端收到 `agent_start` 事件后，永远收不到 `agent_done` 或 `error` 事件
5. 用户界面一直显示"进行中"状态

### 为什么之前的异常处理没有解决问题

之前在 workflow_v2.py 的4个节点中添加了 try-except：
```python
try:
    result = await llm.generate(system_prompt, user_prompt, temperature=0.8)
except Exception as e:
    logger.exception("chapter_writer LLM 调用失败: %s", e)
    result = f"[生成失败] {str(e)}..."
```

但这些异常处理只能捕获**已经抛出的异常**。如果 `llm.generate()` 因为没有超时设置而一直等待，根本不会抛出异常，try-except 就无法生效。

### 对比发现

在 `backend/api/llm_settings.py` 第147行的测试连接中，**正确设置了超时**：
```python
client = AsyncOpenAI(api_key=req.api_key, base_url=base_url, timeout=5.0)
```

这说明系统其他部分已经意识到需要超时保护，但 LLMProvider 的主要创建点遗漏了这个设置。

## 修复方案

### 代码变更

**文件**: `backend/agents/llm_provider.py`
**行号**: 552

**修改前**:
```python
self.client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)
```

**修改后**:
```python
# 设置120秒超时：章节生成可能较慢，但不应无限等待
self.client = AsyncOpenAI(api_key=api_key, base_url=base_url or None, timeout=120.0)
```

### 超时时间选择

选择 **120秒** 的理由：
- 章节生成通常需要10-60秒，取决于：
  - 模型速度（GPT-4 较慢，DeepSeek 较快）
  - 目标字数（2000字 vs 5000字）
  - 网络延迟
- 120秒足够覆盖正常的慢速响应
- 但不会让用户无限等待
- 超时后会抛出 `openai.APITimeoutError`，被 try-except 捕获
- 用户会看到明确的错误提示，而不是界面卡住

### 修复效果

修复后的行为：
1. 如果 LLM API 在120秒内没有响应，AsyncOpenAI 会抛出 `APITimeoutError`
2. workflow_v2.py 中的 try-except 捕获这个异常
3. 日志记录异常详情：`logger.exception("chapter_writer LLM 调用失败: %s", e)`
4. 返回错误提示给前端：
   ```
   [生成失败] Request timed out.

   请检查：
   1. API Key 是否有效
   2. 网络连接是否正常
   3. LLM 服务是否可用
   ```
5. 前端收到完成事件，节点状态更新为 error 或显示错误内容
6. 用户可以重试或检查配置

## 测试验证

### 验证步骤

1. **正常场景**：使用有效的 API Key 生成章节，应在60秒内完成
2. **超时场景**：使用无效的 base_url 或故意阻塞的 endpoint，应在120秒后返回超时错误
3. **网络中断场景**：生成过程中断开网络，应快速检测到连接失败

### 预期结果

- ✅ 正常生成不受影响
- ✅ 超时后有明确错误提示
- ✅ 后端日志记录异常堆栈
- ✅ 前端不再卡在"进行中"状态

## 相关文件

- `backend/agents/llm_provider.py` - 主要修复点
- `backend/agents/workflow_v2.py` - 已有的异常处理（配合超时机制生效）
- `backend/api/llm_settings.py` - 参考的超时实现

## 历史记录

- **2026-07-07**: 发现并修复 OpenAIProvider 缺少超时设置的问题
- **之前**: 在 workflow_v2.py 添加了异常处理，但未解决根本原因

## 相关问题

- 如果用户需要更长的生成时间（如10000字章节），可以考虑：
  - 将超时时间设为可配置
  - 或者使用流式生成（已支持 generate_stream）
  - 或者拆分为多个小段生成
