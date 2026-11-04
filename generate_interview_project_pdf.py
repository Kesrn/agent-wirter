#!/usr/bin/env python3
"""Generate a polished, interview-oriented project experience PDF.

The content is intentionally grounded in the repository's current v2 workflow:
context -> optional clarification/task-card review -> architect -> writer ->
structural critic -> narrative editor -> continuity check -> human review.
"""

from __future__ import annotations

import os
from datetime import date

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(ROOT, "output", "pdf", "AI创作平台_面试项目经验手册.pdf")


def register_font() -> str:
    candidates = [
        ("PingFang", "/System/Library/Fonts/PingFang.ttc"),
        ("PingFang", "/System/Library/Fonts/Supplemental/PingFang.ttc"),
        ("Hiragino", "/System/Library/Fonts/Hiragino Sans GB.ttc"),
        ("Songti", "/System/Library/Fonts/Supplemental/Songti.ttc"),
    ]
    for name, path in candidates:
        if not os.path.exists(path):
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, path, subfontIndex=0))
            return name
        except Exception:
            pass
    return "Helvetica"


FONT = register_font()
INK = HexColor("#172033")
MUTED = HexColor("#64748B")
BLUE = HexColor("#1D4ED8")
TEAL = HexColor("#0F766E")
LINE = HexColor("#CBD5E1")
PALE_BLUE = HexColor("#EFF6FF")
PALE_TEAL = HexColor("#ECFDF5")
PALE_AMBER = HexColor("#FFFBEB")
PALE_SLATE = HexColor("#F8FAFC")


def make_styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle(
        "base", fontName=FONT, fontSize=9.3, leading=14.4,
        textColor=INK, alignment=TA_LEFT, spaceAfter=2.2 * mm,
    )
    return {
        "title": ParagraphStyle("title", parent=base, fontSize=25, leading=33,
                                textColor=BLUE, alignment=TA_CENTER, spaceAfter=2 * mm),
        "subtitle": ParagraphStyle("subtitle", parent=base, fontSize=11.5, leading=18,
                                   textColor=MUTED, alignment=TA_CENTER, spaceAfter=6 * mm),
        "h1": ParagraphStyle("h1", parent=base, fontSize=15.2, leading=23,
                              textColor=BLUE, spaceBefore=5.5 * mm, spaceAfter=2.5 * mm),
        "h2": ParagraphStyle("h2", parent=base, fontSize=11.3, leading=17,
                              textColor=TEAL, spaceBefore=3.5 * mm, spaceAfter=1.5 * mm),
        "body": base,
        "small": ParagraphStyle("small", parent=base, fontSize=7.9, leading=11.2, textColor=MUTED),
        "bullet": ParagraphStyle("bullet", parent=base, leftIndent=6.5 * mm,
                                 firstLineIndent=-3.8 * mm, spaceAfter=1.1 * mm),
        "quote": ParagraphStyle("quote", parent=base, fontSize=10.1, leading=16,
                                 textColor=INK, leftIndent=4 * mm, rightIndent=4 * mm),
        "code": ParagraphStyle("code", fontName="Courier", fontSize=7.6, leading=11.2,
                                textColor=HexColor("#334155"), backColor=PALE_SLATE,
                                leftIndent=4 * mm, rightIndent=4 * mm, borderPadding=4,
                                spaceBefore=1 * mm, spaceAfter=2 * mm),
        "center": ParagraphStyle("center", parent=base, alignment=TA_CENTER),
    }


S = make_styles()


def p(text: str, style: str = "body") -> Paragraph:
    return Paragraph(text, S[style])


def h1(text: str) -> Paragraph:
    return p(text, "h1")


def h2(text: str) -> Paragraph:
    return p(text, "h2")


def b(text: str) -> Paragraph:
    return p("• " + text, "bullet")


def gap(n: float = 3) -> Spacer:
    return Spacer(1, n * mm)


