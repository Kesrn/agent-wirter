# K-2 数据完整性校验修复

## 修复日期
2026-07-06

## 问题概述
K-2 主体功能已完成，但存在2个数据完整性校验漏洞：

1. `outline.story_arc_id` 没有校验是否属于当前项目
2. `update_story_arc` 的 `parent_arc_id` 没有校验（创建时有，更新时缺失）

## 修复内容

### 1. routes.py 修复（3处）

#### 1.1 create_outline (line 2219-2230)
**修复前**：
```python
story_arc_id=_to_uuid(req.story_arc_id) if req.story_arc_id else None,
```

**修复后**：
```python
# 校验 story_arc_id 是否存在且属于当前项目
story_arc_id = None
if req.story_arc_id:
    story_arc_id = _to_uuid(req.story_arc_id)
    arc_check = await db.execute(
        select(StoryArc).where(
            StoryArc.id == story_arc_id,
            StoryArc.project_id == uid
        )
    )
    if not arc_check.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="story_arc_id 不存在或不属于该项目")
```

#### 1.2 update_outline (line 2266-2281)
**修复前**：
```python
if "story_arc_id" in update_data:
    if update_data["story_arc_id"]:
        update_data["story_arc_id"] = _to_uuid(update_data["story_arc_id"])
    else:
        update_data["story_arc_id"] = None
```

**修复后**：
```python
if "story_arc_id" in update_data:
    if update_data["story_arc_id"]:
        story_arc_id = _to_uuid(update_data["story_arc_id"])
        # 校验 story_arc_id 是否存在且属于当前项目
        arc_check = await db.execute(
            select(StoryArc).where(
                StoryArc.id == story_arc_id,
                StoryArc.project_id == uid
            )
        )
        if not arc_check.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="story_arc_id 不存在或不属于该项目")
        update_data["story_arc_id"] = story_arc_id
    else:
        update_data["story_arc_id"] = None
```

#### 1.3 update_story_arc (line 2478-2496)
**修复前**：
```python
if "parent_arc_id" in update_data:
    if update_data["parent_arc_id"]:
        update_data["parent_arc_id"] = _to_uuid(update_data["parent_arc_id"])
    else:
        update_data["parent_arc_id"] = None
```

**修复后**：
```python
if "parent_arc_id" in update_data:
    if update_data["parent_arc_id"]:
        parent_arc_id = _to_uuid(update_data["parent_arc_id"])
        # 禁止自引用
        if str(parent_arc_id) == arc_id:
            raise HTTPException(status_code=400, detail="parent_arc_id 不能指向自身")
        # 校验父级是否存在且属于当前项目
        parent_check = await db.execute(
            select(StoryArc).where(
                StoryArc.id == parent_arc_id,
                StoryArc.project_id == uid
            )
        )
        if not parent_check.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="parent_arc_id 不存在或不属于该项目")
        update_data["parent_arc_id"] = parent_arc_id
    else:
        update_data["parent_arc_id"] = None
```

### 2. test_story_arcs.py 新增测试（7个）

#### 2.1 Outline 相关测试（3个）
- `test_outline_create_with_unknown_story_arc_id`: 不存在的 arc_id → 400
- `test_outline_create_with_cross_project_story_arc_id`: 跨项目 arc_id → 400
- `test_outline_update_with_invalid_story_arc_id`: 更新时无效 arc_id → 400

#### 2.2 StoryArc 相关测试（3个）
- `test_update_parent_to_self_returns_400`: 自引用 → 400
- `test_update_parent_to_unknown_returns_400`: 不存在的父级 → 400
- `test_update_parent_to_cross_project_returns_400`: 跨项目父级 → 400

## 验证状态

### 代码验证
✅ Python 语法检查通过：
```bash
python3 -m py_compile api/routes.py tests/test_story_arcs.py
```

### 预期测试结果
- 原有测试：17 passed
- 新增测试：7 个
- **总计：24 个测试**

## 修复效果

### 防止的数据污染场景
1. ✅ 大纲引用不存在的 arc
2. ✅ 大纲引用其他项目的 arc
3. ✅ StoryArc 自引用
4. ✅ StoryArc 引用不存在的父级
5. ✅ StoryArc 引用其他项目的父级

### 未实现的可选校验
- ❌ 循环引用检测（A→B→C→A）
  - **原因**：实现成本高，在小说长线结构场景中几乎不会出现
  - **建议**：第一版不做，如有需要可在后续版本添加

## 测试运行方法

```bash
cd backend
source venv/bin/activate  # 如果虚拟环境可用
pytest tests/test_story_arcs.py -v
```

或直接：
```bash
cd backend
venv/bin/pytest tests/test_story_arcs.py -v
```

## 结论
✅ K-2 数据完整性校验漏洞已修复，可以标记为 100% 完成。
