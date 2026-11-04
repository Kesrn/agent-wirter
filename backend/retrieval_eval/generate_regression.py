#!/usr/bin/env python3
"""结构化回归候选查询生成器（阶段零：评测集构建；阶段一扩充：困难样本）。

从 dev-web.sqlite3（只读）读取 ability_profile / event_timeline / character_profile /
world_rule 的真实数据，按模板为每行生成 1-2 条自然问句查询，写为
retrieval_eval/datasets/regression_candidates.json。

生成规则（每条查询的 expected 引用真实记录）：
    - ability_profile 行 → "XX的YY是什么等级？"（ability_query）/ "XX是什么时候掌握YY的？"
      evidence_type=ability_profile，entities=[character_name, ability_name]，chapter_no=first_seen_chapter
    - event_timeline 行 → "第N章发生了什么？" / "第N章有哪些角色出场？"
      evidence_type=event_timeline，chapter_no=实际值，entities=characters JSON
    - character_profile 行 → "介绍下XX" / "XX是什么角色？"
      evidence_type=character_profile，entities=[name]
    - world_rule 行 → "这个世界里XX的规则是什么？" / "这个世界的XX设定是怎么样的？"
      evidence_type=world_rule，entities=[category]

阶段一扩充（困难样本，见 HARD_SAMPLE_DEFS，每类 ≥6 条）：
    - alias      别名查询：猴子/泥猴子/大魔头/全校公敌/冲榜狂魔/绿茶男/凡哥 等
                 （character_alias_clusters 表为空，别名全部从正文真实提取并核对）
    - typo       错别字：张小候/张候/慕白/雷息/风鬼 等（部分错别字来自语料自身）
    - homophone  同音字：莫烦/张小猴/赵曼延/木白/汤月 等（同音构造，侯→猴 恰是真实别名）
    - same_name  同名跨项目：莫凡/张小侯/赵满延 在主项目、E2E、测试小说中设定不同
    - conflict   多来源矛盾：张小侯风系时间(16/1/19)、张小侯/赵满延魔法系构成、
                 鳞皮母妖击杀者、莫凡系别数量 等
    - cross_chapter 多事件合并查询：觉醒→天生双系、首现→风轨、绿茶男→冰蔓 等
    - no_result  查无结果：王小明/林黛玉/乾坤袋 等（生成时全库检索核对 0 命中）

设计要点：
    - 数据库只读：sqlite3 以 file:...?mode=ro URI 打开，SQLite 层面拒绝任何写操作。
    - expected.source_ids 填 source 级 id（project_sources.id，即 search_project_knowledge
      返回的 source_id），保证基线命中规则 A 可命中；同时保留结构化表行 id 到
      expected.record_ids（扩展字段，仅溯源，run_baseline.py 不读取）。
    - 困难样本生成时对引用词做只读 SELECT 核对：no_result 词在语料中 0 命中才有效；
      别名/错别字/同音字引用词需在语料中存在，缺失时记入 meta.hard_sample_warnings。
    - 仅用标准库（sqlite3 / json / dataclasses），不建虚拟环境、不装依赖。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

EVAL_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVAL_DIR.parent
DEFAULT_DB = BACKEND_DIR / "dev-web.sqlite3"
DEFAULT_OUT = EVAL_DIR / "datasets" / "regression_candidates.json"

# 结构化表清单：(表名, 主键列, 证据类型, 需解析的 JSON 列)
TABLES = [
    ("ability_profile", "ability_profile", ("character_name", "ability_name"), ("evidence",)),
    ("event_timeline", "event_timeline", ("event_title",), ("characters", "evidence")),
    ("character_profile", "character_profile", ("name",), ("aliases", "evidence")),
    ("world_rule", "world_rule", ("category", "rule_text"), ("evidence",)),
]

# 已知数据一致性问题的行：difficulty 覆盖（基于真实库内容核对后人工标注）
# key = (表名, 行 id)
DIFFICULTY_OVERRIDES: dict[tuple[str, str], dict[str, str]] = {
    # 张小侯风系：ability_profile.first_seen_chapter=16（主项目 d62d4447），
    # 但 E2E 项目（49aee26a）同角色第 1 章即掌握风系，正文第 19 章才首次展示风轨，
    # 三处时间互相冲突 → 标记 conflict
    ("ability_profile", "46707d57-5ee9-4c6d-85f6-a711239b6f81"): {
        "difficulty": "conflict",
        "notes": "张小侯风系 first_seen_chapter=16 与 E2E 项目第1章、正文第19章(风之力，风轨)不一致，属跨来源时间冲突",
    },
    # E2E 项目的莫凡火系：与主项目(全职法师)同名角色设定不同 → same_name
    ("ability_profile", "516d3dd9-b660-4386-9524-e2d212831877"): {
        "difficulty": "same_name",
        "notes": "E2E 项目莫凡与主项目(全职法师)莫凡为同名不同角色，能力设定不同，需项目上下文消歧",
    },
    ("ability_profile", "07e8bde0-f06a-487e-8ac5-6869469eb0a1"): {
        "difficulty": "same_name",
        "notes": "E2E 项目张小侯与主项目张小侯同名，风系掌握时间(第1章)与主项目(第16章)不同，需项目上下文消歧",
    },
}

# ──────────────────────────────────────────────────────────
# 困难样本（阶段一扩充：每类 ≥6 条，全部基于库内真实数据人工核对后构造）
# ──────────────────────────────────────────────────────────
# 常量：三个项目的 source 级 id / project 级 id（dev-web.sqlite3 实测值）
MAIN_SOURCE = "e6b5ec9a-2b87-428d-aa42-124a8ffa2482"      # d62d4447 全职法师全文
E2E_SOURCE = "36cdfeeb-4016-4f90-85c8-b920e06405d5"       # 49aee26a E2E小说
TEST_SOURCE = "06f34d62-2c85-4dac-958b-41a0254da46b"      # de04afc8 小说抽取测试
MAIN_PROJECT = "d62d4447-b8d7-4fc3-9193-a6d363d8f541"
E2E_PROJECT = "49aee26a-3535-4ad9-85f2-2f235d663e00"
TEST_PROJECT = "de04afc8-306e-41b6-97c9-904b4a0da9bd"

# 每类困难样本的 query_id 前缀
HARD_PREFIX = {
    "alias": "reg-al",
    "typo": "reg-ty",
    "homophone": "reg-ho",
    "same_name": "reg-sn",
    "conflict": "reg-cf",
    "cross_chapter": "reg-cc",
    "no_result": "reg-nr",
}

# 困难样本定义。字段说明：
#   query/category/evidence_type/source_ids/entities/chapter_no/project_id/notes
#   与 dataset_schema 契约一致（difficulty 在下方按类标注）；
#   verify_terms：生成时做只读 SELECT 核对的引用词（别名/技能/人名/章节关键词），
#   任一词在 project_source_chunks/facts/chapters 三张表都 0 命中则记入 meta 警告。
#   所有词条均先在 dev-web.sqlite3 中 SELECT 核对过（2026-08-02），
#   no_result 样本的实体词经全库检索确认 0 命中。
HARD_SAMPLE_DEFS: list[dict[str, Any]] = [
    # ── alias：正文提取的真实别名（character_alias_clusters 表为 0 行）──
    {"difficulty": "alias", "category": "ability_query", "query": "猴子是什么系的法师？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["张小侯", "猴子", "风系"],
     "chapter_no": 9, "project_id": MAIN_PROJECT, "verify_terms": ["猴子", "张小侯"],
     "notes": "别名：正文多处称张小侯为『猴子』（第9章『猴子说他每个星期最多只能够练习1个小时的把控』），答案=风系"},
    {"difficulty": "alias", "category": "character_profile", "query": "泥猴子指的是谁？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["张小侯", "泥猴子"],
     "chapter_no": 2, "project_id": MAIN_PROJECT, "verify_terms": ["泥猴子"],
     "notes": "别名：第2章『这泥猴子叫张候』；第345章『张小侯好不容易才把自己收拾的稍微干净一些，这会又变成了泥猴子』"},
    {"difficulty": "alias", "category": "character_profile", "query": "大魔头是谁？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["莫凡", "大魔头"],
     "chapter_no": 171, "project_id": MAIN_PROJECT, "verify_terms": ["大魔头", "莫凡"],
     "notes": "别名：第171章『大魔头莫凡真就将大家的东西给抢空了』；第417章『用不了一个月这个明珠学府就会知道自己大魔头特么又回来了』"},
    {"difficulty": "alias", "category": "character_profile", "query": "全校公敌说的是谁？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["莫凡", "全校公敌"],
     "chapter_no": 188, "project_id": MAIN_PROJECT, "verify_terms": ["全校公敌"],
     "notes": "别名：第188章『在这青校区，莫凡这个名字早就成为了全校公敌』；第202/206章亦有"},
    {"difficulty": "alias", "category": "source_chunk", "query": "冲榜狂魔是谁的外号？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["莫凡", "冲榜狂魔"],
     "chapter_no": 519, "project_id": MAIN_PROJECT, "verify_terms": ["冲榜狂魔"],
     "notes": "别名：第519章『顷刻间人们便又给莫凡取了一个外号，冲榜狂魔！』"},
    {"difficulty": "alias", "category": "character_profile", "query": "绿茶男是哪个角色？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["穆白", "绿茶男"],
     "chapter_no": 7, "project_id": MAIN_PROJECT, "verify_terms": ["绿茶男", "穆白"],
     "notes": "别名：第7章标题即『绿茶男』；第10章『慕白你这绿茶男怎么跟你爷爷我斗』；第574章『绿茶男，好久不见』莫凡打量着穆白"},
    {"difficulty": "alias", "category": "source_chunk", "query": "黑白无常妇是莫凡给谁取的绰号？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["撒朗", "伊之纱", "黑白无常妇"],
     "chapter_no": 1232, "project_id": MAIN_PROJECT, "verify_terms": ["黑白无常妇", "撒朗", "伊之纱"],
     "notes": "别名：第1232章『黑白无常妇，那是莫凡给撒朗和伊之纱取的绰号。黑无常妇，自然是撒朗。白无常妇是伊之纱』"},
    {"difficulty": "alias", "category": "character_profile", "query": "冷面判官是谁？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["杰西", "冷面判官"],
     "chapter_no": 1102, "project_id": MAIN_PROJECT, "verify_terms": ["冷面判官", "杰西"],
     "notes": "别名：第1102章『有外号叫冷面判官。主修植物系，次修诅咒系，辅修光系』（杰西）"},
    {"difficulty": "alias", "category": "source_chunk", "query": "光系初阶学生的技能外号是什么？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["光系", "移动电灯泡"],
     "chapter_no": 47, "project_id": MAIN_PROJECT, "verify_terms": ["移动电灯泡"],
     "notes": "别名（技能外号）：第47章『光系学生们施展了他们的能力…他们初阶的技能外号确实是「移动电灯泡」』"},
    {"difficulty": "alias", "category": "character_profile", "query": "凡哥是哪个角色？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["莫凡", "凡哥"],
     "chapter_no": 2, "project_id": MAIN_PROJECT, "verify_terms": ["凡哥"],
     "notes": "别名：正文中张候等邻里少年称莫凡为『凡哥』/『莫凡哥』（第2章『莫凡哥，回来了啊』），全库凡哥 236 处"},
    {"difficulty": "alias", "category": "character_profile", "query": "莫凡哥是谁？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["莫凡", "莫凡哥"],
     "chapter_no": 2, "project_id": MAIN_PROJECT, "verify_terms": ["莫凡哥"],
     "notes": "别名：『莫凡哥』是正文对莫凡的称呼（第2章起，全库 94 处），此处为别名查询"},
    {"difficulty": "alias", "category": "source_chunk", "query": "莫凡都有哪些外号？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["莫凡", "大魔头", "全校公敌", "冲榜狂魔"],
     "project_id": MAIN_PROJECT, "verify_terms": ["大魔头", "全校公敌", "冲榜狂魔"],
     "notes": "别名聚合：莫凡的外号随时间变化——大魔头（第171章起）、全校公敌（第188章）、冲榜狂魔（第519章），需跨章节聚合"},

    # ── typo：错别字（前 5 条错别字来自语料自身，其余为近形构造）──
    {"difficulty": "typo", "category": "ability_query", "query": "张小候是什么系的法师？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["张小侯", "张小候", "风系"],
     "chapter_no": 19, "project_id": MAIN_PROJECT, "verify_terms": ["张小候", "张小侯"],
     "notes": "错别字 侯→候：语料自身即误写『张小候』（全库 3 处），正确写法张小侯，风系"},
    {"difficulty": "typo", "category": "character_first_appearance", "query": "张小候第一次出现在第几章？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["张小侯", "张小候"],
     "chapter_no": 16, "project_id": MAIN_PROJECT, "verify_terms": ["张小候"],
     "notes": "错别字 侯→候：正确角色张小侯第16章（新女老师，唐月）首次在正文出场"},
    {"difficulty": "typo", "category": "ability_query", "query": "张候觉醒了什么系？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["张小侯", "张候", "风系"],
     "chapter_no": 5, "project_id": MAIN_PROJECT, "verify_terms": ["张候"],
     "notes": "错别字 侯→候：第5章『泥猴子张候…一股奇异的风旋涡在觉醒石上显现…很好，风系』（语料自身误写）"},
    {"difficulty": "typo", "category": "character_profile", "query": "泥猴子张候是哪个角色？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["张小侯", "泥猴子", "张候"],
     "chapter_no": 2, "project_id": MAIN_PROJECT, "verify_terms": ["张候", "泥猴子"],
     "notes": "错别字 侯→候：第2章『这泥猴子叫张候』，即张小侯（语料自身误写）"},
    {"difficulty": "typo", "category": "character_profile", "query": "慕白是哪个角色？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["穆白", "慕白"],
     "chapter_no": 10, "project_id": MAIN_PROJECT, "verify_terms": ["慕白", "穆白"],
     "notes": "错别字 穆→慕：语料多处误写『慕白』（全库 23 处），正确写法穆白"},
    {"difficulty": "typo", "category": "ability_query", "query": "慕白是什么系的法师？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["穆白", "慕白", "冰系"],
     "chapter_no": 25, "project_id": MAIN_PROJECT, "verify_terms": ["慕白", "冰蔓"],
     "notes": "错别字 穆→慕：穆白为冰系法师（第25章『穆白…好期待冰系技能啊』），慕白为语料误写"},
    {"difficulty": "typo", "category": "character_first_appearance", "query": "莫凡的雷息是什么时候觉醒的？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["莫凡", "雷系", "雷息"],
     "chapter_no": 4, "project_id": MAIN_PROJECT, "verify_terms": ["雷息"],
     "notes": "错别字 系→息（同音）：语料含『雷息』（全库 6 处），正确词雷系，莫凡第4章法系觉醒仪式觉醒"},
    {"difficulty": "typo", "category": "source_chunk", "query": "风鬼是谁释放的？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["张小侯", "风轨", "风鬼"],
     "chapter_no": 19, "project_id": MAIN_PROJECT, "verify_terms": ["风鬼"],
     "notes": "错别字 轨→鬼（同音）：正确技能名『风轨』，第19章张小侯在宿舍走廊释放风轨"},
    {"difficulty": "typo", "category": "ability_query", "query": "赵满廷是什么系的法师？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["赵满延", "赵满廷", "光系"],
     "project_id": MAIN_PROJECT, "verify_terms": ["赵满延", "光系"],
     "notes": "错别字 延→廷（近形）：正确写法赵满延，主修光系（facts：赵满延是光系魔法师，拥有光佑·圣盾）"},
    {"difficulty": "typo", "category": "ability_query", "query": "穆百是什么系的法师？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["穆白", "穆百", "冰系"],
     "project_id": MAIN_PROJECT, "verify_terms": ["穆白"],
     "notes": "错别字 白→百（近形）：正确写法穆白，冰系法师（第25章释放冰系技能-冰蔓）"},
    {"difficulty": "typo", "category": "character_profile", "query": "唐朋老师是第几章出现的？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["唐月", "唐朋"],
     "chapter_no": 16, "project_id": MAIN_PROJECT, "verify_terms": ["唐月"],
     "notes": "错别字 月→朋（近形）：正确写法唐月，第16章『新女老师，唐月』成为新老师"},
    {"difficulty": "typo", "category": "ability_query", "query": "冰曼是穆白释放的技能吗？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["穆白", "冰系", "冰蔓", "冰曼"],
     "chapter_no": 25, "project_id": MAIN_PROJECT, "verify_terms": ["冰蔓"],
     "notes": "错别字 蔓→曼（近形）：正确技能名『冰蔓』，第25章穆白释放冰系技能-冰蔓"},
    {"difficulty": "typo", "category": "character_profile", "query": "陆晓斌是张小侯的室友吗？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["陆小斌", "张小侯"],
     "chapter_no": 19, "project_id": MAIN_PROJECT, "verify_terms": ["陆小斌"],
     "notes": "错别字 小→晓（近形）：正确写法陆小斌，第19章『同宿舍的陆小斌说道』，与张小侯同宿舍"},

    # ── homophone：同音字（凡→烦 等构造；侯→猴 恰与真实别名『猴子』重合，易误导）──
    {"difficulty": "homophone", "category": "ability_query", "query": "莫烦的火系是什么等级？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE], "entities": ["莫凡", "莫烦", "火系"],
     "chapter_no": 1, "project_id": MAIN_PROJECT, "verify_terms": ["莫凡"],
     "notes": "同音字 凡→烦：莫凡 火系 初阶（ability_profile first_seen_chapter=1）"},
    {"difficulty": "homophone", "category": "character_first_appearance", "query": "莫烦是什么时候觉醒火系的？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE], "entities": ["莫凡", "莫烦", "火系", "雷系"],
     "chapter_no": 1, "project_id": MAIN_PROJECT, "verify_terms": ["莫凡"],
     "notes": "同音字 凡→烦：莫凡第1章觉醒（ability_profile），正文第4章觉醒仪式详写火苗"},
    {"difficulty": "homophone", "category": "ability_query", "query": "张小猴掌握风系吗？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE], "entities": ["张小侯", "张小猴", "风系"],
     "chapter_no": 16, "project_id": MAIN_PROJECT, "verify_terms": ["张小侯", "风系"],
     "notes": "同音字 侯→猴：且『猴子』恰是张小侯的真实别名（第19章），检索容易误配到别名而非本人"},
    {"difficulty": "homophone", "category": "source_chunk", "query": "张小猴释放过什么技能？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["张小侯", "张小猴", "风轨"],
     "chapter_no": 19, "project_id": MAIN_PROJECT, "verify_terms": ["张小侯", "风轨"],
     "notes": "同音字 侯→猴：张小侯第19章释放风系初阶技能-风轨"},
    {"difficulty": "homophone", "category": "ability_query", "query": "赵曼延是什么系的法师？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["赵满延", "赵曼延", "光系", "水系"],
     "project_id": MAIN_PROJECT, "verify_terms": ["赵满延"],
     "notes": "同音字 满→曼：赵满延主修光系、次修水系（facts），与 E2E 项目同名角色设定不同"},
    {"difficulty": "homophone", "category": "character_profile", "query": "木白是哪个角色？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["穆白", "木白"],
     "chapter_no": 7, "project_id": MAIN_PROJECT, "verify_terms": ["穆白"],
     "notes": "同音字 穆→木：穆白，第7章起被称为『绿茶男』"},
    {"difficulty": "homophone", "category": "ability_query", "query": "汤月老师是什么系的？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["唐月", "汤月", "火系"],
     "chapter_no": 16, "project_id": MAIN_PROJECT, "verify_terms": ["唐月"],
     "notes": "同音字 唐→汤：唐月老师，facts『看上去温柔贤良的唐月老师竟然是一位火魔法师』"},
    {"difficulty": "homophone", "category": "source_chunk", "query": "冰漫是谁释放的技能？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["穆白", "冰系", "冰蔓", "冰漫"],
     "chapter_no": 25, "project_id": MAIN_PROJECT, "verify_terms": ["冰蔓"],
     "notes": "同音字 蔓→漫：正确技能名冰蔓，第25章『穆白考释放了…好期待冰系技能啊』"},
    {"difficulty": "homophone", "category": "source_chunk", "query": "星晨魔器是什么？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["星尘魔器", "星晨魔器"],
     "chapter_no": 20, "project_id": MAIN_PROJECT, "verify_terms": ["星尘魔器"],
     "notes": "同音字 尘→晨：第20章标题『星尘魔器』，一件魔器（唐月老师处理过星尘魔器事件）"},
    {"difficulty": "homophone", "category": "ability_query", "query": "火姿是火系初阶技能吗？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["火滋", "火姿", "火系"],
     "chapter_no": 25, "project_id": MAIN_PROJECT, "verify_terms": ["火滋"],
     "notes": "同音字 滋→姿：正确技能名火滋，第25章『莫凡完成火滋的释放后所有人的反应比穆白的冰蔓更加强烈十倍』"},
    {"difficulty": "homophone", "category": "ability_query", "query": "水玉是什么系的初阶技能？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["水御", "水玉", "水系"],
     "chapter_no": 19, "project_id": MAIN_PROJECT, "verify_terms": ["水御"],
     "notes": "同音字 御→玉：正确技能名水御，水系初阶技能（第19章『离释放水系技能-水御还远着呢』）"},
    {"difficulty": "homophone", "category": "character_profile", "query": "莫佳兴是谁？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["莫家兴", "莫佳兴", "莫凡"],
     "project_id": MAIN_PROJECT, "verify_terms": ["莫家兴"],
     "notes": "同音字 家→佳：莫家兴，莫凡的父亲（全库 74 处）"},
    {"difficulty": "homophone", "category": "character_first_appearance", "query": "木宁雪什么时候降临学校的？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["穆宁雪", "木宁雪"],
     "chapter_no": 21, "project_id": MAIN_PROJECT, "verify_terms": ["穆宁雪"],
     "notes": "同音字 穆→木：穆宁雪，第21章标题『降临学校，穆宁雪』"},
    {"difficulty": "homophone", "category": "character_profile", "query": "叶心瑕是谁？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE], "entities": ["叶心夏", "叶心瑕"],
     "project_id": MAIN_PROJECT, "verify_terms": ["叶心夏"],
     "notes": "同音字 夏→瑕：叶心夏，正文主要女性角色（全库 207 处）"},

    # ── same_name：同名跨项目（莫凡/张小侯/赵满延 在三个项目设定不同，需项目上下文消歧）──
    {"difficulty": "same_name", "category": "ability_query", "query": "莫凡觉醒了什么系？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE, E2E_SOURCE, TEST_SOURCE],
     "entities": ["莫凡", "火系", "雷系"], "chapter_no": 1, "project_id": MAIN_PROJECT,
     "verify_terms": ["莫凡"],
     "notes": "同名不同人：主项目(全职法师)莫凡=火系+雷系（天生双系），E2E 项目与测试小说=仅火系；无项目上下文时答案不唯一"},
    {"difficulty": "same_name", "category": "ability_query", "query": "张小侯是什么系的法师？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE, E2E_SOURCE],
     "entities": ["张小侯", "风系", "土系"], "chapter_no": 1, "project_id": MAIN_PROJECT,
     "verify_terms": ["张小侯"],
     "notes": "同名不同人：主项目张小侯=风系（facts 另有辅修土系/水系说法），E2E 项目=仅风系；需项目上下文消歧"},
    {"difficulty": "same_name", "category": "ability_query", "query": "赵满延是什么系的法师？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE, E2E_SOURCE],
     "entities": ["赵满延", "光系", "水系"], "project_id": MAIN_PROJECT,
     "verify_terms": ["赵满延"],
     "notes": "同名不同人：主项目赵满延=主修光系次修水系（facts 另有雷系灵种说法），E2E 项目=光系；需项目上下文消歧"},
    {"difficulty": "same_name", "category": "ability_query", "query": "莫凡会雷系魔法吗？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE, E2E_SOURCE, TEST_SOURCE],
     "entities": ["莫凡", "雷系"], "chapter_no": 1, "project_id": MAIN_PROJECT,
     "verify_terms": ["莫凡", "雷系"],
     "notes": "同名不同人：主项目莫凡第1章觉醒雷系，E2E 项目与测试小说中的莫凡没有雷系；答案取决于项目"},
    {"difficulty": "same_name", "category": "source_chunk", "query": "测试小说里的莫凡觉醒了什么？",
     "evidence_type": "source_chunk", "source_ids": [TEST_SOURCE],
     "entities": ["莫凡", "火系"], "project_id": TEST_PROJECT,
     "verify_terms": ["莫凡", "火系"],
     "notes": "测试小说（de04afc8）仅一章：『第一章 觉醒 莫凡觉醒了火系』，与主项目双系莫凡不同"},
    {"difficulty": "same_name", "category": "source_chunk", "query": "E2E小说里张小侯掌握的技能是什么？",
     "evidence_type": "source_chunk", "source_ids": [E2E_SOURCE],
     "entities": ["张小侯", "风轨", "风系"], "chapter_no": 1, "project_id": E2E_PROJECT,
     "verify_terms": ["张小侯", "风轨"],
     "notes": "E2E 小说第一章：『张小侯自己觉醒的是风系。张小侯释放风轨，证明他掌握风系』"},
    {"difficulty": "same_name", "category": "character_first_appearance", "query": "张小侯的风系在E2E项目里第几章掌握的？",
     "evidence_type": "ability_profile", "source_ids": [E2E_SOURCE, MAIN_SOURCE],
     "entities": ["张小侯", "风系"], "chapter_no": 1, "project_id": E2E_PROJECT,
     "verify_terms": ["张小侯"],
     "notes": "同名不同人+时间差异：E2E 项目张小侯第1章即掌握风系，主项目 ability_profile=第16章，正文第19章才展示风轨"},
    {"difficulty": "same_name", "category": "source_chunk", "query": "赵满延的光系魔法在哪个项目里保护了张小侯？",
     "evidence_type": "source_chunk", "source_ids": [E2E_SOURCE],
     "entities": ["赵满延", "光系", "张小侯"], "chapter_no": 1, "project_id": E2E_PROJECT,
     "verify_terms": ["赵满延", "光系"],
     "notes": "E2E 小说第一章：『赵满延的光系魔法保护了张小侯』；主项目无此情节，需项目上下文"},
    {"difficulty": "same_name", "category": "ability_query", "query": "莫凡的火系等级在不同项目里一致吗？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE, E2E_SOURCE],
     "entities": ["莫凡", "火系", "初阶"], "chapter_no": 1, "project_id": MAIN_PROJECT,
     "verify_terms": ["莫凡"],
     "notes": "同名核对：主项目与 E2E 项目莫凡火系都是初阶（ability_profile.level_desc=初阶），但仍需跨项目核对才能作答"},

    # ── conflict：多来源矛盾（结构化表 vs facts vs 正文，或多章节不一致）──
    {"difficulty": "conflict", "category": "character_first_appearance", "query": "张小侯是什么时候掌握风系的？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE, E2E_SOURCE],
     "entities": ["张小侯", "风系"], "project_id": MAIN_PROJECT,
     "verify_terms": ["张小侯", "风系"],
     "notes": "多来源冲突：主项目 ability_profile=第16章、E2E 项目=第1章、正文第19章才首次展示风轨；三处时间互相冲突，需按 source_priority 仲裁"},
    {"difficulty": "conflict", "category": "ability_query", "query": "张小侯掌握哪些魔法系？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE],
     "entities": ["张小侯", "风系", "土系", "水系"], "project_id": MAIN_PROJECT,
     "verify_terms": ["张小侯"],
     "notes": "结构化表与自由文本不一致：ability_profile 仅登记风系，但 facts 含『张小侯作为一个兼备风系和土系』『张小侯辅修的是土系』『张小侯和赵满延都是水系法师』"},
    {"difficulty": "conflict", "category": "ability_query", "query": "赵满延是水系还是光系法师？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE, E2E_SOURCE],
     "entities": ["赵满延", "水系", "光系"], "project_id": MAIN_PROJECT,
     "verify_terms": ["赵满延"],
     "notes": "多来源矛盾：facts 有『张小侯和赵满延都是水系法师』，也有『赵满延是光系魔法师，拥有光佑·圣盾』『雷系灵种』；E2E 小说又只写其光系"},
    {"difficulty": "conflict", "category": "source_chunk", "query": "谁杀死了鳞皮母妖？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["赵满延", "莫凡", "鳞皮母妖"], "project_id": MAIN_PROJECT,
     "verify_terms": ["鳞皮母妖", "赵满延", "莫凡"],
     "notes": "事实冲突：校方/媒体公布赵满延杀死鳞皮母妖立功，学生流传版本『光系赵满延才是协助者，真正杀死鳞皮母妖的一定是那个莫凡』"},
    {"difficulty": "conflict", "category": "ability_query", "query": "莫凡是几系法师？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE, E2E_SOURCE, TEST_SOURCE],
     "entities": ["莫凡", "火系", "雷系"], "project_id": MAIN_PROJECT,
     "verify_terms": ["莫凡"],
     "notes": "多来源矛盾：主项目=火+雷双系（天生双系），E2E 项目=仅火系，测试小说=仅火系；答案取决于数据源"},
    {"difficulty": "conflict", "category": "ability_query", "query": "张小侯是不是水系法师？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE],
     "entities": ["张小侯", "水系"], "project_id": MAIN_PROJECT,
     "verify_terms": ["张小侯"],
     "notes": "事实冲突：facts『张小侯和赵满延都是水系法师，他们在海里活动非常自如』vs ability_profile 仅登记风系；另有『张小侯不是冰系法师』"},
    {"difficulty": "conflict", "category": "source_chunk", "query": "莫凡是全校公敌还是见义勇为者？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["莫凡", "全校公敌"], "project_id": MAIN_PROJECT,
     "verify_terms": ["全校公敌"],
     "notes": "多来源矛盾评价：第188章『莫凡这个名字早就成为了全校公敌』vs 第202章表彰大会『这个一度被认为全校公敌的大魔头莫凡竟然做了一次惊人的见义勇为之举』"},
    {"difficulty": "conflict", "category": "character_first_appearance", "query": "莫凡的雷系是什么时候觉醒的？",
     "evidence_type": "ability_profile", "source_ids": [MAIN_SOURCE],
     "entities": ["莫凡", "雷系"], "project_id": MAIN_PROJECT,
     "verify_terms": ["莫凡", "雷系"],
     "notes": "时间冲突：ability_profile first_seen_chapter=1，但正文第4章『法系觉醒』才写觉醒仪式火苗、第5-6章『天生双系』才揭示雷系，结构化表与正文时间不一致"},

    # ── cross_chapter：多事件合并/跨章节聚合查询 ──
    {"difficulty": "cross_chapter", "category": "ability_query", "query": "莫凡第4章觉醒仪式觉醒了什么系？第5章揭示的天生双系是什么？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["莫凡", "火系", "雷系"], "chapter_no": 4, "project_id": MAIN_PROJECT,
     "verify_terms": ["天生双系", "觉醒"],
     "notes": "跨章节聚合：第4章『法系觉醒』觉醒仪式（莫凡觉醒石出现火苗+雷系），第5/6章『天生双系（上/下）』揭示双系"},
    {"difficulty": "cross_chapter", "category": "timeline_event", "query": "从莫凡觉醒到张小侯第一次在正文出现隔了几章？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["莫凡", "张小侯"], "chapter_no": 16, "project_id": MAIN_PROJECT,
     "verify_terms": ["张小侯"],
     "notes": "跨章节推理：莫凡觉醒=第4章（法系觉醒），张小侯首次正文出场=第16章（新女老师，唐月），间隔约12章"},
    {"difficulty": "cross_chapter", "category": "timeline_event", "query": "张小侯第16章首次出现，第19章做了什么？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["张小侯", "风轨"], "chapter_no": 16, "project_id": MAIN_PROJECT,
     "verify_terms": ["张小侯", "风轨"],
     "notes": "跨章节事件：张小侯首现=第16章（新女老师，唐月），第19章『风之力，风轨！』在宿舍走廊释放风轨"},
    {"difficulty": "cross_chapter", "category": "timeline_event", "query": "穆白第7章被称为绿茶男，第25章释放的冰系技能是什么？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["穆白", "绿茶男", "冰系", "冰蔓"], "chapter_no": 25, "project_id": MAIN_PROJECT,
     "verify_terms": ["绿茶男", "冰蔓"],
     "notes": "跨章节聚合：第7章标题『绿茶男』；第25章『冰系，冰蔓！』穆白释放冰系技能-冰蔓"},
    {"difficulty": "cross_chapter", "category": "timeline_event", "query": "莫凡从觉醒火系到释放雷印经历了多长时间？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["莫凡", "雷印", "雷系"], "chapter_no": 10, "project_id": MAIN_PROJECT,
     "verify_terms": ["雷印"],
     "notes": "跨章节推理：第4章觉醒，第10章『释放，雷印』（正文：从开学到现在学期结束，莫凡一共只用了5个月）"},
    {"difficulty": "cross_chapter", "category": "timeline_event", "query": "张小侯第19章释放风轨，第44章在飞崖上施展的又是什么？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["张小侯", "风轨"], "chapter_no": 44, "project_id": MAIN_PROJECT,
     "verify_terms": ["风轨"],
     "notes": "跨章节事件：第19章风轨首次展示，第44章标题『风轨，飞崖！』风轨进阶使用"},
    {"difficulty": "cross_chapter", "category": "character_profile", "query": "唐月第16章成为新老师，正文里她主修什么系？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["唐月", "火系", "暗影系"], "chapter_no": 16, "project_id": MAIN_PROJECT,
     "verify_terms": ["唐月"],
     "notes": "跨章节聚合：第16章『新女老师，唐月』；facts 称唐月主修火系，另有施展暗影系技能描述"},
    {"difficulty": "cross_chapter", "category": "source_chunk", "query": "莫凡的大魔头外号出现在哪章？冲榜狂魔呢？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["莫凡", "大魔头", "冲榜狂魔"], "project_id": MAIN_PROJECT,
     "verify_terms": ["大魔头", "冲榜狂魔"],
     "notes": "跨章节聚合：大魔头=第171章起，全校公敌=第188章，冲榜狂魔=第519章；需聚合多个章节的外号证据"},
    {"difficulty": "cross_chapter", "category": "timeline_event", "query": "第3章开学大典与第4章法系觉醒相隔多久？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["开学大典", "法系觉醒"], "chapter_no": 3, "project_id": MAIN_PROJECT,
     "verify_terms": ["开学大典"],
     "notes": "跨章节推理：第3章『开学大典』九月一号开学，第4章『法系觉醒』20个班级同时进行觉醒仪式，开学后随即觉醒"},
    {"difficulty": "cross_chapter", "category": "character_first_appearance", "query": "莫凡第1章得知魔法觉醒在开学典礼开启，第4章实际觉醒了什么系？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["莫凡", "火系", "雷系"], "chapter_no": 4, "project_id": MAIN_PROJECT,
     "verify_terms": ["魔法觉醒", "开学典礼"],
     "notes": "跨章节聚合：第1章『世界大变』交代魔法觉醒在高中开学典礼开启，第4章『法系觉醒』莫凡实际觉醒火系+雷系"},
    {"difficulty": "cross_chapter", "category": "ability_query", "query": "穆白第25章释放冰蔓时，莫凡释放的技能是什么？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["莫凡", "火滋", "穆白", "冰蔓"], "chapter_no": 25, "project_id": MAIN_PROJECT,
     "verify_terms": ["火滋", "冰蔓"],
     "notes": "跨章节对比：第25章『冰系，冰蔓！』穆白释放冰蔓，同章莫凡完成火滋的释放（华丽大翻身）"},
    {"difficulty": "cross_chapter", "category": "ability_query", "query": "莫凡的天生双系是哪两系？他在第10章用雷系释放了什么？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["莫凡", "雷系", "火系", "雷印"], "chapter_no": 10, "project_id": MAIN_PROJECT,
     "verify_terms": ["天生双系", "雷印"],
     "notes": "跨章节聚合：第5/6章揭示天生双系=火系+雷系，第10章『释放，雷印』用雷系释放雷印"},
    {"difficulty": "cross_chapter", "category": "timeline_event", "query": "莫凡觉醒火系（第4章）到完成火滋释放（第25章）经历了多少章？",
     "evidence_type": "source_chunk", "source_ids": [MAIN_SOURCE],
     "entities": ["莫凡", "火系", "火滋"], "chapter_no": 25, "project_id": MAIN_PROJECT,
     "verify_terms": ["火滋"],
     "notes": "跨章节推理：第4章觉醒火系 → 第25章完成火滋释放，间隔约21章；需跨章节聚合作答"},

    # ── no_result：查无结果（实体词/probe 短语均经全库检索确认 0 命中，生成时再次核对）──
    {"difficulty": "no_result", "category": "no_result", "query": "王小明是什么系的法师？",
     "evidence_type": "source_chunk", "source_ids": [], "entities": ["王小明"], "probe": "王小明", "project_id": MAIN_PROJECT,
     "verify_terms": [],
     "notes": "查无结果：『王小明』全库 0 命中，期望检索不召回任何证据（hit=False 为正确行为）"},
    {"difficulty": "no_result", "category": "no_result", "query": "李小雨是哪个角色？",
     "evidence_type": "source_chunk", "source_ids": [], "entities": ["李小雨"], "probe": "李小雨", "project_id": MAIN_PROJECT,
     "verify_terms": [],
     "notes": "查无结果：『李小雨』全库 0 命中，期望检索不召回证据"},
    {"difficulty": "no_result", "category": "no_result", "query": "林黛玉在这个世界会魔法吗？",
     "evidence_type": "source_chunk", "source_ids": [], "entities": ["林黛玉"], "probe": "林黛玉", "project_id": MAIN_PROJECT,
     "verify_terms": [],
     "notes": "查无结果：『林黛玉』全库 0 命中（异世界角色），期望检索不召回证据"},
    {"difficulty": "no_result", "category": "no_result", "query": "莫凡的爷爷叫什么名字？",
     "evidence_type": "source_chunk", "source_ids": [], "entities": ["莫凡", "爷爷"], "probe": "莫凡的爷爷", "project_id": MAIN_PROJECT,
     "verify_terms": [],
     "notes": "查无结果：短语『莫凡的爷爷』全库 0 命中（正文只提莫家兴，无爷爷信息；单词莫凡/爷爷在语料中存在，检索需按组合不召回）"},
    {"difficulty": "no_result", "category": "no_result", "query": "乾坤袋是什么法器？",
     "evidence_type": "source_chunk", "source_ids": [], "entities": ["乾坤袋"], "probe": "乾坤袋", "project_id": MAIN_PROJECT,
     "verify_terms": [],
     "notes": "查无结果：『乾坤袋』全库 0 命中（小说无此设定），期望检索不召回证据"},
    {"difficulty": "no_result", "category": "no_result", "query": "金丹期是什么境界？",
     "evidence_type": "source_chunk", "source_ids": [], "entities": ["金丹期"], "probe": "金丹期", "project_id": MAIN_PROJECT,
     "verify_terms": [],
     "notes": "查无结果：『金丹期』全库 0 命中（非本世界修炼体系），期望检索不召回证据"},
    {"difficulty": "no_result", "category": "no_result", "query": "炼丹术在这个世界存在吗？",
     "evidence_type": "source_chunk", "source_ids": [], "entities": ["炼丹术"], "probe": "炼丹术", "project_id": MAIN_PROJECT,
     "verify_terms": [],
     "notes": "查无结果：『炼丹术』全库 0 命中，期望检索不召回证据"},
    {"difficulty": "no_result", "category": "no_result", "query": "查克拉是什么？",
     "evidence_type": "source_chunk", "source_ids": [], "entities": ["查克拉"], "probe": "查克拉", "project_id": MAIN_PROJECT,
     "verify_terms": [],
     "notes": "查无结果：『查克拉』全库 0 命中（外来世界观名词），期望检索不召回证据"},
    {"difficulty": "no_result", "category": "no_result", "query": "张伟是谁？",
     "evidence_type": "source_chunk", "source_ids": [], "entities": ["张伟"], "probe": "张伟", "project_id": MAIN_PROJECT,
     "verify_terms": [],
     "notes": "查无结果：『张伟』全库 0 命中，期望检索不召回证据"},
]


def _chunk_like_count(conn: sqlite3.Connection, term: str, table: str) -> int:
    """只读统计某个词在指定表里 LIKE 命中的行数（全库）。"""
    if table == "project_source_chunks":
        sql = "SELECT COUNT(*) FROM project_source_chunks WHERE content LIKE ?"
        args = (f"%{term}%",)
    elif table == "project_knowledge_facts":
        sql = "SELECT COUNT(*) FROM project_knowledge_facts WHERE evidence_text LIKE ? OR subject LIKE ? OR object LIKE ?"
        args = (f"%{term}%",) * 3
    else:  # project_source_chapters
        sql = "SELECT COUNT(*) FROM project_source_chapters WHERE content LIKE ? OR chapter_title LIKE ?"
        args = (f"%{term}%",) * 2
    return int(conn.execute(sql, args).fetchone()[0])


def verify_hard_defs(conn: sqlite3.Connection) -> list[str]:
    """生成时对困难样本引用词做只读核对，返回警告列表。

    - no_result 样本：实体词在检索语料（project_source_chunks）0 命中才有效；
      若有命中说明样本失效，需人工更换实体。
    - 其余样本：verify_terms 中的引用词需出现在语料/事实/章节任一表中；
      全部 0 命中说明 notes 引用的语料已变化，记入警告（不阻断生成）。
    """
    warnings: list[str] = []
    counts: dict[str, int] = {}
    for d in HARD_SAMPLE_DEFS:
        diff = d["difficulty"]
        counts[diff] = counts.get(diff, 0) + 1
        qid = f"{HARD_PREFIX[diff]}-{counts[diff]:03d}"
        if diff == "no_result":
            probe = d.get("probe") or (d["entities"][0] if d["entities"] else "")
            if probe:
                n = _chunk_like_count(conn, probe, "project_source_chunks")
                if n > 0:
                    warnings.append(f"{qid} no_result 探测短语 {probe!r} 在 project_source_chunks 有 {n} 处命中，no_result 样本失效，需更换")
        else:
            for term in d.get("verify_terms") or []:
                n = sum(
                    _chunk_like_count(conn, term, t)
                    for t in ("project_source_chunks", "project_knowledge_facts", "project_source_chapters")
                )
                if n == 0:
                    warnings.append(f"{qid} 引用词 {term!r} 在三张表（chunks/facts/chapters）均 0 命中，notes 可能过期")
    return warnings


def build_hard_samples(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """按 HARD_SAMPLE_DEFS 构造困难样本。返回 (samples, per_difficulty_counts)。"""
    samples: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for d in HARD_SAMPLE_DEFS:
        diff = d["difficulty"]
        counts[diff] = counts.get(diff, 0) + 1
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
            "query_id": f"{HARD_PREFIX[diff]}-{counts[diff]:03d}",
            "category": d["category"],
            "query": d["query"],
            "expected": exp,
            "difficulty": diff,
            "notes": d["notes"],
        })
    return samples, counts


def _readonly_conn(db_path: str) -> sqlite3.Connection:
    """只读打开 SQLite：mode=ro 时 SQLite 拒绝任何写操作，绝不修改库。"""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _load_json(raw: Optional[str]) -> Any:
    """解析 JSON 列；失败时原样返回（部分脏数据是普通字符串）。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _entity_list(raw: Optional[str]) -> list[str]:
    """把 characters/aliases 等 JSON 数组列规范为字符串列表。"""
    val = _load_json(raw)
    if isinstance(val, list):
        return [str(x) for x in val if x]
    if isinstance(val, str):
        return [val]
    return []


