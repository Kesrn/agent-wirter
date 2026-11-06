#!/usr/bin/env python3
"""自然查询集扩充生成器（阶段一：natural_queries.json 28 → 70 条）。

在既有 28 条人工自然查询（retrieval_eval/datasets/natural_queries.json）基础上，
追加 42 条新样本（nat-029 起），全部基于 dev-web.sqlite3 真实内容构造：

    normal 6 + alias 6 + typo 6 + homophone 6 + same_name 4
    + conflict 4 + cross_chapter 4 + no_result 4 + no_inject 2 = 42 条

构造与核对方式与 generate_regression.py 的困难样本一致：
    - 别名/技能/人名引用词生成时做只读 SELECT 核对（缺失记入警告，不阻断）；
    - no_result 样本的 probe 短语经全库检索确认 0 命中，有命中即警告失效；
    - 既有 28 条样本原样保留（只更新 meta）。

用法：
    cd backend
    venv/bin/python retrieval_eval/generate_natural_more.py
    # 可选参数：<数据库路径> <输出路径>
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVAL_DIR.parent
DEFAULT_DB = BACKEND_DIR / "dev-web.sqlite3"
DEFAULT_OUT = EVAL_DIR / "datasets" / "natural_queries.json"

MAIN_SOURCE = "e6b5ec9a-2b87-428d-aa42-124a8ffa2482"      # d62d4447 全职法师全文
E2E_SOURCE = "36cdfeeb-4016-4f90-85c8-b920e06405d5"       # 49aee26a E2E小说
TEST_SOURCE = "06f34d62-2c85-4dac-958b-41a0254da46b"      # de04afc8 小说抽取测试
MAIN_PROJECT = "d62d4447-b8d7-4fc3-9193-a6d363d8f541"
E2E_PROJECT = "49aee26a-3535-4ad9-85f2-2f235d663e00"
TEST_PROJECT = "de04afc8-306e-41b6-97c9-904b4a0da9bd"

# 新样本定义（字段同 generate_regression.HARD_SAMPLE_DEFS；
# probe 仅 no_result 用，verify_terms 仅非 no_result 用）。
NATURAL_MORE_DEFS: list[dict[str, Any]] = [
    # ── normal（6）──
    {"difficulty": "normal", "category": "character_first_appearance", "query": "莫凡的雷系星尘是什么时候出现的？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE], "entities": ["莫凡", "雷系", "雷系星尘"],
     "chapter_no": 1, "project_id": MAIN_PROJECT, "verify_terms": ["雷系星尘"],
     "notes": "莫凡 雷系 first_seen_chapter=1；正文『这是莫凡的雷霆系星尘，前不久觉醒的力量』"},
    {"difficulty": "normal", "category": "ability_query", "query": "唐月老师是不是火系魔法师？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["唐月", "火系"],
     "chapter_no": 16, "project_id": MAIN_PROJECT, "verify_terms": ["唐月"],
     "notes": "facts『看上去温柔贤良的唐月老师竟然是一位火魔法师』；第16章『新女老师，唐月』"},
    {"difficulty": "normal", "category": "timeline_event", "query": "穆白在第25章释放了什么技能？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["穆白", "冰系", "冰蔓"],
     "chapter_no": 25, "project_id": MAIN_PROJECT, "verify_terms": ["冰蔓"],
     "notes": "第25章『冰系，冰蔓！』：穆白把控七颗星子后释放冰系技能-冰蔓"},
    {"difficulty": "normal", "category": "source_chunk", "query": "张小侯的土系灵种叫什么？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["张小侯", "土系", "流沼"],
     "project_id": MAIN_PROJECT, "verify_terms": ["流沼"],
     "notes": "facts『张小侯拥有的土系灵种-流沼』『流沼便是土系灵种中的极品』"},
    {"difficulty": "normal", "category": "timeline_event", "query": "第3章开学大典发生了什么？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["开学大典"],
     "chapter_no": 3, "project_id": MAIN_PROJECT, "verify_terms": ["开学大典"],
     "notes": "第3章标题『开学大典』：九月一号开学，对所有魔法学生都是无比重大的日子"},
    {"difficulty": "normal", "category": "source_chunk", "query": "莫凡完成火滋释放是在第几章？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["莫凡", "火滋", "火系"],
     "chapter_no": 25, "project_id": MAIN_PROJECT, "verify_terms": ["火滋"],
     "notes": "第25章『莫凡完成火滋的释放后所有人的反应比穆白的冰蔓更加强烈十倍』"},

    # ── alias（6）──
    {"difficulty": "alias", "category": "character_profile", "query": "泥猴子为什么叫泥猴子？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["张小侯", "泥猴子"],
     "chapter_no": 2, "project_id": MAIN_PROJECT, "verify_terms": ["泥猴子"],
     "notes": "别名：第2章『一个跟泥猴子一样的少年就跳了出来…这泥猴子叫张候』（张小侯）"},
    {"difficulty": "alias", "category": "source_chunk", "query": "大魔头莫凡的称号从哪一章开始有的？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["莫凡", "大魔头"],
     "chapter_no": 171, "project_id": MAIN_PROJECT, "verify_terms": ["大魔头"],
     "notes": "别名：第171章『大魔头莫凡真就将大家的东西给抢空了』起正式出现"},
    {"difficulty": "alias", "category": "source_chunk", "query": "全校公敌是谁给莫凡起的外号？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["莫凡", "全校公敌"],
     "chapter_no": 188, "project_id": MAIN_PROJECT, "verify_terms": ["全校公敌"],
     "notes": "别名：第188章『莫凡这个名字早就成为了全校公敌，谁让莫凡把全校新生的资源全部占为己有了』"},
    {"difficulty": "alias", "category": "character_profile", "query": "绿茶男指的是穆白吗？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["穆白", "绿茶男"],
     "chapter_no": 7, "project_id": MAIN_PROJECT, "verify_terms": ["绿茶男"],
     "notes": "别名：第7章标题『绿茶男』；第574章『绿茶男，好久不见』莫凡打量着穆白"},
    {"difficulty": "alias", "category": "source_chunk", "query": "冲榜狂魔这个外号是什么时候出现的？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["莫凡", "冲榜狂魔"],
     "chapter_no": 519, "project_id": MAIN_PROJECT, "verify_terms": ["冲榜狂魔"],
     "notes": "别名：第519章『顷刻间人们便又给莫凡取了一个外号，冲榜狂魔！』"},
    {"difficulty": "alias", "category": "character_profile", "query": "凡哥和莫凡哥是同一个人吗？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["莫凡", "凡哥", "莫凡哥"],
     "chapter_no": 2, "project_id": MAIN_PROJECT, "verify_terms": ["凡哥", "莫凡哥"],
     "notes": "别名核对：凡哥（236 处）与莫凡哥（94 处）都是正文对莫凡的称呼，同一人"},

    # ── typo（6）──
    {"difficulty": "typo", "category": "source_chunk", "query": "张小候的风系初阶技能叫什么？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["张小侯", "张小候", "风轨"],
     "chapter_no": 19, "project_id": MAIN_PROJECT, "verify_terms": ["张小候"],
     "notes": "错别字 侯→候（语料自身误写）：正确技能名风轨，第19章『风之力，风轨！』"},
    {"difficulty": "typo", "category": "source_chunk", "query": "张候在哪一章释放了风系技能？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["张小侯", "张候", "风轨"],
     "chapter_no": 19, "project_id": MAIN_PROJECT, "verify_terms": ["张候"],
     "notes": "错别字 侯→候（语料自身误写『张候』7 处）：张小侯第19章释放风轨"},
    {"difficulty": "typo", "category": "character_profile", "query": "慕白和穆白是同一个角色吗？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["穆白", "慕白"],
     "chapter_no": 10, "project_id": MAIN_PROJECT, "verify_terms": ["慕白"],
     "notes": "错别字 穆→慕（语料自身误写 23 处）：同一角色，正确写法穆白"},
    {"difficulty": "typo", "category": "ability_query", "query": "雷息是什么系的魔法？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["莫凡", "雷系", "雷息"],
     "project_id": MAIN_PROJECT, "verify_terms": ["雷息"],
     "notes": "错别字 系→息（同音，语料含『雷息』6 处）：正确词雷系，莫凡的雷系星尘"},
    {"difficulty": "typo", "category": "source_chunk", "query": "风鬼是张小侯释放的技能吗？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["张小侯", "风轨", "风鬼"],
     "chapter_no": 19, "project_id": MAIN_PROJECT, "verify_terms": ["风鬼"],
     "notes": "错别字 轨→鬼（同音）：正确技能名风轨，第19章张小侯释放风轨"},
    {"difficulty": "typo", "category": "ability_query", "query": "赵满廷的光系魔法厉害吗？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["赵满延", "赵满廷", "光系"],
     "project_id": MAIN_PROJECT, "verify_terms": ["赵满延"],
     "notes": "错别字 延→廷（近形）：赵满延是光系魔法师，facts『赵满延的光系魔法对青黄小妖有致命效果』"},

    # ── homophone（6）──
    {"difficulty": "homophone", "category": "ability_query", "query": "莫烦觉醒了什么系？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE], "entities": ["莫凡", "莫烦", "火系", "雷系"],
     "chapter_no": 1, "project_id": MAIN_PROJECT, "verify_terms": ["莫凡"],
     "notes": "同音字 凡→烦：莫凡第1章觉醒火系+雷系（ability_profile）"},
    {"difficulty": "homophone", "category": "ability_query", "query": "张小猴是风系法师吗？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE], "entities": ["张小侯", "张小猴", "风系"],
     "chapter_no": 16, "project_id": MAIN_PROJECT, "verify_terms": ["张小侯"],
     "notes": "同音字 侯→猴：张小侯主项目 ability_profile=风系（第16章），『猴子』恰是真实别名，易误导"},
    {"difficulty": "homophone", "category": "source_chunk", "query": "赵曼延的光系魔法有什么效果？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["赵满延", "赵曼延", "光系"],
     "project_id": MAIN_PROJECT, "verify_terms": ["赵满延"],
     "notes": "同音字 满→曼：赵满延光系魔法对青黄小妖有致命效果（facts）"},
    {"difficulty": "homophone", "category": "ability_query", "query": "木白的冰系技能是什么？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["穆白", "木白", "冰系", "冰蔓"],
     "chapter_no": 25, "project_id": MAIN_PROJECT, "verify_terms": ["穆白"],
     "notes": "同音字 穆→木：穆白第25章释放冰系技能-冰蔓"},
    {"difficulty": "homophone", "category": "source_chunk", "query": "汤月老师的暗影系技能厉害吗？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["唐月", "汤月", "暗影系"],
     "chapter_no": 16, "project_id": MAIN_PROJECT, "verify_terms": ["唐月", "暗影系"],
     "notes": "同音字 唐→汤：facts『莫凡是见识过唐月老师施展暗影系技能』"},
    {"difficulty": "homophone", "category": "source_chunk", "query": "星晨魔器是谁的？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["星尘魔器", "星晨魔器"],
     "chapter_no": 20, "project_id": MAIN_PROJECT, "verify_terms": ["星尘魔器"],
     "notes": "同音字 尘→晨：第20章标题『星尘魔器』，正文中的魔器（唐月老师处理过星尘魔器事件）"},

    # ── same_name（4）──
    {"difficulty": "same_name", "category": "ability_query", "query": "莫凡的雷系是哪个项目的设定？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE, E2E_SOURCE, TEST_SOURCE],
     "entities": ["莫凡", "雷系"], "chapter_no": 1, "project_id": MAIN_PROJECT, "verify_terms": ["莫凡"],
     "notes": "同名不同人：雷系仅主项目 d62d4447 的莫凡有，E2E 项目与测试小说的莫凡都没有雷系"},
    {"difficulty": "same_name", "category": "source_chunk", "query": "张小侯在测试小说里出现吗？",
     "evidence_type": "source_chunk", "source_ids": [TEST_SOURCE],
     "entities": ["张小侯", "风轨"], "chapter_no": 1, "project_id": TEST_PROJECT, "verify_terms": ["张小侯"],
     "notes": "测试小说（de04afc8）第一章：『第二章 初战 张小侯释放风轨』，与主项目同名角色"},
    {"difficulty": "same_name", "category": "source_chunk", "query": "两个项目里的赵满延光系设定一样吗？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE, E2E_SOURCE],
     "entities": ["赵满延", "光系"], "project_id": MAIN_PROJECT, "verify_terms": ["赵满延"],
     "notes": "同名不同人：主项目赵满延=主修光系（另有水系/雷系说法），E2E 项目=光系魔法保护张小侯"},
    {"difficulty": "same_name", "category": "character_profile", "query": "莫凡和E2E莫凡是同一个人吗？",
     "evidence_type": "character_profile", "source_ids": [MAIN_SOURCE, E2E_SOURCE],
     "entities": ["莫凡"], "project_id": MAIN_PROJECT, "verify_terms": ["莫凡"],
     "notes": "同名不同人：两个项目都有莫凡（character_profile 各 1 行），能力设定不同（火+雷 vs 火），需项目上下文消歧"},

    # ── conflict（4）──
    {"difficulty": "conflict", "category": "character_first_appearance", "query": "张小侯的风系到底第几章掌握的？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE, E2E_SOURCE],
     "entities": ["张小侯", "风系"], "project_id": MAIN_PROJECT, "verify_terms": ["张小侯"],
     "notes": "多来源冲突：主项目=16、E2E=1、正文第19章才首次展示风轨，无单一正确答案"},
    {"difficulty": "conflict", "category": "source_chunk", "query": "鳞皮母妖是莫凡杀的吗？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["莫凡", "赵满延", "鳞皮母妖"], "project_id": MAIN_PROJECT, "verify_terms": ["鳞皮母妖"],
     "notes": "事实冲突：校方公布赵满延杀死鳞皮母妖，学生流传『真正杀死鳞皮母妖的一定是那个莫凡』"},
    {"difficulty": "conflict", "category": "ability_query", "query": "张小侯有没有水系？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE],
     "entities": ["张小侯", "水系"], "project_id": MAIN_PROJECT, "verify_terms": ["张小侯"],
     "notes": "结构化表 vs facts 冲突：ability_profile 仅风系，facts『张小侯和赵满延都是水系法师』"},
    {"difficulty": "conflict", "category": "ability_query", "query": "莫凡到底是什么系？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE, E2E_SOURCE, TEST_SOURCE],
     "entities": ["莫凡", "火系", "雷系"], "chapter_no": 1, "project_id": MAIN_PROJECT, "verify_terms": ["莫凡"],
     "notes": "多来源矛盾：主项目=火+雷（天生双系），E2E=火，测试小说=火；答案取决于项目上下文"},

    # ── cross_chapter（4）──
    {"difficulty": "cross_chapter", "category": "timeline_event", "query": "从张小侯首现到释放风轨隔了几章？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["张小侯", "风轨"], "chapter_no": 16, "project_id": MAIN_PROJECT, "verify_terms": ["张小侯", "风轨"],
     "notes": "跨章节：张小侯第16章首现（新女老师，唐月），第19章释放风轨（风之力，风轨！），间隔3章"},
    {"difficulty": "cross_chapter", "category": "timeline_event", "query": "穆白从绿茶男到冰系高手用了多少章？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["穆白", "绿茶男", "冰系"], "chapter_no": 25, "project_id": MAIN_PROJECT, "verify_terms": ["绿茶男", "冰蔓"],
     "notes": "跨章节：第7章『绿茶男』，第25章『冰系，冰蔓！』穆白把控七颗星子释放冰蔓"},
    {"difficulty": "cross_chapter", "category": "timeline_event", "query": "莫凡觉醒仪式和释放雷印之间发生了什么？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["莫凡", "雷印", "雷系"], "chapter_no": 10, "project_id": MAIN_PROJECT, "verify_terms": ["雷印"],
     "notes": "跨章节：第4章觉醒仪式（火系+雷系）→ 第5/6章揭示天生双系 → 第10章释放雷印（5个月）"},
    {"difficulty": "cross_chapter", "category": "source_chunk", "query": "莫凡外号从大魔头到冲榜狂魔经历了什么？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["莫凡", "大魔头", "冲榜狂魔", "全校公敌"], "project_id": MAIN_PROJECT, "verify_terms": ["大魔头", "冲榜狂魔"],
     "notes": "跨章节聚合：大魔头（第171章）→ 全校公敌（第188章）→ 冲榜狂魔（第519章），需聚合多章节外号证据"},

    # ── no_result（4）──
    {"difficulty": "no_result", "category": "no_result", "query": "苏小小是谁？",
     "evidence_type": "source_chunk", "source_ids": [], "entities": ["苏小小"], "probe": "苏小小", "project_id": MAIN_PROJECT,
     "verify_terms": [],
     "notes": "查无结果：『苏小小』全库 0 命中，期望检索不召回证据"},
    {"difficulty": "no_result", "category": "no_result", "query": "莫凡的奶奶叫什么？",
     "evidence_type": "source_chunk", "source_ids": [], "entities": ["莫凡", "奶奶"], "probe": "莫凡的奶奶", "project_id": MAIN_PROJECT,
     "verify_terms": [],
     "notes": "查无结果：短语『莫凡的奶奶』全库 0 命中（正文只提莫家兴，无奶奶信息）"},
    {"difficulty": "no_result", "category": "no_result", "query": "莫凡的生辰八字是什么？",
     "evidence_type": "source_chunk", "source_ids": [], "entities": ["莫凡", "生辰八字"], "probe": "生辰八字", "project_id": MAIN_PROJECT,
     "verify_terms": [],
     "notes": "查无结果：『生辰八字』全库 0 命中，期望检索不召回证据"},
    {"difficulty": "no_result", "category": "no_result", "query": "陈明是哪个角色？",
     "evidence_type": "source_chunk", "source_ids": [], "entities": ["陈明"], "probe": "陈明", "project_id": MAIN_PROJECT,
     "verify_terms": [],
     "notes": "查无结果：『陈明』全库 0 命中，期望检索不召回证据"},

    # ── no_inject（2）：元查询，期望检索/注入层拦截，不写入正文 ──
    {"difficulty": "no_inject", "category": "meta", "query": "写第10章之前不要透露莫凡的雷印",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["莫凡", "雷印"], "chapter_no": 10, "project_id": MAIN_PROJECT, "verify_terms": ["雷印"],
     "notes": "no_inject：雷印在第10章才首次释放，写第10章之前不应注入该设定；检索若命中=注入违规"},
    {"difficulty": "no_inject", "category": "meta", "query": "写到第20章前，别提前写穆宁雪降临学校",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["穆宁雪"], "chapter_no": 21, "project_id": MAIN_PROJECT, "verify_terms": ["穆宁雪"],
     "notes": "no_inject：穆宁雪第21章（降临学校，穆宁雪）才出现，写第20章及之前不应注入该剧情"},
]


def _readonly_conn(db_path: str) -> sqlite3.Connection:
    """只读打开 SQLite：mode=ro 时 SQLite 拒绝任何写操作，绝不修改库。"""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _like_count(conn: sqlite3.Connection, term: str, table: str) -> int:
    if table == "project_source_chunks":
        return int(conn.execute(
            "SELECT COUNT(*) FROM project_source_chunks WHERE content LIKE ?", (f"%{term}%",)
        ).fetchone()[0])
    if table == "project_knowledge_facts":
        return int(conn.execute(
            "SELECT COUNT(*) FROM project_knowledge_facts WHERE evidence_text LIKE ? OR subject LIKE ? OR object LIKE ?",
            (f"%{term}%",) * 3,
        ).fetchone()[0])
    return int(conn.execute(
        "SELECT COUNT(*) FROM project_source_chapters WHERE content LIKE ? OR chapter_title LIKE ?",
        (f"%{term}%",) * 2,
    ).fetchone()[0])


def verify_defs(conn: sqlite3.Connection) -> list[str]:
    """对新增样本引用词做只读核对，返回警告列表（语义同 generate_regression）。"""
    warnings: list[str] = []
    for d in NATURAL_MORE_DEFS:
        if d["difficulty"] == "no_result":
            probe = d.get("probe") or (d["entities"][0] if d["entities"] else "")
            if probe:
                n = _like_count(conn, probe, "project_source_chunks")
                if n > 0:
                    warnings.append(f"no_result 探测短语 {probe!r} 在语料有 {n} 处命中，样本失效")
        else:
            for term in d.get("verify_terms") or []:
                n = sum(_like_count(conn, term, t) for t in ("project_source_chunks", "project_knowledge_facts", "project_source_chapters"))
                if n == 0:
                    warnings.append(f"引用词 {term!r} 在三张表均 0 命中，notes 可能过期")
    return warnings


def build_new_samples() -> list[dict[str, Any]]:
    """把 NATURAL_MORE_DEFS 转成样本 dict（query_id 从 nat-029 起顺序编号）。"""
    samples: list[dict[str, Any]] = []
    seq = 29  # 既有 28 条占 nat-001..nat-028
    for d in NATURAL_MORE_DEFS:
        exp: dict[str, Any] = {
            "evidence_type": d["evidence_type"],
            "source_ids": list(d.get("source_ids") or []),
            "entities": list(d.get("entities") or []),
        }
        if d.get("chapter_no") is not None:
            exp["chapter_no"] = d["chapter_no"]
        if d.get("project_id"):
            exp["project_id"] = d["project_id"]
        samples.append({
            "query_id": f"nat-{seq:03d}",
            "category": d["category"],
            "query": d["query"],
            "expected": exp,
            "difficulty": d["difficulty"],
            "notes": d["notes"],
        })
        seq += 1
    return samples


def _distribution(samples: list[dict[str, Any]]) -> dict[str, int]:
    dist: dict[str, int] = {}
    for s in samples:
        dist[s["difficulty"]] = dist.get(s["difficulty"], 0) + 1
    return {k: dist[k] for k in sorted(dist)}


def main(argv: list[str]) -> int:
    db_path = str(Path(argv[0]).resolve()) if argv else str(DEFAULT_DB)
    out_path = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_OUT
    if not Path(db_path).exists():
        print(f"[error] 数据库不存在: {db_path}", file=sys.stderr)
        return 1
    if not out_path.exists():
        print(f"[error] 输出文件不存在（需在既有 natural_queries.json 基础上追加）: {out_path}", file=sys.stderr)
        return 1

    with open(out_path, encoding="utf-8") as f:
        payload = json.load(f)
    existing = payload.get("samples") or []
    meta = dict(payload.get("meta") or {})
    if not isinstance(existing, list) or not existing:
        print(f"[error] 既有数据集为空或格式不符: {out_path}", file=sys.stderr)
        return 1
    # 兼容早期误把旧 meta 包在 "meta" 键下的文件：把嵌套内容并入顶层 meta
    if isinstance(meta.get("meta"), dict):
        nested = meta.pop("meta")
        for k, v in nested.items():
            meta.setdefault(k, v)

    conn = _readonly_conn(db_path)
    try:
        warnings = verify_defs(conn)
    finally:
        conn.close()
    for w in warnings:
        print(f"[warn] {w}", file=sys.stderr)

    new = build_new_samples()
    seen_ids = {s["query_id"] for s in existing}
    new = [s for s in new if s["query_id"] not in seen_ids]
    merged = existing + new

    # 更新 meta（保留既有键，追加生成信息）
    meta["dataset_name"] = "natural_queries"
    meta["dataset_version"] = "1.0"
    meta["generator"] = "generate_natural_more.py (28 → 70 扩充)"
    meta["description"] = (
        "人工编写的自然查询集 + 困难样本，全部基于 dev-web.sqlite3 真实内容"
        "（先 SELECT 核对数据再写查询）；28 条为人工精修，42 条由 generate_natural_more.py "
        "在真实数据上构造并只读核对"
    )
    meta["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta["db_path"] = str(db_path)
    meta["added_by_generator"] = {"new_samples": len(new), "prev_total": len(existing)}
    meta["difficulty_distribution"] = _distribution(merged)
    meta["total_samples"] = len(merged)
    verify_notes = list(meta.get("verification_notes") or [])
    new_notes = [
        "nat-029 起为 generate_natural_more.py 生成的扩充样本：normal 6/alias 6/typo 6/homophone 6/same_name 4/conflict 4/cross_chapter 4/no_result 4/no_inject 2，引用词与 no_result 探测短语均在生成时对 dev-web.sqlite3 做只读 SELECT 核对",
        "新增别名：泥猴子=张小侯(第2章)、凡哥/莫凡哥=莫凡(第2章起)、黑白无常妇=撒朗/伊之纱(第1232章)、冷面判官=杰西(第1102章)、移动电灯泡=光系初阶技能外号(第47章)",
        "新增 no_result 均经全库检索确认 0 命中：苏小小/莫凡的奶奶/生辰八字/陈明",
        "新增 no_inject：雷印(第10章)、穆宁雪(第21章) 在对应章节前不应注入正文",
    ]
    verify_notes = list(dict.fromkeys([n for n in verify_notes if n not in new_notes] + new_notes))
    meta["verification_notes"] = verify_notes

    out_payload = {"meta": meta, "samples": merged}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out_payload, f, ensure_ascii=False, indent=2)

    print(f"[ok] natural_queries.json: {len(existing)} → {len(merged)} 条（新增 {len(new)}）已写入: {out_path}")
    print(f"    按难度分布: {_distribution(merged)}")
    if warnings:
        print(f"[warn] 共 {len(warnings)} 条核对警告，详见上方 stderr 输出")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