def table(headers: list[str], rows: list[list[str]], widths: list[float]) -> Table:
    data = [[p(x, "small") for x in headers]]
    data += [[p(x) for x in row] for row in rows]
    result = Table(data, colWidths=[x * mm for x in widths], repeatRows=1, hAlign="LEFT")
    result.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.35, LINE),
        ("BACKGROUND", (0, 0), (-1, 0), PALE_BLUE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    return result


def key_values(rows: list[tuple[str, str]]) -> Table:
    data = [[p(k, "small"), p(v)] for k, v in rows]
    result = Table(data, colWidths=[33 * mm, 127 * mm], hAlign="LEFT")
    result.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.35, LINE),
        ("BACKGROUND", (0, 0), (0, -1), PALE_SLATE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return result


def callout(title: str, body: str, color=PALE_TEAL) -> Table:
    result = Table([[p(f"<b>{title}</b><br/>{body}")]], colWidths=[160 * mm], hAlign="LEFT")
    result.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), color),
        ("BOX", (0, 0), (-1, -1), 0.35, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return result


def flow(labels: list[str]) -> Table:
    cells = []
    for i, label in enumerate(labels):
        cells.append(p(label, "center"))
        if i < len(labels) - 1:
            cells.append(p("→", "center"))
    # Fit the complete flow inside the 160 mm content width.  A six-step
    # business flow otherwise runs past the right margin on A4.
    arrow_width = 7
    node_width = (160 - arrow_width * (len(labels) - 1)) / len(labels)
    widths = [node_width if i % 2 == 0 else arrow_width for i in range(len(cells))]
    result = Table([cells], colWidths=[x * mm for x in widths], hAlign="LEFT")
    style = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("TEXTCOLOR", (1, 0), (-1, 0), MUTED),
    ]
    for index in range(0, len(cells), 2):
        style += [
            ("BACKGROUND", (index, 0), (index, 0), PALE_BLUE if index < len(cells) - 2 else PALE_TEAL),
            ("BOX", (index, 0), (index, 0), 0.35, LINE),
        ]
    result.setStyle(TableStyle(style))
    return result


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(BLUE)
    canvas.setLineWidth(1.2)
    canvas.line(18 * mm, A4[1] - 12 * mm, A4[0] - 18 * mm, A4[1] - 12 * mm)
    canvas.setFont(FONT, 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9 * mm, "AI 创作平台 · 面试项目经验手册")
    canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"第 {doc.page} 页")
    canvas.restoreState()