def fetch_table(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    """读取一张表全部行（dict 化，含项目/来源/主键信息）。"""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM {table} ORDER BY rowid"
    ).fetchall()
    return [dict(zip(cols, row)) for row in rows]


def query_for_ability(row: dict[str, Any], seq: int) -> list[dict[str, Any]]:
    """ability_profile 行 → 1-2 条查询。"""
    cname, aname = row["character_name"], row["ability_name"]
    level = row.get("level_desc") or "未知等级"
    chapter = row.get("first_seen_chapter")
    queries = [
        {
            "query_id": f"reg-{seq:03d}",
            "category": "ability_query",
            "query": f"{cname}的{aname}是什么等级？",
            "expected": {
                "evidence_type": "ability_profile",
                "chapter_no": chapter,
                "source_ids": [row["source_id"]] if row.get("source_id") else [],
                "entities": [cname, aname],
            },
            "notes": f"生成自 ability_profile：{cname} 掌握 {aname}（{level}，状态={row.get('status')}）",
        }
    ]
    if chapter is not None:
        queries.append({
            "query_id": f"reg-{seq:03d}b",
            "category": "character_first_appearance",
            "query": f"{cname}是什么时候掌握{aname}的？",
            "expected": {
                "evidence_type": "ability_profile",
                "chapter_no": chapter,
                "source_ids": [row["source_id"]] if row.get("source_id") else [],
                "entities": [cname, aname],
            },
            "notes": f"生成自 ability_profile.first_seen_chapter={chapter}（{cname} 掌握 {aname} 的章节）",
        })
    return queries


