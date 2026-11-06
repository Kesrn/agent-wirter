# 最小 Agent 演示

这是一个不接 API、不写数据库的独立工具循环示例。离线轨迹是预先写好的剧本，只演示循环、工具观察、校验失败后的重写和预算控制，不代表模型能力。

在 `backend/` 运行：

```bash
python scripts/agent_demo.py
python scripts/agent_demo.py --max-steps 2
```

## 模块图

`scripts/agent_demo.py` → `agent_demo.loop` → `agent_demo.tools` → `ProjectStore`（当前只有 `InMemoryProjectStore`）；`ScriptedProvider` 为测试和离线剧本提供确定响应。

## 工具

- `read_outline`：读取全部或指定章节大纲。
- `get_character`：按姓名或别名读取角色卡。
- `search_chapters`：用简单关键词/二元组重合度检索已写段落。
- `write_scene`：调用一次模型写场景，保存全文并只返回草稿编号、字数和短预览。
- `run_validators`：对草稿运行任务卡确定性规则。

## 预算与停止原因

|预算|默认值|停止原因|
|---|---:|---|
|Agent 步数|8|`max_steps`|
|累计 token|20000|`token_budget`|
|墙钟时间|120 秒|`time_budget`|
|工具错误|超过 3 次|`too_many_tool_errors`|
|相同工具与参数|最多 2 次|`repeated_call`|
|单次观察|1500 字符|超长截断，不单独停止|
|模型调用异常|—|`llm_error`|
|模型返回无工具调用|—|`final_answer`|

## 设计取舍与局限

现有 `complete()` 接口每次只接收 system/user 两段文本，没有 messages 列表；因此每轮把目标、前序工具调用和观察结果重新序列化到 user prompt（重放式上下文），实现简单但会重复上下文。`write_scene` 不把全文塞回循环，只回传 `draft_id`，后续由验证工具在本地读取。数据通过 `ProjectStore` 抽象提供，暂不接数据库，便于离线运行和测试。

工具按模型返回顺序串行执行，没有并发工具调用优化；检索是玩具实现；没有接入 `AiRun` 追踪表；离线轨迹是固定剧本。与现有固定工作流（LangGraph）相比，这个 demo 由模型决定下一步，流程更灵活，但每步仍受预算约束，成本与步骤数不如固定流程确定；固定工作流的顺序更可控、成本更容易预估。

只有 `--real --yes` 才会构造真实 provider；`--real` 单独使用会打印预算并退出，不发起模型调用。运行轨迹会写入 `agent_demo/runs/`，该目录已被忽略。
