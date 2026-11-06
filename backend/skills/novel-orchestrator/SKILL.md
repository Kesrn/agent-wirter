---
name: novel_orchestrator
description: >
  总编调度器。第一版用规则引擎把 TaskType 映射到 WorkflowDefinition，
  不做自由 LLM Agent。
---

# 总编调度器 — novel-orchestrator

## 核心职责

作为**调度层**，把用户的 TaskType（生成 / 续写 / 增强 / 审稿 / 重写 / 总结 / 抽取记忆）
映射到对应的 WorkflowDefinition。第一版用**确定性规则引擎**实现，不做自由 LLM Agent，
避免成本失控、循环调用、难测试。

## 必须输入

- TaskType（GENERATE_CHAPTER / CONTINUE / ENHANCE_SCENE / REVIEW / REWRITE / SUMMARIZE / EXTRACT_MEMORY）
- 项目模式（novel / article）

## 输出格式

输出选定的 workflow key 与节点序列：

```
workflow_key        // 如 generate_chapter_standard
workflow_version    // v2.0
steps[]             // 节点序列 + expert_key + step_order + checkpoint 标记
```

## 禁止事项

- 不做自由 LLM 调度（不靠 LLM 决定调用链）
- 不允许多个专家无限循环审稿
- 不修改正文 / 任务卡 / 大纲
- 不直接写正式设定表

## 权限边界

- 能做：根据 TaskType 规则映射到 workflow、决定 checkpoint 开关
- 不能做：自由编排任意调用链、改正文、改设定、参与正文生成