def query_for_event(row: dict[str, Any], seq: int) -> list[dict[str, Any]]:
    """event_timeline 行 → 1-2 条查询。"""
    ch = row["chapter_no"]
    entities = _entity_list(row.get("characters"))
    title = row.get("event_title") or ""
    queries = [
        {
            "query_id": f"reg-{seq:03d}",
            "category": "timeline_event",
            "query": f"第{ch}章发生了什么？",
            "expected": {
                "evidence_type": "event_timeline",
                "chapter_no": ch,
                "source_ids": [row["source_id"]] if row.get("source_id") else [],
                "entities": entities,
            },
            "notes": f"生成自 event_timeline：第{ch}章『{title}』，出场角色={entities}",
        }
    ]
    if entities:
        queries.append({
            "query_id": f"reg-{seq:03d}b",
            "category": "timeline_event",
            "query": f"第{ch}章有哪些角色出场？",
            "expected": {
                "evidence_type": "event_timeline",
                "chapter_no": ch,
                "source_ids": [row["source_id"]] if row.get("source_id") else [],
                "entities": entities,
            },
            "notes": f"生成自 event_timeline.characters：第{ch}章出场角色={entities}",
        })
    return queries


def query_for_character(row: dict[str, Any], seq: int) -> list[dict[str, Any]]:
    """character_profile 行 → 1-2 条查询。"""
    name = row["name"]
    aliases = _entity_list(row.get("aliases"))
    queries = [
        {
            "query_id": f"reg-{seq:03d}",
            "category": "character_profile",
            "query": f"介绍下{name}",
            "expected": {
                "evidence_type": "character_profile",
                "source_ids": [row["source_id"]] if row.get("source_id") else [],
                "entities": [name] + aliases,
            },
            "notes": f"生成自 character_profile：{name}（{row.get('identity_desc') or '无身份描述'}，"
                     f"状态={row.get('status_desc') or '无'}，别名={aliases}）",
        },
        {
            "query_id": f"reg-{seq:03d}b",
            "category": "character_profile",
            "query": f"{name}是什么角色？",
            "expected": {
                "evidence_type": "character_profile",
                "source_ids": [row["source_id"]] if row.get("source_id") else [],
                "entities": [name] + aliases,
            },
            "notes": f"生成自 character_profile：{name}",
        },
    ]
    return queries


