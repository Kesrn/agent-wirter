# 剪映 Skill 选型 · GitHub 候选与本机兼容性

> **查于**：2026-10-03
> **结论先行**：候选有 7 个，但**你这台机器上能真正跑通的只有 UI 自动化那一个** —— 因为新版剪映的草稿文件不是明文了。

---

## 一、GitHub 候选清单（按推荐度）

| # | 仓库 | 路线 | 热度 | 适配你的机器？ |
|---|---|---|---|---|
| 1 | **`mcncarl/yichen-skills@yichen-jianying-editor`** | **UI 自动化**（Computer Use 驱动剪映界面） | 27 installs | ✅ **明确为 macOS 写的**，还专门处理了 macOS 中文文件名导入问题 |
| 2 | `AiPersonacademy/capcut-mcp` | **MCP 服务**（一整套 capcut_* 工具） | — | ⚠️ 工具集最完整，但底层仍写草稿文件，受版本影响 |
| 3 | `luoluoluo22/jianying-editor-skill` | pyJianYingDraft 写草稿 | 3.6K star / 2K installs / MIT | ❌ README 自述：macOS 仅实验性、**不支持自动导出** |
| 4 | `cclank/lanshu-waytovideo@jianying-video-gen` | 草稿生成 | 547 installs | ❌ 同上，版本依赖 |
| 5 | `renezander030/capcut-cli@capcut-edit` | 命令行 | 347 installs | ❌ 面向 CapCut 国际版 |
| 6 | `gspandy/jianying-editor-skill` | 同 #3 的 fork | — | ❌ 同 #3 |
| 7 | `openclaw/skills` → `jianying-video-compose` | 私有 HTTP API | — | ❌ **依赖东方财富的内部接口**，不建议 |

**安装命令**

```bash
# 1（推荐）
npx skills add mcncarl/yichen-skills --skill yichen-jianying-editor

# 2（MCP 路线）
git clone https://github.com/AiPersonacademy/capcut-mcp
```

---

## 二、本机兼容性实测（这一步否掉了大半候选）

### 你的剪映版本

| 项 | 值 |
|---|---|
| 应用路径 | `/Applications/VideoFusion-macOS.app` |
| Bundle ID | `com.lemon.lvpro` |
| **版本** | **11.5.13252**（CFBundleVersion 11.6.0-beta4） |

### 草稿文件状态（关键）

逐日对照了四个草稿的时间轴主文件：

| 草稿 | `draft_info.json` | 是否明文 |
|---|---|---|
| 10月3日 | 3,760 B | ❌ 非明文（首字符 `2`） |
| 9月19日 | 567,668 B | ❌ 非明文（首字符 `B`） |
| 8月28日 | 355,104 B | ❌ 非明文（首字符 `a`） |
| 8月7日 | 6,948 B | ❌ 非明文（首字符 `D`） |

补充证据：

- 草稿目录里存在 `crypto_key_store.dat` + `.bak`
- **全盘找不到任何 `draft_content.json`**（老版剪映的明文草稿文件）
- `Timelines/project.json` 虽是明文 JSON，但只有 **479 字节**，只是配置壳（color_space 等），不含时间轴
- 结论对四个日期一致 → **不是近期版本变化，是一直如此**

**【推断】** 新版剪映对草稿时间轴做了加密/编码。我没有去逆它的算法，也不需要 —— **「全部草稿均非明文 + 存在密钥存储文件 + 无明文草稿文件」这三条已经足够下判断**。

### 这否掉了什么

候选 #3 #4 #5 #6 全部基于 **pyJianYingDraft**：直接读写 `draft_content.json` / `draft_info.json`。**在这台机器上，它们写出来的草稿剪映很可能根本认不出。**

叠加 #3 自己的 README 声明：自动导出**只支持剪映 5.9 及以下 + Windows**。你现在是 **11.5 的 Mac 版** —— 版本和平台两条都够不上。

---

## 三、推荐路线

### 首选：#1 `yichen-jianying-editor`

**为什么**：

- 它**不碰草稿文件**，用 Computer Use 直接驱动 `com.lemon.lvpro` 的界面 —— 版本加密对它没影响
- 作者明确处理了 macOS 的坑（中文文件名在 macOS 文件选择器里会出问题，它会先复制成英文名再导入）
- 功能覆盖你需要的：导入素材、上时间轴、裁切/缩放、字幕、标题、贴纸、音效、配乐、手动修剪、导出
- 每次操作还会写结构化日志（Action / Details / Decision / Next），能断点续做

**已知边界（作者自述）**：不做 ASR 转写和自动粗剪，那些属于另一个火山 ASR 技能；它只负责在剪映里"精修 + 导出"。

### 次选：#2 `capcut-mcp`

工具集最完整（建草稿、加片段、变换、变速、关键帧、转场、SRT 字幕、无头预览导出），而且是 **MCP 形态**，接进 Agent 最自然。**但它的实现仍落回草稿文件** → 大概率撞上同一个加密问题。如果要用，先拿一个空草稿做最小验证。

### 不推荐

- #3 系（含 #6 fork）：版本 + 平台双重不匹配
- #7：依赖第三方公司内部接口，随时会挂

---

## 四、下一步

1. 先装 #1 试一条最短链路：**导入一个 mp4 → 上时间轴 → 导出**。这一步能通，后面才值得往下走
2. 如果 #1 通了：把「H3 出片 → 剪映自动拼接」接起来
3. 如果想走 MCP（#2）：先单独验证「能不能建出一个剪映能识别的草稿」，再谈其他

---

## 五、附：截图里的原始检索结果

```
luoluoluo22/jianying-editor-skill@jianying-editor   2K installs
cclank/lanshu-waytovideo@jianying-video-gen        547 installs
renezander030/capcut-cli@capcut-edit               347 installs
mcncarl/yichen-skills@yichen-jianying-edit          47 installs
mcncarl/yichen-skills@yichen-jianying-editor        27 installs
skills.volces.com@jianying-auto-editor              20 installs
```
