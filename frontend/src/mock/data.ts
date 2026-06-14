import type { Expert, Project, Chapter, OutlineItem, Character, CharacterRelation, WorldEntry, ReviewComment, HiddenThread } from '../api/types'

/** 6 个默认专家 */
export const DEFAULT_EXPERTS: Expert[] = [
  {
    id: 'creative',
    name: '创意大师',
    role: 'creative',
    role_type: 'writer',
    description: '构建独特的世界观、设计引人入胜的角色和情节',
    system_prompt: '你是一位才华横溢的创意大师，擅长构建独特的世界观、设计引人入胜的角色和情节。',
    temperature: 0.9,
    max_tokens: 4096,
    workflow_position: 'pre_writer',
    context_scope: { include_world: true, include_characters: true, include_outline: true, include_previous_chapters: 0 },
    trigger: 'manual',
    is_builtin: true,
    is_enabled: true,
    color: '#6366f1',
  },
  {
    id: 'cruel',
    name: '残酷大师',
    role: 'cruel',
    role_type: 'critic',
    description: '严苛的文学评论家，找出逻辑漏洞、角色扁平、情节拖沓',
    system_prompt: '你是一位以严苛著称的文学评论家，你的职责是找出作品中的逻辑漏洞、角色扁平、情节拖沓。',
    temperature: 0.3,
    max_tokens: 2048,
    workflow_position: 'post_critic',
    context_scope: { include_world: false, include_characters: false, include_outline: false, include_previous_chapters: 3 },
    trigger: 'auto_on_draft',
    is_builtin: true,
    is_enabled: true,
    color: '#ef4444',
  },
  {
    id: 'twist',
    name: '情节转折大师',
    role: 'twist',
    role_type: 'writer',
    description: '在平淡情节中埋下伏笔，设计合乎逻辑的转折',
    system_prompt: '你擅长在看似平淡的情节中埋下伏笔，设计出令人意想不到但合乎逻辑的转折。',
    temperature: 0.85,
    max_tokens: 4096,
    workflow_position: 'post_writer',
    context_scope: { include_world: true, include_characters: true, include_outline: true, include_previous_chapters: 5 },
    trigger: 'manual',
    is_builtin: true,
    is_enabled: true,
    color: '#f59e0b',
  },
  {
    id: 'renderer',
    name: '渲染大师',
    role: 'renderer',
    role_type: 'writer',
    description: '文笔精湛，擅长细腻描写场景、动作、对话和内心独白',
    system_prompt: '你是一位文笔精湛的小说家，擅长用细腻的笔触描写场景、动作、对话和内心独白。',
    temperature: 0.8,
    max_tokens: 8192,
    workflow_position: 'replace_writer',
    context_scope: { include_world: true, include_characters: true, include_outline: true, include_previous_chapters: 5 },
    trigger: 'manual',
    is_builtin: true,
    is_enabled: true,
    color: '#10b981',
  },
  {
    id: 'editor',
    name: '专业编辑',
    role: 'editor',
    role_type: 'editor',
    description: '关注叙事节奏、章节衔接、文字质量和读者体验',
    system_prompt: '你是一位经验丰富的小说编辑，关注叙事节奏、章节衔接、文字质量和读者体验。',
    temperature: 0.4,
    max_tokens: 4096,
    workflow_position: 'post_critic',
    context_scope: { include_world: false, include_characters: false, include_outline: true, include_previous_chapters: 3 },
    trigger: 'auto_on_draft',
    is_builtin: true,
    is_enabled: true,
    color: '#3b82f6',
  },
  {
    id: 'summarizer',
    name: '概括者',
    role: 'summarizer',
    role_type: 'researcher',
    description: '将长篇内容提炼为精炼摘要，保留核心情节和关键转折',
    system_prompt: '你擅长将长篇内容提炼为精炼的摘要，保留核心情节、角色发展和关键转折。',
    temperature: 0.3,
    max_tokens: 2048,
    workflow_position: 'standalone',
    context_scope: { include_world: false, include_characters: false, include_outline: false, include_previous_chapters: 10 },
    trigger: 'auto_on_chapter_complete',
    is_builtin: true,
    is_enabled: true,
    color: '#8b5cf6',
  },
]

