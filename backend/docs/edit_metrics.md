# 人工改动比例

## 定义

指标表示 AI 版本被采用后，用户后来手工修改了多少。缓存保存在
`generation_records.human_edit_metrics`，原稿和人工版本正文仍只保存在
`chapter_versions`，不会把原文重复写入生成记录。

对状态为 `applied` 且 `accepted_version_id` 非空的生成记录：

1. 锚点 A 是 `accepted_version_id` 指向的不可变章节版本。
2. 从同一章节中版本号大于 A 的版本按升序扫描。遇到来源不属于 `manual` 或
   `finalize`、带 `run_id`、带 `rollback_from_version_id`，或被另一条 applied
   记录引用为锚点的版本时停止。
3. 停止前最后一个版本是人工终稿 H；没有这样的版本时 H 为空。
4. 只比较 A 与 H。多次编辑只看最后一个人工版本，中间版本不累计。

正文计数先去掉所有空白字符。编辑差异使用 `difflib-v1`：按换行段落对齐，只对
发生变化的段落调用现有 `calculate_edit_change_metrics` 字符级算法。

```text
edit_ratio = round(min(1.0, changed_chars / max(original_chars, 1)), 4)
edited = changed_chars > 0
```

`changed_chars` 对替换块计两侧长度的较大值，对删除或插入计变更侧长度；
`changed_original_chars` 只计被替换或删除的原文字符，`inserted_chars` 计新增字符。
`similarity` 是段落对齐后匹配字符的归一化相似度，保留四位小数。P90 使用
nearest-rank 定义，即升序数组中索引 `ceil(0.9 * n) - 1` 的值。

## 重要说明

- 这是“AI 采用后被改了多少”。用户自己新写的内容也会计入 `inserted_chars`。
- 刚采用、还没有手改的章节显示 0；这不代表用户满意，只代表尚未改动。
- 用户多次编辑只看最后一个人工版本，中间版本不累计。
- `accepted_version_id` 为空时不计算，指标保持 `null`。例如批准时正文与章节当前正文
  相同，保存服务不会创建新版本，因此没有可引用的锚点。
- `applied_at` 使用 `GenerationRecord.updated_at`。读时刷新指标缓存会显式保留这个时间，
  避免 SQLAlchemy 的 `onupdate` 把采用时间改成缓存计算时间。
- 列表、详情、原稿和统计读取会尽力刷新最多 20 条缓存；单条刷新接口可强制重算。
  超出批量额度的统计记录使用已有缓存，失败的重算保留原缓存并记录 warning。
- 锚点版本受 `generation_records.accepted_version_id` 引用时，版本剪枝会保留它。