def build_story() -> list:
    story: list = []

    story += [gap(25), p("AI 创作平台", "title"), p("面试项目经验手册", "subtitle")]
    story.append(callout(
        "一句话定位",
        "一个面向小说与长文创作者的 AI 协作写作平台：把上下文、模型生成、审校、人工确认和版本留痕组织成可控的创作流程。",
        PALE_BLUE,
    ))
    story += [gap(7), key_values([
        ("建议应聘方向", "全栈开发 / AI 应用开发 / 后端开发。"),
        ("核心能力", "Vue 3 前端工作台、FastAPI 业务 API、LangGraph 工作流、SSE 流式交互、知识库/RAG、LLM 接入与可观测性。"),
        ("重点讲法", "不要说“做了很多功能”；聚焦“如何把一次模型调用升级为可控、可追溯的人机协同流程”。"),
        ("材料依据", "依据当前仓库实现和 workflow_v2：任务卡规划、结构化审稿、编辑、一致性检查与人工审核。"),
        ("生成日期", date.today().isoformat()),
    ]), gap(5)]
    story.append(table(["30 秒版本", "可直接说的内容"], [[
        "项目介绍",
        "我做了一个 AI 创作平台，服务小说和长文写作。它不仅调用大模型生成内容，还维护项目级的角色、世界观、大纲和资料库；生成前组装上下文，生成后经过结构审稿、一致性检查和人工确认，最终再写入正文与版本记录。",
    ]], [31, 129]))
    story.append(PageBreak())

    story.append(h1("一、项目背景与业务价值"))
    story.append(p("长篇创作的痛点不是“没有一段文本”，而是多次生成后角色设定、剧情节奏和信息揭示容易失控；同时用户希望能看到过程、介入决策，并在不满意时回退。项目把这些问题拆成可落地的产品能力。"))
    story.append(table(["业务问题", "产品设计", "面试可强调的价值"], [
        ["设定分散，模型易遗忘", "项目级资料库：角色、世界观、大纲、暗线、上传资料与结构化知识。", "让生成依赖可管理的项目上下文，而不是只靠一条 Prompt。"],
        ["直接生成不可控", "先由章节策划师输出任务卡，再由 writer 按任务卡写作；可选生成前澄清与任务卡审核。", "把“自由生成”变成“先规划、后执行”的流程。"],
        ["AI 结果缺乏质量保障", "结构审稿、叙事编辑、连续性检查分工处理；用户在最终写入前审批。", "实现 AI 质量控制与人机协同，而不是盲目信任模型。"],
        ["用户看不到等待过程", "后端以 SSE 推送节点状态、正文 token、审稿与错误事件。", "改善长耗时 AI 任务的可感知性和可恢复性。"],
        ["生成结果无法追溯", "记录 AI Run、运行步骤、调用日志、生成记录、章节版本和人工决策。", "定位问题、比较版本、复盘成本和质量。"],
    ], [37, 65, 58]))
    story.append(h2("简历表述（选 3-4 条即可）"))
    for item in [
        "设计并实现面向小说/长文创作的 AI 协作平台，覆盖项目、章节、资料库、生成、审校、审批与版本管理全链路。",
        "基于 LangGraph 设计章节生成工作流，将上下文加载、任务卡规划、正文生成、结构化审稿、编辑与一致性检查拆分为独立节点。",
        "使用 FastAPI + SSE 实现流式生成协议，前端按事件类型区分正文、过程日志、审校结果和人工中断状态。",
        "实现项目级知识库与结构化内容抽取，结合用户显式选择与相关性检索构造生成上下文。",
        "抽象 LLM Provider，支持 OpenAI 兼容模型接入、用户级配置和 API Key 加密存储。",
    ]:
        story.append(b(item))
    story.append(PageBreak())

    story.append(h1("二、系统架构与技术栈"))
    story.append(table(["层", "技术", "职责"], [
        ["前端", "Vue 3 + TypeScript + Vite + Pinia + Vue Router", "工作台、编辑器、素材管理、生成面板、审批弹窗、状态管理与路由守卫。"],
        ["通信", "REST + fetch + SSE", "CRUD 使用 REST；生成任务用 SSE 推送进度、token、节点产物、暂停与错误。"],
        ["后端", "FastAPI + Pydantic + SQLAlchemy Async", "认证、权限、参数校验、事务、生成路由、版本与运行记录。"],
        ["AI 编排", "LangGraph StateGraph + TypedDict", "显式描述节点、状态、条件路由、暂停/恢复和任务卡审核。"],
        ["模型与知识", "OpenAI 兼容 Provider + Embedding + Skill Pack", "多模型调用、项目知识加载、提示词组装、技能资料注入。"],
        ["数据与部署", "PostgreSQL / SQLite（桌面端）+ Alembic + Docker Compose + Electron", "持久化、数据库迁移、本地桌面端打包和服务启动。"],
    ], [24, 52, 84]))
    story.append(h2("高层链路"))
    story.append(flow(["创作工作台", "FastAPI API", "LangGraph", "LLM / 知识库", "版本与运行记录"]))
    story.append(h2("关键代码定位"))
    story.append(key_values([
        ("backend/api/routes.py", "生成接口：鉴权、项目归属校验、分支路由、SSE 事件封装、AI Run 与人工中断处理。"),
        ("backend/agents/workflow_v2.py", "v2 工作流状态与节点编排：任务卡、结构审稿、编辑、条件回路和 interrupt_before。"),
        ("backend/services/chapter_context.py", "按章节组装上文、角色、世界观、大纲、暗线与知识库上下文。"),
        ("backend/harness/*", "AI Run、步骤、LLM 调用日志、人工中断等可追溯能力。"),
        ("frontend/src/api/client.ts", "REST/SSE 客户端、鉴权 Header、错误转换和登录失效处理。"),
        ("frontend/src/components/AgentPanel.vue", "生成交互、事件消费、任务卡/审校/审批的用户侧呈现。"),
    ]))
    story.append(PageBreak())

    story.append(h1("三、核心业务逻辑梳理"))
    story.append(h2("1. 从用户操作到候选稿"))
    story.append(flow(["选择章节与上下文", "提交生成请求", "工作流生成", "SSE 展示", "人工确认", "保存版本"]))
    for item in [
        "用户在工作台选择章节、大纲、角色、世界观、目标字数和补充要求；也可以上传资料或维护结构化设定。",
        "后端先校验 JWT、项目所有权与章节归属，再读取用户级 LLM 配置，并创建 AI Run/步骤记录。",
        "ContextLoader 组装上下文：显式选择的素材按 ID 精确加载；未选择时可从知识库/相关资料中补充。",
        "v2 流程由章节策划师产出 ChapterTaskCard，writer 以任务卡为约束生成 draft，结构审稿与编辑进一步提高可控性。",
        "前端把正文、审稿意见、连续性报告、进度事件分流显示；候选稿未被用户采纳前不直接覆盖章节正文。",
        "最终由用户接受、拒绝或带反馈修订；采纳后才保存正文并形成版本/生成记录。",
    ]:
        story.append(b(item))
    story.append(h2("2. 生成模式的业务差异"))
    story.append(table(["模式", "适用场景", "关键行为"], [
        ["full_pipeline", "完整章节生成", "走完整工作流与人工审核，候选稿与审校信息分别处理。"],
        ["continue", "已有正文后的续写", "按续写方向快速生成后续内容；v2 续写图不经过完整审稿/人工暂停。"],
        ["enhance", "润色现有正文", "先给出可选改写方向，再根据用户选择执行改写。"],
        ["summarize", "读者视角反馈", "产出评价/建议，不修改原正文。"],
        ["article", "文章或文案项目", "走文章专属提示词与事件名，不进入小说章节工作流。"],
    ], [35, 42, 83]))
    story.append(PageBreak())

    story.append(h1("四、关键代码逻辑：怎么讲得像自己做的"))
    story.append(h2("A. LangGraph v2：先规划、再执行"))
    story.append(p("v2 的核心改动是将“章节规划”和“正文写作”拆开。chapter_architect 输出结构化任务卡，writer 只消费任务卡与上下文。这减少了 writer 同时承担规划、设定取舍与写作导致的不稳定。"))
    story.append(flow(["ContextLoader", "Architect", "Writer", "Structural Critic", "Editor"]))
    story.append(flow(["Continuity Check", "Human Review", "接受/修订/拒绝"]))
    story.append(p("工作流还支持按配置增加生成前澄清和任务卡审核：FAST 路径快速进入写作；PLANNING/STRICT 可先暂停，等用户回答澄清问题或修改任务卡再继续。"))
    story.append(h2("B. TypedDict 状态与节点边界"))
    story.append(p("CreativeStateV2 将 project_id、chapter_id、context、draft、chapter_task_card、structural_critique、edit_report、revision_count、skill_packs 等放进一个显式状态对象。每个节点只更新自己负责的字段，这使流程可测试、可暂停、可追踪，也避免大量参数在函数间传递。"))
    story.append(p("关键思路可以这样表述：<br/>“我没有把多 Agent 协作写成一串互相调用的函数，而是让节点通过共享状态通信。这样才能方便地插入人工审核、条件回路和后续的自定义 Agent。”", "quote"))
    story.append(h2("C. SSE 事件：把 AI 长任务变成可交互过程"))
    story.append(table(["事件类别", "示例", "前端处理原则"], [
        ["过程", "progress、agent_start、agent_done", "更新步骤与日志，不写入正文。"],
        ["正文", "writer_output / content_output", "仅正文进入候选稿或编辑器。"],
        ["质量", "critic_output、consistency_check、editor_output", "展示为审校信息，避免污染正文。"],
        ["交互", "task_card、clarification、human_review", "弹出对应确认界面并保留 run/thread 状态。"],
        ["收尾", "done、error", "停止 loading，保存运行状态或提示错误。"],
    ], [36, 49, 75]))
    story.append(h2("D. RAG 与显式选择的取舍"))
    story.append(p("用户主动选中的大纲、角色、世界观按 ID 加载，优先级最高；当用户没有选择或资料较多时，再按当前章节及查询计划补充相关知识。面试重点不是声称“RAG 能保证正确”，而是说明：显式选择提供确定性，检索提供便利性，两者结合避免上下文只靠黑盒召回。"))
    story.append(PageBreak())

    story.append(h1("五、面试问答（可直接复述）"))
    story.append(table(["问题", "回答要点"], [
        ["为什么不直接调一次大模型？", "单次调用适合 demo，但长篇写作需要上下文控制、过程可见、质量审校和用户决定是否落库。我把它做成状态工作流，换来可扩展和可追溯。"],
        ["为什么用 LangGraph？", "它适合表达有状态、有条件分支、可暂停恢复的流程。这里可以清楚描述任务卡审核、人工确认、修订回路，而不是在路由里塞一堆 if/else。"],
        ["任务卡解决了什么？", "先让 architect 输出本章目标、场景、冲突、字数预算、禁止揭示的信息；writer 再按任务卡写。它把“想清楚再写”产品化，也让用户能在生成前介入。"],
        ["SSE 断开如何处理？", "当前实现会保留已到前端的内容和运行记录；长期方案是把事件日志与 checkpoint 持久化，实现断线重连后的状态恢复和事件回放。"],
        ["如何避免审校内容混进正文？", "协议层区分 writer_output 与 critic/consistency/editor 事件，前端分流存储。候选正文和过程日志有不同状态，保存前仍需用户确认。"],
        ["API Key 如何处理？", "后端使用 Fernet 加密后再存库，对外不回传明文；生产环境还应使用独立 KMS/Secret Manager，并强制配置 JWT/加密密钥。"],
        ["项目最大难点是什么？", "不是模型调用本身，而是协调上下文、流式交互、人工决策、版本留痕和多步骤异常处理，让生成从黑盒变成可控业务流程。"],
        ["你做过什么取舍？", "小规模资料检索先采用应用层能力与结构化上下文，优先验证业务闭环；当规模增长，再将向量检索、重排序和持久化 checkpoint 下沉到基础设施。"],
        ["如果再做一次会怎么优化？", "拆分过大的 routes/workspace 模块；用 DB checkpoint 与任务队列承载长任务；为 RAG 引入 pgvector 索引和 reranker；补齐 E2E 与可观测指标。"],
    ], [47, 113]))
    story.append(h2("面试官追问“是不是做得太复杂”"))
    story.append(callout(
        "推荐回答",
        "我把复杂度集中在真正需要控制的生成链路上：任务卡、审核、版本和运行记录。普通 CRUD 保持常规 REST。实际产品化时会用埋点评估各环节价值，保留高价值节点，避免为了多 Agent 而多 Agent。",
        PALE_AMBER,
    ))
    story.append(PageBreak())

    story.append(h1("六、项目边界与诚实表达"))
    story.append(p("这一页很重要：面试中主动说清边界，比夸大效果更显得可信。"))
    story.append(table(["当前边界", "如何诚实说明", "下一步"], [
        ["核心 API 路由和工作台组件较大", "迭代阶段把主链路优先跑通，已经识别出模块职责偏重。", "按领域拆分路由、composable、store 与测试。"],
        ["部分流程使用内存 checkpointer", "单进程运行可支持当前会话内中断恢复，但服务重启后需恢复策略。", "接入数据库持久化 checkpoint 和事件回放。"],
        ["RAG 仍面向小规模素材", "显式选择与上下文组装已能解决核心体验，复杂检索是后续优化点。", "pgvector 索引、混合检索、reranker、评测集。"],
        ["模型输出不完全可靠", "通过 JSON 容错、结构审稿、连续性检查和用户审批降低风险，但不承诺模型绝对正确。", "引入评测基准、结构化校验与自动回归。"],
        ["当前测试环境需保证依赖完整", "前端构建/测试已覆盖主要交互；后端应使用隔离环境安装 requirements 后运行完整测试。", "锁定依赖版本并在 CI 中执行测试与迁移检查。"],
    ], [44, 57, 59]))
    story.append(h2("最后的 1 分钟收尾"))
    story.append(p("这个项目最能体现的不是“我会写 Prompt”，而是我能把 LLM 放进一个有业务约束的产品里：先用项目知识和任务卡约束输入，再以流式交互和人工审核控制过程，最后通过版本、运行记录和模型调用日志让结果可追溯。"))
    story.append(h2("现场演示顺序（如面试允许）"))
    for item in [
        "创建或打开一个项目，展示章节、角色、世界观、大纲等资料如何被组织。",
        "从工作台触发生成，展示流式进度与正文/审校信息分流。",
        "展示任务卡或人工审核，说明用户为何能在落库前介入。",
        "展示版本历史、生成记录或运行步骤，收尾到“可追溯”。",
    ]:
        story.append(b(item))
    return story


def build_pdf() -> str:
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    doc = SimpleDocTemplate(
        OUTPUT, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title="AI 创作平台面试项目经验手册", author="Codex",
    )
    doc.build(build_story(), onFirstPage=footer, onLaterPages=footer)
    return OUTPUT


if __name__ == "__main__":
    print(build_pdf())
