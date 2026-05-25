#!/usr/bin/env python3
"""Generate an interview-oriented project experience PDF for this repository."""

from __future__ import annotations

import os
from datetime import date

from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(ROOT, "AI创作平台_项目经验讲解版.pdf")


def register_font() -> str:
    """Register a Chinese-capable font on macOS, with a Helvetica fallback."""
    candidates = [
        ("PingFang", "/System/Library/Fonts/PingFang.ttc"),
        ("PingFang", "/System/Library/Fonts/Supplemental/PingFang.ttc"),
        ("Songti", "/System/Library/Fonts/Supplemental/Songti.ttc"),
        ("Hiragino", "/System/Library/Fonts/Hiragino Sans GB.ttc"),
    ]
    for name, path in candidates:
        if not os.path.exists(path):
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, path, subfontIndex=0))
            return name
        except Exception:
            continue
    return "Helvetica"


FONT = register_font()
PRIMARY = HexColor("#1D4ED8")
ACCENT = HexColor("#0F766E")
INK = HexColor("#111827")
MUTED = HexColor("#64748B")
SOFT = HexColor("#F8FAFC")
LINE = HexColor("#CBD5E1")
PALE_BLUE = HexColor("#EFF6FF")
PALE_GREEN = HexColor("#ECFDF5")
PALE_AMBER = HexColor("#FFFBEB")


def styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle(
        "Base",
        fontName=FONT,
        fontSize=9.4,
        leading=15,
        textColor=INK,
        alignment=TA_LEFT,
        spaceAfter=2.2 * mm,
    )
    return {
        "title": ParagraphStyle(
            "Title",
            parent=base,
            fontSize=25,
            leading=34,
            textColor=PRIMARY,
            alignment=TA_CENTER,
            spaceAfter=2 * mm,
        ),
        "subtitle": ParagraphStyle(
            "Subtitle",
            parent=base,
            fontSize=12,
            leading=18,
            textColor=MUTED,
            alignment=TA_CENTER,
            spaceAfter=6 * mm,
        ),
        "h1": ParagraphStyle(
            "H1",
            parent=base,
            fontSize=15.5,
            leading=23,
            textColor=PRIMARY,
            spaceBefore=7 * mm,
            spaceAfter=3 * mm,
        ),
        "h2": ParagraphStyle(
            "H2",
            parent=base,
            fontSize=11.5,
            leading=18,
            textColor=ACCENT,
            spaceBefore=4 * mm,
            spaceAfter=1.8 * mm,
        ),
        "body": base,
        "bullet": ParagraphStyle(
            "Bullet",
            parent=base,
            leftIndent=7 * mm,
            firstLineIndent=-4 * mm,
            spaceAfter=1.2 * mm,
        ),
        "small": ParagraphStyle(
            "Small",
            parent=base,
            fontSize=8,
            leading=12,
            textColor=MUTED,
        ),
        "code": ParagraphStyle(
            "Code",
            fontName="Courier",
            fontSize=8,
            leading=12,
            textColor=HexColor("#334155"),
            backColor=SOFT,
            leftIndent=5 * mm,
            rightIndent=5 * mm,
            borderPadding=4,
            spaceBefore=1 * mm,
            spaceAfter=2.5 * mm,
        ),
        "tag": ParagraphStyle(
            "Tag",
            parent=base,
            fontSize=8,
            leading=12,
            textColor=PRIMARY,
            alignment=TA_CENTER,
        ),
        "quote": ParagraphStyle(
            "Quote",
            parent=base,
            fontSize=10,
            leading=16,
            textColor=HexColor("#0F172A"),
            leftIndent=5 * mm,
            rightIndent=5 * mm,
            backColor=PALE_BLUE,
            borderPadding=6,
        ),
    }


S = styles()


def p(text: str, style: str = "body") -> Paragraph:
    return Paragraph(text, S[style])


def h1(text: str) -> Paragraph:
    return p(text, "h1")


def h2(text: str) -> Paragraph:
    return p(text, "h2")