/** Mock 项目列表 */
export const MOCK_PROJECTS: Project[] = [
  {
    id: 'proj-1',
    title: '星际迷途',
    genre: '科幻',
    style: '严肃',
    status: 'writing',
    mode: 'novel',
    description: '一部关于星际文明的硬科幻长篇',
    overall_outline: '林远追踪神秘信号，逐步发现外星建筑与人类文明起源之间的联系。',
    target_words: 200000,
    created_at: '2026-05-01T10:00:00Z',
    updated_at: '2026-05-14T08:30:00Z',
  },
  {
    id: 'proj-2',
    title: '雾城暗影',
    genre: '悬疑',
    style: '暗黑',
    status: 'draft',
    mode: 'article',
    description: 'AI 工具在悬疑创作中的实战指南',
    overall_outline: '',
    target_words: 30000,
    created_at: '2026-05-10T14:00:00Z',
    updated_at: '2026-05-13T16:00:00Z',
  },
  {
    id: 'proj-3',
    title: '山海经·新传',
    genre: '玄幻',
    style: '史诗',
    status: 'completed',
    mode: 'novel',
    description: '基于山海经的东方玄幻新编',
    overall_outline: '古老山海异兽在现代世界重新现身，主角在追查中重建人与神怪的秩序。',
    target_words: 150000,
    created_at: '2026-04-20T09:00:00Z',
    updated_at: '2026-05-12T20:00:00Z',
  },
]

/** Mock 审核意见 */
export const MOCK_REVIEW_COMMENTS: ReviewComment[] = [
  { id: 'rc-1', chapter_id: 'ch-1', expert_id: 'cruel', comment: '开篇节奏偏慢，建议更快引入信号的危险性暗示。', severity: 'warning' },
  { id: 'rc-2', chapter_id: 'ch-1', expert_id: 'editor', comment: '舷窗冷白光斑的描写不错，但后续内心独白可以更精炼。', severity: 'info' },
]

/** Mock 章节 */
export const MOCK_CHAPTERS: Chapter[] = [
  {
    id: 'ch-1',
    project_id: 'proj-1',
    chapter_num: 1,
    title: '启航',
    summary: '主角林远在空间站收到神秘信号，决定独自驾驶飞船前往未知星域。',
    draft: '晨光透过空间站的舷窗，在林远的脸上投下冷白色的光斑。他盯着通讯面板上那条不断跳动的信号——一组从未见过的频率，像是来自宇宙深处的低语。\n\n"又来了。"他低声说，手指悬在接收键上方。三周来，这条信号每晚准时出现，又准时消失，仿佛某种精密的钟表在虚空中运转。\n\n他决定不再等待。',
    final_text: '',
    status: 'reviewing',
    review_comment_ids: ['rc-1', 'rc-2'],
    review_round: 1,
  },
  {
    id: 'ch-2',
    project_id: 'proj-1',
    chapter_num: 2,
    title: '信号源',
    summary: '林远追踪信号到达一颗废弃行星，发现一座仍在运转的外星建筑。',
    draft: '',
    final_text: '',
    status: 'draft',
    review_comment_ids: [],
    review_round: 0,
  },
  {
    id: 'ch-3',
    project_id: 'proj-1',
    chapter_num: 3,
    title: '第一接触',
    summary: '林远进入外星建筑，发现一段记录着文明兴衰的全息影像。',
    draft: '',
    final_text: '',
    status: 'draft',
    review_comment_ids: [],
    review_round: 0,
  },
]

/** Mock 暗线 */
export const MOCK_HIDDEN_THREADS: HiddenThread[] = [
  { id: 'ht-1', project_id: 'proj-1', name: '外星文明的求救', description: '信号实际上是求救，而非威胁', chapter_nums: [1, 2], created_at: '2026-05-01T10:00:00Z', updated_at: '2026-05-14T08:30:00Z' },
  { id: 'ht-2', project_id: 'proj-1', name: '建筑中的守卫AI', description: '外星建筑内沉睡的守卫AI', chapter_nums: [2, 3], created_at: '2026-05-01T10:00:00Z', updated_at: '2026-05-14T08:30:00Z' },
]