def query_for_world(row: dict[str, Any], seq: int) -> list[dict[str, Any]]:
    """world_rule 行 → 1-2 条查询。"""
    category = row["category"]
    rule_text = row.get("rule_text") or ""
    queries = [
        {
            "query_id": f"reg-{seq:03d}",
            "category": "world_rule",
            "query": f"这个世界里{category}的规则是什么？",
            "expected": {
                "evidence_type": "world_rule",
                "chapter_no": row.get("chapter_no"),
                "source_ids": [row["source_id"]] if row.get("source_id") else [],
                "entities": [category],
            },
            "notes": f"生成自 world_rule：{category} / {rule_text}（priority={row.get('priority')}）",
        },
        {
            "query_id": f"reg-{seq:03d}b",
            "category": "world_rule",
            "query": f"这个世界的{category}设定是怎么样的？",
            "expected": {
                "evidence_type": "world_rule",
                "chapter_no": row.get("chapter_no"),
                "source_ids": [row["source_id"]] if row.get("source_id") else [],
                "entities": [category],
            },
            "notes": f"生成自 world_rule：{category} / {rule_text}",
        },
    ]
    return queries


def build_samples(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """遍历四张表生成样本。返回 (samples, table_counts)。"""
    table_counts: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    seq = 0

    for table, evidence_type, _name_cols, _json_cols in TABLES:
        rows = fetch_table(conn, table)
        table_counts[table] = len(rows)
        for row in rows:
            seq += 1
            if table == "ability_profile":
                queries = query_for_ability(row, seq)
            elif table == "event_timeline":
                queries = query_for_event(row, seq)
            elif table == "character_profile":
                queries = query_for_character(row, seq)
            elif table == "world_rule":
                queries = query_for_world(row, seq)
            else:  # pragma: no cover
                queries = []

            for q in queries:
                # 附加溯源扩展字段（基线运行器不读取）
                q["expected"]["project_id"] = row.get("project_id")
                q["expected"]["record_ids"] = [str(row["id"])]
                q["expected"]["evidence_type"] = evidence_type

                # 已知问题行覆盖 difficulty / notes
                override = DIFFICULTY_OVERRIDES.get((table, str(row["id"])))
                if override:
                    q["difficulty"] = override["difficulty"]
                    q["notes"] = f"{override['notes']}；{q['notes']}"
                else:
                    q["difficulty"] = "normal"

                if not q["expected"]["source_ids"]:
                    q["expected"]["source_ids"] = [str(row["id"])]
                samples.append(q)

    return samples, table_counts


def main(argv: list[str]) -> int:
    db_path = str(Path(argv[0]).resolve()) if argv else str(DEFAULT_DB)
    out_path = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_OUT
    if not Path(db_path).exists():
        print(f"[error] 数据库不存在: {db_path}", file=sys.stderr)
        return 1

    conn = _readonly_conn(db_path)
    try:
        samples, table_counts = build_samples(conn)
        hard_samples, hard_counts = build_hard_samples(conn)
        hard_warnings = verify_hard_defs(conn)
        samples.extend(hard_samples)
    finally:
        conn.close()

    for w in hard_warnings:
        print(f"[warn] {w}", file=sys.stderr)

    meta = {
        "generator": "generate_regression.py",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "db_path": str(db_path),
        "table_row_counts": table_counts,
        "difficulty_override_count": len(DIFFICULTY_OVERRIDES),
        "hard_sample_counts": hard_counts,
        "hard_sample_warnings": hard_warnings,
        "caveats": [
            "character_alias_clusters 表当前为空(0行)，alias 类困难样本的别名全部从正文提取核对（猴子/泥猴子/大魔头/全校公敌/冲榜狂魔/绿茶男/黑白无常妇/冷面判官/移动电灯泡/凡哥/莫凡哥）",
            "typo 类样本前 5 条（张小候/张候/慕白/雷息/风鬼）的错别字来自语料自身（SELECT 核对有命中），其余为近形字构造",
            "homophone 类样本为同音字构造（凡→烦/侯→猴/满→曼/穆→木/唐→汤 等），其中『张小猴』与真实别名『猴子』重合，用于检验别名误导",
            "same_name/conflict 样本涉及三个项目：d62d4447(全职法师全文) 与 49aee26a(E2E)、de04afc8(测试小说) 存在同名角色(莫凡/张小侯/赵满延)，设定不同",
            "张小侯风系 first_seen_chapter 在主项目为 16、E2E 项目为 1、正文第19章才首次展示风轨，三处不一致，相关样本 difficulty=conflict",
            "project_knowledge_facts 主语噪声大（'一些'/'一位'/'一名'等），未作为本数据集证据来源",
            "expected.source_ids 为 source 级 id(project_sources.id)，基线命中规则 A 可命中；record_ids 为结构化表行 id，仅溯源用",
            "no_result 样本期望检索不召回（hit=False 为正确行为），生成时已对实体词做全库 0 命中核对",
        ],
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "samples": samples}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    by_difficulty: dict[str, int] = {}
    for s in samples:
        by_difficulty[s["difficulty"]] = by_difficulty.get(s["difficulty"], 0) + 1
    print(f"[ok] 样本 {len(samples)} 条已写入: {out_path}")
    print(f"    表行数: {table_counts}")
    print(f"    困难样本按类分布: {hard_counts}")
    print(f"    按难度分布: {by_difficulty}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