def bullet(text: str) -> Paragraph:
    return p(f"· {text}", "bullet")


def gap(size: float = 3) -> Spacer:
    return Spacer(1, size * mm)


def kv_table(rows: list[tuple[str, str]], widths: tuple[float, float] = (34, 126)) -> Table:
    data = [[p(k, "small"), p(v)] for k, v in rows]
    table = Table(data, colWidths=[widths[0] * mm, widths[1] * mm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, LINE),
                ("BACKGROUND", (0, 0), (0, -1), SOFT),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def matrix_table(headers: list[str], rows: list[list[str]], widths: list[float]) -> Table:
    data = [[p(h, "small") for h in headers]]
    data.extend([[p(cell) for cell in row] for row in rows])
    table = Table(data, colWidths=[w * mm for w in widths], hAlign="LEFT", repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, LINE),
                ("BACKGROUND", (0, 0), (-1, 0), PALE_BLUE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def tag_row(tags: list[str]) -> Table:
    table = Table([[p(tag, "tag") for tag in tags]], colWidths=[23 * mm] * len(tags))
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PALE_BLUE),
                ("BOX", (0, 0), (-1, -1), 0.4, HexColor("#BFDBFE")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def callout(title: str, body: str, bg=PALE_GREEN) -> Table:
    data = [[p(f"<b>{title}</b><br/>{body}")]]
    table = Table(data, colWidths=[160 * mm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), bg),
                ("BOX", (0, 0), (-1, -1), 0.4, LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
            ]
        )
    )
    return table


def on_page(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(PRIMARY)
    canvas.setLineWidth(1.4)
    canvas.line(18 * mm, A4[1] - 12 * mm, A4[0] - 18 * mm, A4[1] - 12 * mm)
    canvas.setFont(FONT, 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9 * mm, "AI 创作平台项目经验讲解版")
    canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"第 {doc.page} 页")
    canvas.restoreState()


