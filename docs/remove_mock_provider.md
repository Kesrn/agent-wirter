# 移除 Mock Provider 配置

## 变更说明

按用户要求，移除了所有默认使用 mock provider 的配置，强制系统必须配置真实的 LLM provider。

## 修改的文件

### 1. `backend/config/settings.py`

**变更前**:
```python
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "mock")
EMBEDDING_PROVIDER: str = os.getenv("EMBEDDING_PROVIDER", "mock")
```

**变更后**:
```python
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "openai")
EMBEDDING_PROVIDER: str = os.getenv("EMBEDDING_PROVIDER", "openai")
```

注释也更新为移除 mock 选项。

### 2. `backend/agents/llm_provider.py`

**变更前**:
```python
if provider == "mock":
    return MockProvider()
```

**变更后**:
```python
if provider == "mock":
    logger.warning("检测到 mock provider 配置，但 mock 已被禁用，将抛出错误")
    raise LLMConfigError("Mock provider 已被禁用，请配置真实的 LLM provider (OpenAI/DeepSeek等)")
```

现在如果检测到 mock 配置，会抛出明确的错误提示。

### 3. `.env`

**变更前**:
```bash
LLM_PROVIDER=mock
EMBEDDING_PROVIDER=mock
```

**变更后**:
```bash
LLM_PROVIDER=openai
EMBEDDING_PROVIDER=openai
```

注释更新为提示 mock 已被禁用。

### 4. `backend/desktop_server.py`

**变更前**:
```python
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("EMBEDDING_PROVIDER", "mock")
```

**变更后**:
```python
# Mock provider 已被禁用，用户需要在前端配置真实的 LLM provider
# os.environ.setdefault("LLM_PROVIDER", "mock")
# os.environ.setdefault("EMBEDDING_PROVIDER", "mock")
```

注释掉了强制设置 mock 的代码。

## MockProvider 类保留

`MockProvider` 类本身**保留在代码中**，因为：
1. 测试代码可能需要它（`backend/tests/` 中的测试）
2. 但不会被 `get_llm_provider()` 自动返回

如果测试需要使用 mock，可以在测试代码中直接实例化：
```python
from agents.llm_provider import MockProvider
provider = MockProvider()
```

## 影响

### ✅ 优点
1. **强制真实测试**：无法再意外使用 mock provider，所有测试都会使用真实的 LLM API
2. **及早发现配置问题**：如果没有配置 API Key，会立即报错而不是静默使用 mock
3. **更接近生产环境**：开发环境的行为与生产环境一致

### ⚠️ 需要注意
1. **必须配置 API Key**：系统启动前必须在前端或 .env 中配置真实的 LLM provider
2. **会产生 API 费用**：所有生成操作都会调用真实 API，会产生费用
3. **网络依赖**：需要网络连接才能工作
4. **测试套件可能受影响**：部分测试如果依赖默认 mock 行为，需要显式配置

## 配置指南

### 方式 1：通过前端界面配置

1. 启动应用并登录
2. 进入「设置」页面
3. 配置 LLM 设置：
   - Provider: 选择 `openai` / `deepseek` / `siliconflow` 等
   - API Key: 填写你的 API Key
   - Base URL: （可选）自定义 API 端点
   - Model: 选择模型，如 `gpt-4o-mini` / `deepseek-chat`
4. 保存配置

### 方式 2：通过 .env 文件配置

编辑 `/Users/zcx/ai-creative-platform/.env`：

```bash
# DeepSeek 示例
LLM_PROVIDER=deepseek
LLM_API_KEY=sk-your-deepseek-api-key-here
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat

# OpenAI 示例
LLM_PROVIDER=openai
LLM_API_KEY=sk-your-openai-api-key-here
LLM_BASE_URL=
LLM_MODEL=gpt-4o-mini

# SiliconFlow 示例
LLM_PROVIDER=siliconflow
LLM_API_KEY=sk-your-siliconflow-api-key-here
LLM_BASE_URL=https://api.siliconflow.cn/v1
LLM_MODEL=deepseek-ai/DeepSeek-V3
```

同时配置 Embedding：
```bash
EMBEDDING_PROVIDER=openai
EMBEDDING_API_KEY=sk-your-openai-api-key-here
```

## 如果遇到错误

启动后如果看到以下错误：

```
LLMConfigError: Mock provider 已被禁用，请配置真实的 LLM provider
```

或者：

```
LLMConfigError: 当前模型配置缺少 API Key，请在设置里重新保存 API Key 后再试
```

说明还没有配置 LLM，请按照上面的配置指南进行配置。

## 回滚方法

如果需要临时恢复 mock provider（不推荐），可以：

1. 在 `backend/agents/llm_provider.py` 中修改：
   ```python
   if provider == "mock":
       return MockProvider()  # 取消注释并删除抛出异常的代码
   ```

2. 在 `.env` 中改回：
   ```bash
   LLM_PROVIDER=mock
   ```

## 相关问题

**Q: 超时问题修复了吗？**
A: 是的，已在 `llm_provider.py` 中添加了 120秒超时。如果之前卡住是因为使用了真实 provider 但没有超时机制，现在会在120秒后返回明确的错误提示。

**Q: 测试还能运行吗？**
A: 大部分测试应该可以运行。如果测试依赖 mock provider，需要在测试代码中显式创建 `MockProvider()` 实例，或者配置测试环境的 LLM API Key。

**Q: 为什么移除 mock？**
A: Mock provider 会掩盖真实的问题（如网络超时、API 错误等），导致开发时看起来正常，但部署后出现问题。强制使用真实 provider 可以及早发现这些问题。