/** Mock 大纲 */
export const MOCK_OUTLINE: OutlineItem[] = [
  { id: 'ol-1', project_id: 'proj-1', chapter_num: 1, title: '启航', summary: '主角收到神秘信号，决定前往未知星域', turning_point: '信号突然改变频率，暗示有意识', hidden_thread_ids: ['ht-1'] },
  { id: 'ol-2', project_id: 'proj-1', chapter_num: 2, title: '信号源', summary: '追踪信号到达废弃行星，发现外星建筑', turning_point: '建筑仍在运转，不是废墟', hidden_thread_ids: ['ht-1', 'ht-2'] },
  { id: 'ol-3', project_id: 'proj-1', chapter_num: 3, title: '第一接触', summary: '进入建筑，发现全息影像记录', turning_point: '影像中的文明与人类极其相似', hidden_thread_ids: ['ht-2'] },
  { id: 'ol-4', project_id: 'proj-1', chapter_num: 4, title: '抉择', summary: '守卫AI苏醒，要求林远做出选择', turning_point: '选择将影响两个文明的命运', hidden_thread_ids: [] },
  { id: 'ol-5', project_id: 'proj-1', chapter_num: 5, title: '归途', summary: '林远带着答案返回，但一切已不同', turning_point: '信号从未停止——它一直在等待回应', hidden_thread_ids: [] },
]

/** Mock 角色关系 */
export const MOCK_CHARACTER_RELATIONS: CharacterRelation[] = [
  { id: 'rel-1', project_id: 'proj-1', source_character_id: 'char-1', target_character_id: 'char-2', description: '从敌对到理解' },
  { id: 'rel-2', project_id: 'proj-1', source_character_id: 'char-1', target_character_id: 'char-3', description: '旧日同事，未解的牵挂' },
  { id: 'rel-3', project_id: 'proj-1', source_character_id: 'char-2', target_character_id: 'char-1', description: '从敌对到理解' },
  { id: 'rel-4', project_id: 'proj-1', source_character_id: 'char-3', target_character_id: 'char-1', description: '旧日同事，未解的牵挂' },
]

/** Mock 角色 */
export const MOCK_CHARACTERS: Character[] = [
  {
    id: 'char-1',
    project_id: 'proj-1',
    name: '林远',
    role_type: 'protagonist',
    scope_type: 'core',
    profile: '冷静、执着、略带孤独感。对未知有强烈好奇心，但习惯独处。从逃避联系到主动建立连接。',
    faction: '民间探险家',
    appearance_count: 5,
    metadata: null,
  },
  {
    id: 'char-2',
    project_id: 'proj-1',
    name: 'AI-7',
    role_type: 'supporting',
    scope_type: 'recurring',
    profile: '理性、守护、隐藏着创造者的情感记忆。从机械执行到觉醒自我意识。',
    faction: '外星建筑守卫',
    appearance_count: 3,
    metadata: null,
  },
  {
    id: 'char-3',
    project_id: 'proj-1',
    name: '陈薇',
    role_type: 'supporting',
    scope_type: 'chapter',
    profile: '温暖、敏锐、有领导力。在后方等待中重新审视与林远的关系。',
    faction: '联合国太空署',
    appearance_count: 2,
    metadata: null,
  },
]

/** Mock 世界观 */
export const MOCK_WORLD_ENTRIES: WorldEntry[] = [
  {
    id: 'ws-1',
    project_id: 'proj-1',
    title: '星际时代背景',
    category: '背景',
    scope_type: 'global',
    content: '2187年，人类已在太阳系建立多个空间站，但尚未实现星际航行。一艘改装的深空探测器意外截获了来自半人马座方向的规律信号，频率模式不符合任何已知自然现象。联合国太空署将信号列为最高机密，但消息还是泄露了出去。民间探险家开始私自追踪信号源……',
    rules: null,
    confidence: 'high',
    created_at: '2026-05-01T10:00:00Z',
    updated_at: '2026-05-14T08:30:00Z',
  },
]