def build_story() -> list:
    story: list = []

    story.append(gap(28))
    story.append(p("AI 创作平台", "title"))
    story.append(p("项目经验讲解版 / 面试复盘材料", "subtitle"))
    story.append(
        p(
            "基于 Vue 3、FastAPI、LangGraph、SSE 和 RAG 的多 Agent 智能写作系统",
            "quote",
        )
    )
    story.append(gap(8))
    story.append(
        kv_table(
            [
                ("项目定位", "面向小说/长文创作者的 AI 辅助创作平台，支持素材管理、章节编辑、多 Agent 生成、润色、续写、读者反馈。"),
                ("推荐角色描述", "全栈开发 / AI 应用开发：负责前后端核心链路、LangGraph 工作流、SSE 流式生成、RAG 上下文注入和 LLM 配置。"),
                ("技术栈", "Vue 3 + TypeScript + Pinia + Vue Router；FastAPI + SQLAlchemy Async + PostgreSQL；LangGraph；JWT；Docker Compose。"),
                ("核心亮点", "多 Agent 编排、RAG 双路径上下文、SSE token 级输出、人机协同审批、多厂商 LLM 适配、API Key 加密存储。"),
                ("生成日期", date.today().isoformat()),
            ]
        )
    )
    story.append(gap(6))
    story.append(tag_row(["LangGraph", "RAG", "SSE", "FastAPI", "Vue 3", "PostgreSQL"]))
    story.append(PageBreak())

    story.append(h1("一、30 秒讲清楚这个项目"))
    story.append(
        p(
            "这是一个 AI 小说/长文创作平台。用户可以创建创作项目，维护章节、大纲、角色、世界观和暗线等素材；写作时可以让 AI 以多个 Agent 的形式协作，例如先加载上下文，再由写作 Agent 生成内容，同时由审校 Agent 和一致性检查 Agent 给出反馈，最后交给用户确认是否写入章节。"
        )
    )
    story.append(
        p(
            "项目的重点不是简单调用一次大模型，而是把 AI 写作拆成可编排、可观察、可人工介入的工程流程：前端用 SSE 实时展示生成过程，后端用 LangGraph 管理多节点状态流转，用 RAG 把项目素材注入 prompt，并支持用户按项目配置不同的大模型。"
        )
    )
    story.append(h2("可以放到简历里的版本"))
    for item in [
        "设计并实现 AI 创作平台，支持项目、章节、角色、世界观、大纲、暗线等创作素材管理。",
        "基于 LangGraph 构建多 Agent 写作流水线，实现上下文加载、章节生成、审校、一致性检查和人工审批。",
        "通过 SSE 实现 token 级流式输出，前端可实时展示 AI 生成过程，并按生成/润色/续写模式差异化处理结果。",
        "实现 RAG 上下文注入：用户主动选择素材时按 ID 精确加载，否则基于 embedding 做相关素材检索。",
        "抽象 LLM Provider，兼容 OpenAI 协议模型；支持用户级 LLM 配置和 API Key 加密存储。",
    ]:
        story.append(bullet(item))

    story.append(h2("两分钟展开讲法"))
    story.append(
        p(
            "我把系统分成四层：前端工作台、后端业务 API、AI 工作流层和数据/RAG 层。前端负责编辑器、素材选择和 Agent 面板；后端负责认证、CRUD、SSE 输出和生成流程分流；AI 层用 LangGraph 表达多 Agent 协作；数据层用 PostgreSQL 保存项目素材、用户配置和章节内容。"
        )
    )
    story.append(
        p(
            "其中最值得讲的是生成链路。用户点击“章节生成”后，会先选择大纲、角色、世界观和目标字数。后端拿到这些选择后构造 CreativeState，context_loader 节点加载素材，writer 节点生成小说正文，critic 与 consistency_checker 并行做审校和一致性检查。最后 human_review 暂停，让用户接受、修改后接受或拒绝。"
        )
    )
    story.append(PageBreak())

    story.append(h1("二、系统架构"))
    story.append(
        matrix_table(
            ["层级", "主要模块", "职责"],
            [
                ["前端表现层", "Vue 3、TypeScript、Pinia、Vue Router", "登录、项目列表、写作工作台、素材管理、Agent 配置、LLM 设置。"],
                ["通信层", "REST API + SSE", "普通 CRUD 走 REST；AI 生成走 SSE，按事件类型推送进度、token、审校意见和错误。"],
                ["业务 API 层", "FastAPI、Pydantic、SQLAlchemy Async", "认证、权限校验、项目归属校验、参数校验、数据库读写、导出。"],
                ["AI 编排层", "LangGraph StateGraph", "把上下文加载、写作、审校、一致性检查和人工审批组织为状态图。"],
                ["RAG/模型层", "Embedding、ContextLoader、LLM Provider", "加载创作素材，生成上下文 prompt，调用 mock 或 OpenAI 兼容模型。"],
                ["数据层", "PostgreSQL、Alembic、Docker Compose", "保存用户、项目、章节、角色、世界观、大纲、暗线、专家和 LLM 配置。"],
            ],
            [28, 50, 82],
        )
    )
    story.append(h2("核心目录说明"))
    story.append(
        kv_table(
            [
                ("backend/main.py", "FastAPI 应用入口，挂载 CORS、中间件和路由，启动时初始化数据库。"),
                ("backend/api/routes.py", "核心业务路由：项目/章节/素材/专家 CRUD、章节生成、工作流恢复、导出。"),
                ("backend/agents/workflow.py", "LangGraph 工作流定义，包含 CreativeState、默认节点和动态图构建。"),
                ("backend/agents/llm_provider.py", "LLM 抽象层，MockProvider 与 OpenAIProvider 统一 generate / generate_stream 接口。"),
                ("backend/rag/context_loader.py", "上下文加载和向量相似度检索。"),
                ("frontend/src/views/Workspace.vue", "主工作台，整合编辑器、章节列表、素材面板和 Agent 面板。"),
                ("frontend/src/components/AgentPanel.vue", "生成入口和 SSE 事件处理核心。"),
                ("frontend/src/api/client.ts", "REST 与 SSE 请求封装，包含鉴权 header 和错误解析。"),
            ]
        )
    )
    story.append(h2("数据模型"))
    story.append(
        matrix_table(
            ["模型", "用途", "关键字段"],
            [
                ["Project", "创作项目", "title、description、target_words、mode、owner_id"],
                ["Chapter", "章节", "project_id、title、content、outline、sequence_number、word_count、status"],
                ["WorldEntry", "世界观条目", "category、title、content、rules、confidence、embedding"],
                ["Character", "角色", "name、role_type、profile、faction、metadata、embedding"],
                ["Outline", "大纲", "sequence_number、title、summary、turning_point、hidden_thread_ids"],
                ["HiddenThread", "暗线/伏笔", "name、description、chapter_nums"],
                ["Expert", "Agent 配置", "role_type、system_prompt、workflow_position、temperature、max_tokens"],
                ["LLMConfig", "用户模型配置", "provider、encrypted_api_key、base_url、model_id"],
            ],
            [30, 48, 82],
        )
    )
    story.append(PageBreak())

    story.append(h1("三、核心生成流程"))
    story.append(h2("章节生成：full_pipeline"))
    for item in [
        "前端打开 ContextPicker，让用户选择大纲、角色、世界观，并填写目标字数。",
        "前端调用 POST /api/projects/{project_id}/chapters/generate，mode=full_pipeline，并建立 SSE 连接。",
        "后端校验 JWT、项目归属、章节是否存在，再读取用户 LLM 配置。",
        "查询项目启用的 Expert，动态构建 LangGraph 工作流。",
        "context_loader 加载上下文：有选中素材就按 ID 精确加载；没有选中素材则走 RAG 检索。",
        "writer 生成章节草稿；critic 和 consistency_checker 分别审校与一致性检查。",
        "前端接收 writer_output，把纯小说内容放进 finalDraft；审校和一致性报告只进入输出日志。",
        "done 后前端弹出 ApprovalModal，用户接受后写入章节编辑器并保存。",
    ]:
        story.append(bullet(item))

    story.append(h2("润色：enhance"))
    story.append(
        p(
            "润色被设计成两步：第一次请求不传 enhance_direction，后端只分析当前章节并返回 3 个润色方向；用户选择方向并补充要求后，第二次请求才真正改写全文。这样避免 AI 盲目润色，也让用户对改写目标有控制权。"
        )
    )
    story.append(h2("续写：continue"))
    story.append(
        p(
            "续写同样是两步：先返回 5 个情节转折建议，再按用户选中的方向续写。与润色不同，续写 token 会直接追加到编辑器当前正文末尾，所以用户看到的是实时接龙式创作。"
        )
    )
    story.append(h2("读者视角：summarize"))
    story.append(
        p(
            "summarize 模式不改正文，只让模型以普通读者身份评价章节的吸引力、节奏、角色立体感和情节合理性。它本质上是反馈型 Agent。"
        )
    )

    story.append(h2("SSE 事件设计"))
    story.append(
        matrix_table(
            ["事件", "含义", "前端处理"],
            [
                ["progress", "系统进度", "追加到输出区。"],
                ["agent_start / agent_done", "节点开始/结束", "更新工作流状态。"],
                ["writer_output", "小说正文 token 或完整内容", "按模式写入 finalDraft 或直接追加编辑器。"],
                ["critic_output", "审校意见", "展示在输出日志，不污染正文。"],
                ["consistency_check", "一致性报告", "展示在输出日志，不污染正文。"],
                ["enhance_directions", "润色方向", "弹出 EnhancePicker。"],
                ["turn_suggestions", "续写方向", "弹出 TurnPicker。"],
                ["done / error", "完成或失败", "停止生成状态，显示审批或错误提示。"],
            ],
            [36, 54, 70],
        )
    )
    story.append(PageBreak())

    story.append(h1("四、技术亮点怎么讲"))
    story.append(h2("1. 为什么用 LangGraph"))
    story.append(
        p(
            "这个项目的 AI 流程不是一次模型调用，而是一个多步骤工作流。LangGraph 适合表达这种“先加载上下文、再写作、再并行审校、最后人工审批”的状态图。每个节点只读写 CreativeState 里自己负责的字段，流程可视化、可暂停，也方便之后插入自定义 Agent。"
        )
    )
    story.append(h2("2. 并行审校的价值"))
    story.append(
        p(
            "writer 产出草稿后，critic 和 consistency_checker 都只依赖草稿，不互相依赖，所以可以并行执行。串行时总耗时约等于两个模型调用相加，并行后约等于较慢的那个调用，理论上能明显降低等待时间。"
        )
    )
    story.append(h2("3. RAG 双路径上下文"))
    story.append(
        p(
            "用户主动选择素材时，系统按 ID 精确加载完整大纲、角色和世界观，确保用户选择的内容不丢；用户没有选择时，系统基于当前章节内容做 embedding 检索，自动找相关角色和世界观。两条路径互斥，避免重复和上下文污染。"
        )
    )
    story.append(
        callout(
            "当前实现边界",
            "仓库使用 pgvector 镜像作为数据库环境，但 MVP 代码里 embedding 字段是 Float 数组，检索是在 Python 内存中计算余弦相似度。这个设计适合小规模项目素材，后续可迁移到 pgvector 向量列和数据库索引。",
            PALE_AMBER,
        )
    )
    story.append(h2("4. 流式输出与正文纯净性"))
    story.append(
        p(
            "生成流程会产生正文、日志、审校意见、一致性报告等多种信息。前端把 writer_output 和其他事件严格分开：正文进入 finalDraft 或编辑器，审校与报告只进入 streamOutput。这样审批弹窗和最终保存的内容始终是纯小说正文。"
        )
    )
    story.append(h2("5. 多厂商 LLM 适配"))
    story.append(
        p(
            "LLMProvider 定义 generate 和 generate_stream 两个接口。MockProvider 便于无 Key 开发测试；OpenAIProvider 使用 OpenAI 兼容协议，DeepSeek、通义千问、智谱、Moonshot 等只需要配置不同 base_url 和 model。用户 API Key 用 Fernet 加密后存数据库。"
        )
    )
    story.append(PageBreak())

    story.append(h1("五、前端实现重点"))
    story.append(h2("工作台组织"))
    story.append(
        p(
            "Workspace.vue 是主工作区，进入项目后会加载章节、世界观、角色、角色关系、大纲和暗线。NovelEditor 负责正文编辑和自动保存，AgentPanel 负责 AI 生成入口与 SSE 事件消费。"
        )
    )
    story.append(h2("状态管理"))
    story.append(
        kv_table(
            [
                ("useProjectStore", "项目列表、当前项目、创建项目。"),
                ("useChapterStore", "章节列表、当前章节、草稿更新、保存。"),
                ("useExpertStore", "Agent 列表、生成状态、流式日志、finalDraft、工作流步骤。"),
                ("useWorldEntryStore / useCharacterStore / useOutlineStore", "素材 CRUD 与按项目分组。"),
                ("useLLMSettingsStore", "加载/保存 LLM 配置、获取模型列表。"),
                ("useUiStore", "Toast 通知。"),
            ]
        )
    )
    story.append(h2("三种生成模式的前端差异"))
    story.append(
        matrix_table(
            ["模式", "用户交互", "writer_output 去向", "完成后动作"],
            [
                ["full_pipeline", "选择上下文和目标字数", "累积到 finalDraft", "弹出审批，接受后写入章节。"],
                ["enhance", "先选润色方向", "累积到 finalDraft", "完成后替换当前章节内容并保存。"],
                ["continue", "先选转折方向", "直接追加到章节 draft", "实时写入编辑器，无需额外应用。"],
                ["summarize", "一键读者反馈", "展示为反馈文本", "不修改正文。"],
            ],
            [28, 42, 46, 44],
        )
    )

    story.append(h1("六、后端实现重点"))
    story.append(h2("认证与权限"))
    for item in [
        "注册时用 bcrypt 哈希密码；登录后签发 JWT，前端保存在 localStorage。",
        "所有项目资源请求都通过 get_current_user 校验登录状态。",
        "访问项目内资源前调用 _verify_project_owner，确保用户只能操作自己的项目。",
        "生成和认证相关接口有简单限流器，降低误触或滥用带来的成本风险。",
    ]:
        story.append(bullet(item))
    story.append(h2("数据一致性"))
    for item in [
        "创建项目后自动写入 6 个内置 Expert 模板，保证新项目开箱即用。",
        "创建章节时校验同一项目内 sequence_number 不重复。",
        "世界观和角色变更后可后台更新 embedding，失败不阻塞主流程。",
        "导出接口按章节 sequence_number 排序输出 txt 或 md。",
    ]:
        story.append(bullet(item))
    story.append(PageBreak())

    story.append(h1("七、面试高频问答"))
    qa_rows = [
        [
            "Q1：项目最大难点是什么？",
            "不是调通模型，而是把生成过程工程化：要区分正文与审校日志，要支持流式输出，要能让用户控制上下文和生成方向，还要保证用户资源隔离。",
        ],
        [
            "Q2：为什么不用普通串行函数？",
            "串行函数可以跑 MVP，但难以表达并行审校、人工暂停和动态插入 Agent。LangGraph 把流程状态显式化，更适合扩展。",
        ],
        [
            "Q3：RAG 怎么保证不乱引用？",
            "用户选择素材时不走召回，直接按 ID 加载完整内容；只有没有选择时才做相似度检索。主动选择优先级最高。",
        ],
        [
            "Q4：SSE 断开怎么办？",
            "前端用 AbortController 取消请求，已写入编辑器的 token 保留；当前实现没有完整的跨连接重放，后续可用持久化 checkpoint 和事件日志增强。",
        ],
        [
            "Q5：API Key 怎么保护？",
            "后端只保存 Fernet 加密后的密文，对外只返回 api_key_set 布尔值；生产环境还应把密钥管理迁移到 KMS。",
        ],
        [
            "Q6：项目有什么可改进？",
            "把 MemorySaver 换成数据库 checkpoint；RAG 从 Python 内存相似度升级到 pgvector 索引；加入 reranker；完善 E2E 测试和生成任务队列。",
        ],
    ]
    story.append(matrix_table(["问题", "回答要点"], qa_rows, [48, 112]))

    story.append(h1("八、诚实讲项目边界"))
    for item in [
        "当前 LLM 默认 mock，因此无需 API Key 可以演示流程；真实模型需要在设置页配置 provider、base_url、model 和 API Key。",
        "LangGraph checkpoint 当前使用 MemorySaver，服务重启后暂停状态会丢；代码里预留了数据库 checkpoint 开关，但当前 get_creative_app 仍是内存实现。",
        "RAG 在小规模素材下用 Python 余弦相似度足够，生产大规模场景应改为 pgvector 索引、混合检索和重排序。",
        "前端 store 初始化保留 mock 数据作为兜底，后端不可用时仍能预览 UI；正式环境应明确关闭或隔离 mock fallback。",
    ]:
        story.append(bullet(item))

    story.append(h2("启动方式"))
    story.append(p("docker compose up -d", "code"))
    story.append(p("cd backend && pip install -r requirements.txt && uvicorn main:app --reload", "code"))
    story.append(p("cd frontend && npm install && npm run dev", "code"))
    story.append(gap(8))
    story.append(
        callout(
            "一句话收尾",
            "这个项目可以重点体现：我能把大模型能力落到真实产品流程里，处理上下文、流式交互、状态编排、用户配置、安全存储和前后端工程化，而不是停留在简单 prompt 调用。",
            PALE_BLUE,
        )
    )
    return story


def build_pdf() -> str:
    doc = SimpleDocTemplate(
        OUTPUT,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="AI 创作平台项目经验讲解版",
        author="Codex",
    )
    doc.build(build_story(), onFirstPage=on_page, onLaterPages=on_page)
    return OUTPUT


if __name__ == "__main__":
    print(build_pdf())
