# AutoPPT 网页服务化：仓库分析与开发清单

> 编写日期：2026-10-16 · 基于提交 `482e7c7` 及当前工作区 · 状态：规划草案；P0 出图实测已完成，P1 进行中
>
> **2026-10-16 决定：全部由 Codex 执行**（规划、出图、重建、验收都通过 `codex exec`，出图用 Codex
> 内置 `image_gen`，内测期使用本机 Codex 账号池）。Claude Agent SDK 路线暂停，下文中与 Claude 相关的
> 内容保留作为备选。实测记录见 [p0-codex-imagegen-probe.md](p0-codex-imagegen-probe.md)。
>
> **2026-10-17 决定：网页服务放在独立仓库 `autoppt-web`**，原来的工作流和 skills 仓库（`autoppt`）一律不改，
> 只作为只读依赖（`AUTOPPT_REPO`）。下文所说的“仓库改造”都在 `autoppt-web` 中实现；需要原仓库配合的地方
> （例如 skill 规则）先在服务侧适配，确实需要改原仓库时单独征求同意。

## 0. 结论

**可以做。** 仓库已经覆盖“资料 → 规划 → ImageGen 逐页母版 → 原生可编辑重建 →
PowerPoint 实渲验收”的完整能力。问题在于它目前是一条**由代理（Codex 会话）交互驱动**
的工作流，而不是一个能直接调用的服务。网页化的主要工作不在页面本身，而是把这条工作流
改造成可排队、可计费、可在人工审核点中断后恢复、多用户之间互相隔离的无人值守任务。

开工前必须先处理的 5 个约束：

1. **“号池”合规风险。** 把多个 ChatGPT/Codex 或 Claude 订阅账号汇总起来服务付费用户，
   违反服务条款，有封号风险。Anthropic 的 Agent SDK 文档写明：未经事先批准，第三方产品
   （包括基于 Claude Agent SDK 构建的产品）不得提供 claude.ai 登录或使用其订阅额度，
   必须用 API key 鉴权。OpenAI 的使用条款同样禁止把账号提供给他人使用（上线前请自行核对
   最新条款）。→ 生产环境用 API key 按量计费，“号池”改成“API key 池 + 并发/限额调度”。
2. **出图只能走 Codex 内置 `image_gen`。** `AGENTS.md` 和 `builtin_imagegen_policy` 禁止任何
   图片 API、代理或替代后端。→ 已决定全流程使用 Codex；2026-10-17 实测 `codex exec` 可以无人值守出图，
   来源记录可以通过 rollout 校验（见 D1）。
3. **依赖 Windows + PowerPoint。** 正式验收（渲染、真实编辑证明、图表工作簿证明、去字
   反证）都通过 PowerPoint COM 完成，所以 Worker 必须是装了 Office 的 Windows 机器。
   PowerPoint 是单实例程序，一个登录会话同一时间只能稳定跑一个 Office 任务。微软不支持
   无人值守的服务端 Office 自动化，因此需要自建看门狗，并单独评估 Office 授权。
4. **单个任务的成本和耗时都很高。** 最近一次 14 页答辩稿
   （`runs/zhangzhengxuan_defense_20261015_v66benchmark_r3`）从 2026-10-14 10:07 跑到
   10-15 10:04，约 24 小时；原生重建 15 轮，产生 840 个文件、713 MB，期间还写了多份只针对
   该项目的修复脚本。v66（0/20）和张正轩 v3（0/14）都没有通过严格像素或近零门禁，最终是
   在用户看过预览并批准后，以 `native-priority-approved` 层级交付的。→ 产品不能承诺 Gold
   或“逐像素一致”。应按实际能达到的层级定价，并在 P1 阶段实测单页成本和时长。
5. **人工审核本身就是工作流的一环。** 包括大纲与证据核对、母版审核、原生预览批准。
   `native-priority-approved` 层级要求记录用户原话、所看的预览和母版 hash。→ 网页必须
   实现“审核检查点”，这同时也是产品的卖点。

---

## 1. 仓库现状

### 1.1 可直接复用

| 能力 | 位置 | 在服务中的用途 |
| --- | --- | --- |
| 6 个使用场景档案（含默认页数、单页时长、叙事顺序、禁用表述） | `autoppt_workflow/presentation/profiles.py` | 表单“使用场景”选项与默认值 |
| 11 个风格档案 | `autopptskills/scripts/style_contracts.py` → `STYLE_PROFILES` | 表单“风格”选项 |
| DOCX 段落/表格/图片抽取 | `autopptskills/scripts/extract_docx_evidence.py`、`scripts/extract_thesis_evidence.py` | 文档解析 |
| 证据台账、Slide IR、逐页提示词 | `presentation/orchestrator.py`、`ppt/image_prompt_builder.py`、`scripts/make_deck_spec.py` | 规划阶段 |
| 内置 ImageGen 交接与来源校验 | `ppt/builtin_imagegen_handoff.py`、`verify_imagegen_first_gate.py` | 母版阶段 |
| 图片版 PPTX 组装 | `autopptskills/scripts/build_image_deck.py` | 图片版交付 |
| 测量式与原生重建 | `compose_measured_reconstruction.py`、`compose_editable_pptx.*`、`examples/zhicang_native/` | 可编辑阶段 |
| Office 渲染与各类门禁 | `render_pptx_powerpoint.ps1`、`semantic_mutation_gate.py`、`pptx_office_text_bounds.py`、`pixel_/near_pixel_fidelity_gate.py`、`release_gate.py` | 验收阶段 |
| 讲稿、答辩脚本、预测问题 | `orchestrator.build_defense_materials()` | 附加服务 |
| 单页重做 | `ppt_adapter.regenerate_ppt_slide()`、`merge_high_fidelity_round.py` | 用户请求“重做第 N 页” |
| mock 模式 | `run_presentation_workflow.py --mock` | 前后端联调与 CI，不产生模型费用 |
| 经验闭环 | `improvement/`、`vectorization_history.py` | 需要改造成多租户 |
| 回归测试 | `tests/`（pytest） | CI |

### 1.2 缺失或需要改造

- 没有任何 Web、API、数据库或队列代码。
- 输入解析只支持 DOCX，缺 PDF（含扫描件 OCR，可借用 `windows_ocr_layout.ps1`）、PPTX、
  XLSX/CSV、Markdown/TXT、图片、ZIP。
- `run_ppt()` 要求主题目录里已经有 `presentation_handoff/*定稿.md`、`slide_brief.json`、
  `evidence-ledger.json`，这些原本由上游的科研工作流生成。服务模式下需要一个
  **“需求 + 文件 → 主题目录”** 的 intake 步骤。
- 没有无人值守模式的约定：目前代理会直接在对话里向用户提问；服务模式下必须改为写入
  检查点文件后停下。
- 没有结构化的进度输出，前端无法展示进度。
- 没有给网页用的预览产物（缩略图、母版与成品对比图、联系表）。
- 没有“用户自带母版”（非 ImageGen 生成）的来源类型，矢量化服务需要它。
- 没有成本记录（token、费用、Office 占用分钟数）。
- 场景档案只覆盖学术类，没有商业路演或企业汇报。
- `AGENTS.md` 面向 Codex，包含智仓兴教专属的续跑说明，并要求把经验写入受 Git 跟踪的
  全局台账。多用户场景下这会把客户内容写进共享知识库，各任务之间也会写冲突。
- 仓库里存有真实个人资料（`deliverables/张正轩_*`、`reviews/`、`runs/`、`交付/`），
  不能进入 Worker 镜像。

---

## 2. 开工前需要拍板的决策

| # | 决策 | 选项 | 建议 |
| --- | --- | --- | --- |
| D1 | 测试期怎么出图 | A. Claude 负责规划、重建和验收，母版这一步由 Worker 调用 `codex exec` 出图；B. 测试期让 Claude 直接生成原生 PPT，不经过 ImageGen 母版，作为独立的“原生直出（测试）”层级，不得标注为 ImageGen 版；C. 改规则允许官方图片 API，并新增一种来源类型 | **已定（2026-10-16）：全部由 Codex 执行**，比原方案 A 更进一步，规划、重建和验收也交给 `codex exec`。2026-10-17 已实测无人值守出图可用 |
| D2 | 生产期如何接入模型 | API key 按量计费 / 订阅账号池 | 内测期：**用户本机 Codex 账号池**（每个账号一个 `CODEX_HOME`）。对外商用前仍需按第 0 节第 1 条重新评估 |
| D3 | 矢量化服务的母版来源 | 用户上传的图片版 PPT 或截图不是 ImageGen 产物，过不了 ImageGen-first 门禁 | 新增 `user_supplied_master` 来源类型和对应的交付层级（需要改 `SKILL.md` 和门禁脚本）；另一种做法是先用 ImageGen 重绘再重建，但画面会变 |
| D4 | 目标市场 | 中国大陆 / 海外 | 决定支付方式、备案要求、模型服务可用地区和部署云 |
| D5 | 首发场景范围 | 仅学术类 6 个场景 / 加入商业类 | 先上学术类，商业类之后补 profile |
| D6 | 上架哪些交付层级 | 规划包 / 图片版 / 原生可编辑 / Gold | 上前三档，Gold 不作为自动化商品 |
| D7 | v66 基准稿能否作为参考图 | 张正轩那次运行把 v66 联系表附进了 ImageGen 提示词 | 先确认智仓兴教内容的权属；如果属于第三方，服务模式下不能发给其他用户的任务 |

---

## 3. 产品定义

### 3.1 服务类型

1. **新建 PPT**：资料（可以没有）加描述文字，生成 PPT。
2. **PPT 矢量化**：把已有的图片版 PPT、截图或 PDF 转成原生可编辑的 PPTX。
3. **附加服务**：讲稿与答辩脚本、预测问题、单页重做、追加修改轮次、加急。

### 3.2 交付层级（与仓库层级一一对应）

| 网页档位 | 仓库层级 | 用户得到什么 | 必经检查点 |
| --- | --- | --- | --- |
| 规划包 | Planning package | 大纲、逐页内容、证据映射、逐页提示词 | 大纲确认（可选） |
| 图片版 | Image-only deck | 每页一张完整图片的 PPTX | 母版审核 |
| 原生可编辑版 | `native-priority-approved` | 文字、形状、图表原生可改；照片等作为可替换图片；附逐页对比证据和披露说明 | 母版审核 + **原生预览批准（强制，不能自动通过）** |
| Gold | Gold editable release | 严格门禁全部通过 | 不上架，最多作为人工精修服务 |

页面上要写清楚：原生可编辑版与母版之间存在字体和局部图形差异，这是经过用户确认后
接受的；产品不承诺逐像素一致。

### 3.3 用户需求字段

**必填**

| 字段 | 说明 | 对应仓库 |
| --- | --- | --- |
| 服务类型 | 新建 / 矢量化 | 路由 |
| 描述文字 | 没上传文档时必填，建议不少于 200 字：主题、目的、核心结论、关键数据。有文档时作为“重点说明” | intake → 证据台账（标注为“用户陈述”，不当作事实来源） |
| 使用场景 | 科创比赛答辩 / 毕业论文答辩 / 国自然申请 / 结题答辩 / 论文解读 / 组会报告 | `PRESENTATION_PROFILES` |
| 页数 | 三种方式：固定页数、范围、按演讲时长推算（用档案里的 `default_duration_seconds`） | `slide_brief` |
| 风格 | 默认 Academic Light（参考 v66）；其余风格标出适用场景；深色和极简风格需要用户主动选择 | `STYLE_PROFILES` |
| 交付档位 | 规划包 / 图片版 / 原生可编辑版 | 交付层级 |

**建议填写**

| 字段 | 说明 |
| --- | --- |
| 上传文件（按角色标注） | 主资料（论文/报告/申请书）、补充资料、图表数据、图片素材（实验图、照片、系统截图）、Logo、风格参考（参考 PPT/截图/学校模板）、待矢量化文件 |
| 受众 | 答辩委员会 / 评审专家 / 导师与组员 / 比赛评委 / 投资人 |
| 演讲时长 | 用来校验页数是否合理 |
| 语言 | 中文 / 英文 / 中英双语 |
| 封面信息 | 标题、副标题、汇报人、导师、单位、日期，作为精确文字进入白名单 |
| 必须包含 / 必须避免 | 必讲的点、必用的图、禁用的表述 |
| 原图保留 | 指定的实验图或照片作为有边界的图片保留，不让 ImageGen 重绘（档案规则：禁止装饰性重绘原始图表） |
| 数据图表 | 上传 xlsx/csv，或在页面上填表，用来生成带嵌入工作簿的原生图表（v66 基准要求） |

**高级选项**

| 字段 | 说明 |
| --- | --- |
| 画幅 | 16:9（默认） / 4:3 |
| 字体策略 | 接受 Office 字体（微软雅黑 / 等线）替代母版字形。原生可编辑版必须勾选 |
| 信息密度 | 中高（默认） / 中 / 低 |
| 审核节点 | 大纲确认开或关；母版审核开或关；原生预览批准不能关 |
| 讲稿与预测问题 | 附加项，单独计费 |
| 数据保留期 | 3 / 7 / 30 天后删除；是否允许脱敏后作为案例展示（默认不允许） |
| 截止时间 | 决定能否下单以及是否需要加急 |

**表单校验规则**

- 没有文档且描述过短时，提示补充。只凭描述生成的内容必须标注为“框架或示意”，
  不能编造数据。
- 页数和内容冲突时，在大纲阶段提示用户，不能静默删减内容（仓库规则：不截断已审核内容）。
- 使用学校 Logo 或 VI 前，用户要确认自己有使用权；`official-source-archetypes` 对机构
  标识有限制，所以不提供学校品牌风格，改为让用户上传自己的学校模板。
- 设置上限：页数（例如 ≤ 30）、单文件大小、总大小、PDF 页数。

**支持的文件格式**

| 格式 | 处理方式 |
| --- | --- |
| DOCX | 复用现有抽取器 |
| PDF | 文本层直接抽取；扫描件走 OCR；抽出内嵌图片 |
| PPTX | 抽文字和图片；可以作为资料、风格参考，或矢量化的源文件 |
| XLSX / CSV | 图表数据，生成原生图表 |
| MD / TXT / TEX | 当作文本资料 |
| PNG / JPG / WebP / HEIC | 图片素材或矢量化源文件（HEIC 先转码） |
| ZIP | 解包时防路径穿越和压缩炸弹，再按扩展名分发 |
| 网页链接 | 第一版不做（有 SSRF 风险） |

### 3.4 需求 JSON（前端、后端、Worker 共用一份契约）

```json
{
  "schema_version": "autoppt-job-request-v1",
  "service": "new_deck",
  "inputs": {
    "files": [
      {"file_id": "f_01", "role": "primary_source", "name": "论文终稿.docx", "sha256": "…"},
      {"file_id": "f_02", "role": "chart_data", "name": "实验结果.xlsx", "sha256": "…"},
      {"file_id": "f_03", "role": "keep_as_image", "name": "图3-2.png", "sha256": "…"}
    ],
    "description": "……",
    "must_include": ["本人贡献单独一页"],
    "must_avoid": ["把伪标签一致性写成实测精度"]
  },
  "scenario": "thesis_defense",
  "audience": "答辩委员会",
  "language": "zh-CN",
  "slides": {"mode": "range", "min": 12, "max": 16, "talk_minutes": 12},
  "style": {"profile": "academic_light", "density": "medium_high", "reference_file_ids": []},
  "cover": {"title": "…", "author": "…", "advisor": "…", "institution": "…", "date": "2027-05"},
  "delivery": {
    "tier": "native_editable",
    "aspect_ratio": "16:9",
    "font_policy": "office_native_yahei",
    "charts": "native_with_workbook",
    "speaker_notes": true,
    "anticipated_questions": true
  },
  "review": {"outline_checkpoint": true, "master_checkpoint": true, "native_preview_checkpoint": true},
  "privacy": {"retention_days": 7, "allow_case_study": false}
}
```

---

## 4. 总体架构

```text
┌──────────────┐  HTTPS  ┌───────────────────────┐
│ 浏览器        │ ──────> │ API 服务（FastAPI）    │──> PostgreSQL（用户/积分/任务/审计）
│ Next.js 前端  │ <─SSE── │ 鉴权·积分·任务·文件    │──> Redis（会话/限流/事件）
└──────────────┘         │ 检查点·管理后台        │──> 对象存储（上传件/产物/预览）
                         └──────────┬────────────┘
                                    │ 任务租约（Worker 主动拉取，不需要开放入站端口）
                         ┌──────────▼────────────┐       ┌──────────────────────┐
                         │ Windows Worker 池      │──────>│ 模型网关 / Key 池     │
                         │ PowerPoint+Python+Node │       │ 每任务临时令牌、预算、 │
                         │ 固定版本的 worker 模板  │       │ 用量记录、限流与切换   │
                         │ codex exec ×N          │       └──┬────────────┬──────┘
                         │ （每账号一个 CODEX_HOME）│          ▼            ▼
                         │ （Claude 备选，已暂停）  │    OpenAI/Codex   Anthropic(备选)
                         └────────────────────────┘
```

**技术选型建议**

- **前端**：Next.js + TypeScript + Tailwind（或 shadcn/ui）。上传用预签名 URL 加分片断点续传，
  进度用 SSE 推送。
- **后端**：FastAPI。理由是仓库本身是 Python，场景和风格选项可以直接从
  `presentation_profile_choices()` 和 `STYLE_PROFILES` 导出，前后端选项不会对不上。
  数据库 PostgreSQL；队列第一版用 Postgres `SELECT … FOR UPDATE SKIP LOCKED`，
  以后再换 Redis 或 Celery；对象存储开发期用 MinIO，生产期按市场选 OSS、COS、S3 或 R2。
- **Worker**：运行在 Windows 上的 Python 常驻进程。用自动登录账户加登录启动任务的方式运行，
  **不要做成 Windows 服务**：服务跑在 Session 0，PowerPoint COM 在那里很容易失败。
- **Codex 接入（已定，已实测）**：用 `autoppt_service/codex_exec.py` 运行 `codex exec --json -`，
  提示词按 UTF-8 字节从 stdin 传入。每个账号一个 `CODEX_HOME`，由 `ProfilePool` 轮换，账号触发限额后
  冷却一段时间；用量从 `turn.completed` 事件读取并写入 `service/usage.jsonl`。要求 CLI 版本 ≥ 0.163.0
  （0.155.1 不支持当前模型），可以用 `AUTOPPT_CODEX_BIN` 指定。**不能使用 `--ephemeral`**：来源要从
  rollout 读取。出图时每页单独开一个线程，提示词必须原样传递（`prompt_match`），否则重试。检查点之后
  用 `codex exec resume` 续跑（尚未实测）。
- **Claude 接入（备选，已暂停）**：`claude-agent-sdk`（Python）的 `query()` /
  `ClaudeAgentOptions`。用到的参数：`cwd`（任务目录）、`setting_sources=["project"]`
  （加载任务目录里的 `CLAUDE.md` 和 `.claude/skills/autopptskills/`）、`max_turns`、
  `max_budget_usd`、`permission_mode`、`allowed_tools` / `disallowed_tools`、
  `can_use_tool` 或 hooks（拦截越权路径和联网）、`resume`（检查点之后续跑）、
  `model`、`effort`。从 `ResultMessage` 读 `total_cost_usd`、`model_usage`、`num_turns`、
  `subtype`（用来区分 `error_max_turns` 和 `error_max_budget_usd`）。模型默认用
  `claude-opus-5-5`（$4 / $20 每百万 token）；P1 再实测 `claude-sonnet-5-5`
  （$2 / $10）在规划阶段的质量是否够用。

---

## 5. 任务状态机与审核检查点

```text
draft → estimated → paid(积分冻结) → queued → intake → planning
  → [CP1 大纲确认] → masters → [CP2 母版审核/单页重做] → image_deck_ready
  → reconstructing → office_qa → [CP3 原生预览批准] → packaging → delivered
任一阶段 → blocked（缺输入或能力，等待用户或管理员） / failed（系统失败，自动退款）
       / cancelled（用户取消，按已完成阶段结算）
```

| 检查点 | 前端展示 | 用户操作 | 必须记录的内容 |
| --- | --- | --- | --- |
| CP1 大纲确认 | 逐页标题、要点、证据来源、精确文字和数字白名单 | 通过 / 逐页批注修改（限 N 轮，超出计费） | 批注原文、修订后的 Slide Manifest hash |
| CP2 母版审核 | 每页大图和联系表 | 整体通过 / 勾选若干页并写修改意见后重做（单页计费） | 每页图片 hash、来源记录、重做原因 |
| CP3 原生预览批准 | 原生版每页渲染、与母版的并排对比和差异热图、可编辑范围说明（哪些是原生、哪些是图片） | 接受 / 指出问题并返修 | 生成与 `docs/vectorization-history/zhicang-native-acceptance.json` 同格式的验收记录：`user_reply` 原文、`accepted_reference` 预览、`master_sha256`、`scope`、`tier` |
| CP-V 文字核对（矢量化专用） | OCR 识别出的逐页文字 | 修改和确认 | 已审核的精确文字清单（OCR 不能覆盖已审核的文字） |

实现要点：**每个阶段都开一个新的代理会话**，状态全部落在任务目录的产物文件里，并同步到
对象存储。这样检查点之后可以换一台 Worker 续跑，不依赖本机的会话缓存。`resume` 只用于
同一台机器上的短时续跑。

---

## 6. 积分计费

- **定价公式**：预估积分 = 基础费（解析加规划） + 页数 × 档位单价 + 附加项。
  单价在 P1 实测后确定：单页模型费 + Office 机时 × 机器单价 + 存储费，再乘以毛利系数。
  实测之前不要对外公布价格。
- **冻结与结算**：下单时冻结预估积分。按阶段结算，每个检查点之前结清已完成的阶段。
  超出预估的部分由平台承担，并记录下来用于调价。系统失败或 `blocked` 由平台原因造成时，
  自动退还未完成阶段的积分。
- **重做计费**：CP2 单页重做、CP3 之后的额外返修轮次，在点击前弹窗确认扣费数额。
- **账本**：采用复式记账（账户、流水、冻结单），所有变动幂等（同一业务键只记一次），
  余额不直接修改。
- **测试期**：先不接支付，用管理员充值和兑换码。
- **正式期支付**：
  - 中国大陆：微信支付、支付宝。需要企业或个体工商户主体，并有退款和开票流程。
  - 海外：Stripe 或 Paddle。
  - 支付回调验签、做幂等处理，并定时对账。

---

## 7. 开发清单

> 勾选项即任务；“验收”写的是这一阶段做完的判定标准。

### P0 决策与合规（开工前）

- [ ] 拍板 D1–D7（见第 2 节），并写进 `docs/decisions.md`（D1、D2 已定，其余待定）
- [x] 实测 `codex exec` 无人值守调用内置 `image_gen` 并产出可校验来源（ChatGPT 账号登录，2026-10-17 通过，见 [实测记录](p0-codex-imagegen-probe.md)）
- [ ] 用多个真实账号（各自一个 `CODEX_HOME`）验证轮换和限额切换
- [ ] 对外商用前：重新评估账号池条款风险，必要时改用 API key 并实测 API key 鉴权下的 `image_gen`
- [ ] 清理磁盘空间，或把 `CODEX_HOME` 和任务目录迁到大容量磁盘（C 盘剩 9.6 GB，D 盘剩 22 GB）
- [ ] 评估 Office 授权（服务器或虚拟机上做自动化的许可）和 Windows 机器的成本
- [ ] 按 D4 确定合规清单：ICP 备案、生成式 AI 服务备案或登记、内容安全审核、用户协议、隐私政策、个人信息保护、模型服务商的可用地区（需要法务确认）
- [ ] 用户协议写明：内容准确性由用户最终负责；平台不承诺逐像素一致；学术诚信相关的免责条款

**验收**：决策文档定稿；出图路线已经实测；合规负责人和时间表已确定。

### P1 仓库服务化改造（不做网页，先让单机跑通）

- [x] 新建 `autoppt_service/` 包；已完成 `codex_exec.py`（账号目录、轮换、无头执行、用量记录）
- [x] Codex 出图步骤 `codex_imagegen_step.py`：每页一个独立线程，按 rollout 校验 `exec-` 来源，强制提示词原样传递，失败重试
- [x] 服务侧 `provenance.py` 校验 `exec-` 来源记录（原仓库不改；原版门禁普通模式可接受，含测试）
- [x] 在 `autoppt_service/` 中继续补充以下模块（下文的 `service/…` 路径都指任务目录里的 `service/` 子目录；任务目录布局见 `jobfiles.py`）：
  - [x] `job_request.py`：`autoppt-job-request-v1` 的 JSON Schema 和校验（§3.3 规则：无文档时描述 ≥200 字、页数 ≤30、原生版强制 CP3 与 Office 字体、深色/极简须主动选择、Logo 须确认权属）
  - [x] `intake.py`：根据需求和文件生成主题目录：`source-materials/`、`workspace/evidence_workspace/`（source manifest、`evidence-ledger.json`、图表数据）、`workspace/presentation_handoff/planning-brief.{json,md}`；任务目录不能放在两个仓库内；上传被拒时进入 `blocked`，替换后可重跑
  - [x] 新增解析器（`parsers/`）：PDF、PPTX、DOCX（复用原仓库抽取器）、XLSX/CSV、MD/TXT/TEX、图片、ZIP（防路径穿越和压缩炸弹）；输出统一的 source manifest 和证据条目
    - [x] 扫描页 OCR（`parsers/ocr_pages.py`）：无文字层的 PDF 页用 pypdfium2 渲染（宽 1800 px）后由 Worker 的 Windows OCR 读取，成为 `extraction: ocr`、风险等级 medium 的证据条目（数字拆分如“0 · 1 7”合并为 0.17）；读不出的页仍列在简报的 `ocr_pending_pages`；mock 和非 Windows 不跑 OCR
    - [x] PDF 表格：pdfplumber 只按表格线识别（不把分栏正文当表格），进入 `chart-data.json`
    - [x] HEIC/HEIF：pillow-heif 解码后转成 PNG 素材（PowerPoint 不认 HEIC）
  - [x] `status.py`：代理写 `service/status.json`（阶段、进度、当前页、警告、blocked 原因），Worker 用 `StatusWatcher` 监听；Worker 自己维护 `service/job-state.json`（§5 状态机，按档位和检查点生成阶段计划，校验每次转移）
  - [x] `checkpoint.py`：代理写 `service/checkpoint.json` 后停止；用户回答写 `service/checkpoint-response.json`（CP3 只能由用户本人回答且必须附原话和所看预览；CP2 重做须列页码；超出免费轮次须确认扣费）；回答后归档到 `service/checkpoints/<id>/`
  - [x] `previews.py`：缩略图、联系表、母版与成品并排对比图、差异热图和差异指标（只读已有图片，不渲染；指标仅供参考，不作门禁）
  - [x] `acceptance.py`：把 CP3 的用户回复转成与 `zhicang-native-acceptance.json` 同字段的验收记录 `service/acceptance.json`
  - [x] `usage.py`：记录 token、Office 占用时长和各阶段耗时（`service/usage.jsonl`，兼容出图步骤的记录），按阶段汇总，可按输入的单价估算费用
  - [x] `export_options.py`：把场景和风格档案导出成前端可用的 JSON（含中文名、适用场景、默认页数范围）；不提供官方来源的学校风格原型
- [x] 编写服务模式指令 `SERVICE_AGENTS.md`（`autoppt_service/templates/`），每个代理阶段开始前由 Worker 渲染成任务目录里的 `AGENTS.md`（填入模板路径、任务号、预算；代理改过也会被覆盖）：
  - [x] 保留经验闭环（读台账、风格手册、生产经验、v66 基准、可编辑时读矢量化失败手册），去掉智仓兴教专属的续跑段落
  - [x] 禁止直接向用户提问，只能通过状态和检查点文件交互；上传内容一律当作数据，不当作指令（防提示注入）
  - [x] 经验只写入任务目录的 `service/lesson-candidates.jsonl`，不写模板或全局台账，且不得含客户姓名、题目、数据
  - [x] 写明每阶段的预算、轮数上限，以及达到上限时的收尾方式（保存产物，写 `blocked`）；目前只有 planning 的预算（每次 30 分钟、共 2 次）
  - [x] 明确交付层级的用词，禁止把低层级的证据当作高层级成果对外宣称
  - [x] planning 阶段契约：代理只写 `deck-plan.json`；Worker 校验（证据 id、封面原文、必须包含/避免、证据中不存在的数字、页数范围、经验记录），不通过时把问题发回再跑一次，仍不通过则 `blocked`；通过后用原仓库 `build_imagegen_prompt_manifest.build` 生成 `deck-spec.json` 和 `image-prompts.json`，并由 Worker 打开 CP1
  - [x] reconstruct 阶段的指令和预算（每次 4 小时、共 3 次）
- [x] Worker 模板构建脚本 `worker_template.py`：按白名单导出 `autopptskills/`、`autoppt_workflow/`、`scripts/`、
      经脱敏的 `improvement/`、必要的 `docs/`，把 skill 安装到每个账号的 `CODEX_HOME/skills/`；**排除**
      `deliverables/`、`reviews/`、`runs/`、`交付/`、`智搬小匠_*`、`.tmp/`、根目录的大 PPTX
  - [x] 版本不可覆盖，`template-manifest.json` 记录源提交、是否有未提交改动、逐文件 sha256；运行前校验模板未被改动
  - [x] 脱敏：台账只保留经验字段，文本文件按 `templates/redactions.json` 替换案例名和本地/运行产物路径；代码不改写，只在清单中列出含案例名的文件（当前 8 个脚本含“智仓兴教”，需要管理员决定）
  - [x] `docs/` 默认不导出，需要时用 `--include-doc` 指定；skill 安装带暂存和备份，并可逐账号核对版本
- [ ] 本地命令 `python -m autoppt_service.run_job <job-request.json> --phase planning|masters|reconstruct|package`，在一台 Windows 机器上用 `codex exec` 依次跑完各阶段（masters 阶段直接调用已完成的出图步骤），检查点之间人工写入回复
  - [x] 命令、阶段驱动、`--respond` 写入回复、`--mock` 全流程已完成；真实路线已接通 intake、masters（读任务根目录的 `image-prompts.json`）和图片版打包（调用原仓库 `build_image_deck.py`）
  - [x] 真实 planning 已接通（`--template`、`--profiles`；Codex 以 `-s workspace-write` 加 restricted-token 沙箱运行，不开 `--search`；沙箱实测见 [安全模型](security-model.md)）；2026-10-17 用真实账号跑通一份样例到 CP1（7.7 分钟，约 69 万输入 / 1.1 万输出 token，校验一次通过），见 [实测记录](p1-real-planning-run.md)；账号限额时任务延后而不是 blocked
  - [x] 真实 reconstruct 已接通（`reconstruct.py`）：代理按 SERVICE_AGENTS.md 第 7 节写 `output/native-deck.pptx`，不碰 Office；Worker 在 Office 锁下审核
        （页数、每条已审核文字必须是原生文字、整页图片不算可编辑、有图表数据时必须是带工作簿的真图表）、用 PowerPoint 渲染、把问题发回或打开 CP3；
        2026-10-17/18 用真实 Codex + PowerPoint 跑通一份 9 页样例：母版 24 轮、原生重建 4 轮约 3,778 万输入 token，
        途中补上残留文字、原生形状、渲染可见性、坐标轴 id、额度延后、恢复先复查等门禁，见 [实测记录](p1-real-run-masters-reconstruct.md)
  - [x] 4:3 画幅：原仓库提示词构建器只写 16:9，服务侧把每页提示改写为 4:3 画布（1440x1080，保留工作流门禁要求的整页约定）；图片版由服务按 10 x 7.5 in 打包（原仓库 `build_image_deck.py` 只做 16:9）；母版比例偏差超过 3% 在 CP2 报出。ImageGen 是否按 4:3 出图尚未实测
- [ ] 准备 3–5 份脱敏样例（论文答辩、国自然申请、组会报告、纯描述无文档、矢量化），逐份实跑
- [ ] 记录每份样例的耗时、费用、轮数、失败点，据此定价和设定各阶段预算
- [x] 给新模块补 pytest 用例；`--mock` 路径跑通全流程（供后端联调）：`python -m autoppt_service.run_job req.json --job-dir <dir> --job-id J1 --uploads uploads.json --phase all --mock --auto-respond`

**验收**：至少 3 份样例在无人值守下跑到 CP3，产物通过现有门禁（不通过的地方照实记录）；
得到单页成本与时长的实测表；mock 全流程可以在 CI 里跑。

### P2 后端 API

- [x] 项目骨架：FastAPI、SQLAlchemy/Alembic、配置管理、结构化日志（`autoppt_api/`；开发用 SQLite，生产用 PostgreSQL；每个请求一行 JSON 访问日志并带 `X-Request-ID`）
- [x] 数据表：`users`、`credit_accounts`、`credit_transactions`、`credit_holds`、`orders`、
      `payments`、`redeem_codes`、`jobs`、`job_files`、`job_phases`、`job_events`、
      `checkpoints`、`artifacts`、`workers`、`provider_keys`、`usage_records`、
      `pricing_rules`、`lesson_candidates`、`audit_logs`；另有 `credit_entries`（复式分录）、`auth_codes`、`refresh_tokens`、
      `stored_files`、`upload_sessions`、`idempotency_keys`、`notifications`；初始迁移 `0001`，`alembic check` 无漂移
- [x] 鉴权：邮箱验证码登录（只存哈希、10 分钟有效、限次尝试），HS256 JWT 加一次性刷新令牌（重放即吊销整组），按邮箱和 IP 限流
  - [x] 手机号验证码登录（中国大陆手机号，`users.phone`，迁移 0003）与微信网站应用扫码登录（OAuth2，state 绑定发起的浏览器，回调后用两分钟一次性票据换令牌，令牌不进 URL）；登录页一个输入框兼容邮箱和手机号。短信经 `AUTOPPT_API_SMS_PROVIDER=webhook` 发给运营方的签名中继（阿里云/腾讯云短信需要企业签名和模板审核，厂商调用放在中继里）；微信需要开放平台网站应用的 AppID/AppSecret 和回调域名，未配置时按钮不显示
  - [x] 修复：生产环境下新用户收不到登录验证码（只给已注册用户排队通知，且通知要等 5 分钟一次的维护任务）。验证码现在在请求事务结束后立即发送，且不再明文写入通知表
- [x] 文件接口：签名 URL 分片上传（按偏移续传）、按内容嗅探类型（不信任扩展名）、大小限制、可插拔病毒扫描（`AUTOPPT_API_SCAN_COMMAND`，如 clamdscan）、按 sha256 去重存储
  - 不提供“秒传”：只凭哈希不能取得文件，必须上传字节；本地存储代替对象存储，接口一致，生产换 MinIO/OSS/COS/S3
- [x] 任务接口：创建草稿 → 估价 → 提交（确认金额、冻结积分、幂等）→ 查询 → 取消（释放冻结）→ 删除；SSE 推送事件流
- [x] 检查点接口：获取待审内容（预览用签名下载链接）、提交回复（复用 Worker 侧 `validate_response`；CP3 只能由用户本人通过），重做和超出免费轮次时校验扣费确认并扣费
- [x] Worker 接口（独立凭证）：领取任务租约（带可见性超时）、心跳、上报状态和事件、
      申请产物上传地址、提交检查点、完成或失败；所有调用幂等
  - [x] Worker 客户端 `autoppt_service/worker_client.py`：领取 → 按快照恢复任务目录 → 运行阶段（后台心跳）→ 上传预览、交付物、任务目录快照 → 上报状态、用量和经验候选；按能力（intake/codex/office/package）分派阶段
- [x] 估价服务：读 `pricing_rules`，按阶段拆分，每个阶段完成时结算自己那一份；默认价格表标记为 provisional，P1 实测后由管理员更新
- [ ] 模型网关 / Key 池（内测期用本机 Codex 账号池，由 Worker 侧 `ProfilePool` 轮换和限额延后；`provider_keys` 只登记标签，不存凭证；以下各项等 D2 改用 API key 时再做）：
  - [ ] 每个任务签发临时令牌，绑定预算上限
  - [ ] Agent SDK 通过 `ANTHROPIC_BASE_URL` 指向网关，真实 key 不下发到 Worker
  - [ ] 记录用量；遇到 429 或 5xx 时退避并切换到其他 key；按 key 控制并发
- [x] 通知：任务进入检查点、交付、失败、暂停时写入通知队列，`python -m autoppt_api.maintenance notify` 通过 SMTP 发送（未配置时只记日志）
  - [x] 短信通知：只绑定手机号的用户，检查点、交付等通知以短信发送标题（正文请登录查看）

**验收**：用 mock Worker 走完整个任务生命周期，包括检查点往返、取消、失败退款；
接口有 OpenAPI 文档和集成测试。
**2026-10-17 状态**：已通过。`tests/test_api.py` 用真实 FastAPI + SQLite + mock Worker 客户端跑完原生可编辑任务（三个检查点、CP2 付费重做、
CP3 管理员不能代批）、取消、系统失败退款、可重试失败退避、租约互斥与过期、按能力分派、幂等提交、兑换码、按内容识别类型、刷新令牌重放、
限流、SSE、数据保留期清理；每个用例结束时核对账本（分录平衡、余额与流水一致）。`/openapi.json` 自动生成。

### P3 前端

技术路线调整（2026-10-17）：不用 Next.js，改为无构建的原生 HTML/CSS/ES 模块（`web/`），由同一个 FastAPI 服务托管。
理由：功能以表单和审核为主，不需要引入 npm 依赖树；没有第三方脚本和 CDN；所有服务端内容按文本插入（禁用 innerHTML），
配合严格的 CSP 响应头。只依赖公开的 API，以后可以整体换成 Next.js。

- [ ] 首页与案例页：只用脱敏素材（`docs/assets/`），讲清各交付层级的区别和限制
  - [x] 首页：交付档位、6 个使用场景、限制说明
  - [ ] 案例页（需要脱敏案例素材和授权，见 D7）
- [x] 价格页与积分包（显示当前价格表，标注测试期价格；积分包和在线支付等 P4 正式支付）
- [x] 登录和注册（邮箱验证码；首次登录即注册）
- [x] 新建任务向导：
  - [x] ① 选服务类型和交付档位
  - [x] ② 上传文件（分片上传、进度、按角色标注）并填写描述，实时显示字数和完整度提示
  - [x] ③ 场景、页数（固定/范围/按时长）、风格（色板示意，深色/极简须勾选确认）、受众、语言、信息密度
  - [x] ④ 高级选项：封面信息、必须包含/避免、画幅、图表、字体（原生版必须勾选）、审核节点（CP3 锁定）、附加项、保留期、截止时间、Logo 权属
  - [x] ⑤ 估价与确认（分阶段明细、冻结金额、可用积分、校验警告、层级说明；校验错误逐条显示）
  - [ ] 风格示例图（需要脱敏示例素材）
- [x] 任务详情：状态、已结算/冻结、阶段时间线（只显示状态变化和阶段说明，不显示代理原始输出）、取消、删除
- [x] 审核界面：
  - [x] CP1 大纲逐页批注
  - [x] CP2 母版画廊和联系表，勾选重做并写说明，弹窗确认扣费
  - [x] CP3 并排对比、差异热图、可编辑范围说明，接受或返修；接受时必须输入一句原话
- [x] 下载区：交付包、PPTX、交付说明、验收记录、证据台账（按档位不同）
- [x] 我的任务、积分流水、兑换码
  - [ ] 订单与发票（P4 正式支付后）
- [x] 管理后台：任务监控（暂停任务可恢复）、Worker 状态与添加、用户与积分调整、兑换码、定价参数、经验候选审核、账本核对与审计日志
  - [x] 任务积分退回：管理后台“任务监控”的“退积分”按钮（`POST /api/admin/jobs/{id}/refund`），不超过该任务已结算的积分，必填原因，通知用户并记审计
  - [ ] 订单（真钱）退款界面：随正式支付暂缓（用户 2026-10-18：支付先搁置）；Key 池用量等 D2 改用 API key
- [ ] 移动端适配；支持中文和英文
  - [x] 移动端单列布局（375 px 实测）
  - [ ] 英文只覆盖界面框架；场景、风格、档位说明来自中文档案，尚无英文版

**验收**：内测用户无需指导就能完成一单；三个检查点在真实 Worker 上往返正常。
**2026-10-17 状态**：在内置浏览器里用 mock Worker 实测走完一单：登录 → 兑换 300 积分 → 上传资料 → 向导（漏勾字体时显示校验原因）→
估价 244 → 下单 → CP1 通过 → CP2 重做 S03（提示扣 4 积分）→ 第 2 轮通过 → CP3 未写原话被拦下，写原话后交付 → 下载验收记录（原话原样保存，mock 层级）
→ 余额 52、冻结 0；管理后台账本核对通过。实测发现并修复：无任务时租约接口 204 带正文导致 500、上传完成后“下一步”仍禁用、
`null` 被渲染成文字、时间按 UTC 显示成本地时间、导航余额不刷新、输入框缺少无障碍名称、CSP 放在 meta 中无法限制嵌入。
尚未在真实 Worker（真实 Codex/Office）上往返，也没有内测用户试用。

### P4 积分与支付

- [x] 复式记账：充值、冻结、结算、退款、赠送、过期，每类都有对应流水（`autoppt_api/ledger.py`）
  - 每笔记账分录合计为零、按业务键幂等；用户余额不能为负；冻结不动余额，阶段完成时结算到收入户
  - 过期：赠送和兑换码积分可设有效期（`credit_lots`），先扣即将过期的积分，冻结中的积分不会过期
  - 并发：SQLite 下事务一开始就拿写锁（`BEGIN IMMEDIATE`），PostgreSQL 下对积分账户 `SELECT … FOR UPDATE`
- [x] 兑换码和管理员充值（测试期；兑换码可设积分有效期）
- [ ] 正式支付通道（按 D4），包括回调验签、幂等、对账任务、退款接口
  - [x] 与支付渠道无关的流程：积分包（价格表里配置）、订单、回调验签、按（渠道, 流水号, 状态）去重、金额不符不入账并记审计、
        一单只入账一次、未使用积分退款（按订单单价折算）、对账（过期未付订单、已付未入账、入账无支付记录）
  - [x] 带 HMAC 签名的 mock 渠道（开发和测试用，`AUTOPPT_API_PAYMENT_MOCK_SECRET`）
  - [ ] 微信支付 / 支付宝 / Stripe 适配器（需要 D4 和商户号）
- [x] 失败和取消的结算规则，写进用户协议并实现自动执行：规则草案见 [settlement-rules.md](settlement-rules.md)（需法务确认），
      代码已执行；按页计费的阶段按实际页数结算，不按冻结所用的页数上限
- [ ] 开票流程（中国大陆，需要主体和税务资料）
- [x] 每单毛利报表：`/api/admin/reports/margin`，收入（结算积分 × 每积分价值）减模型费、Office 机时和存储费；
      Codex 账号池的 token 是固定成本，按 0 计；费率在价格表 `cost_rates` 中配置

**验收**：并发下单、重复回调、失败退款、取消结算等用例全部通过，账本余额与流水一致。
**2026-10-17 状态**：已通过（`tests/test_billing.py`、`tests/test_api.py`）：两笔订单并发抢同一笔积分只有一笔成功；重复回调、伪造签名、
金额不符、未知订单都不入账；先收到金额不符、后收到正确通知时仍只入账一次；退款只退未用积分；取消和失败自动释放冻结；
300 步随机记账操作后账本仍平衡。真实支付渠道等 D4。

### P5 矢量化服务

- [ ] 按 D3 实现 `user_supplied_master` 来源类型（上传哈希、上传记录、层级命名），
      并同步更新 `SKILL.md`、门禁脚本和测试
  - [x] 服务侧实现（`autoppt_service/vectorize.py`）：用户页面冻结为母版，`assets/slides/user-supplied-masters.json` 记录来源文件、
        页码、上传哈希和母版哈希，标注“不是 ImageGen 产物”；验收记录使用单独的层级名 `user-supplied-master-native-approved`
  - [ ] `SKILL.md` 和门禁脚本：原仓库不能修改（用户要求），D3 也未拍板；在此之前工作流的 ImageGen-first 门禁不认这类母版
- [x] 输入归一化：PPTX 用 PowerPoint 渲染（调用原仓库 `render_pptx_powerpoint.ps1`，只读使用，同一时间只跑一个 Office 任务），
      PDF 栅格化（pypdfium2，已列入依赖；没有时才用 PyMuPDF，**PyMuPDF 是 AGPL 许可，商用前需评估**），图片统一为 RGB PNG，宽度 1920
  - 2026-10-17 本机实测：2 页中文 PPTX，PowerPoint 渲染 7.1 秒（1920×1080），Windows OCR 1.7 秒，文字全部识别正确
- [x] OCR 后进入 CP-V 文字核对，生成已审核的精确文字清单（Windows OCR，`windows_ocr_layout.ps1`；
      中文逐字空格已合并；用户逐行改正、删除、补充；`reviewed-text.json` 之后不再被 OCR 覆盖）
- [x] 选项：可编辑深度（文字 / 加形状 / 加图表）、照片保留为图片、页码选择（`vectorize` 字段）；页数超过订单时任务暂停并说明
- [x] 复用原生重建与 Office 验收流程，经 CP3 批准后交付（矢量化任务的已审核文字作为原生文字的核对依据；真实运行同 P1，尚未实跑）
- [x] 前端增加矢量化专用向导和文字核对界面（向导第 ③ 步的矢量化选项；CP-V 页面左侧原页、右侧可编辑文字）

**验收**：3 份外部图片版 PPT 跑到 CP3，验证结果和披露说明完整。
**2026-10-17 状态**：mock 下矢量化任务可经 API 和 Worker 跑到交付（`tests/test_vectorize.py`、`tests/test_api.py`）；
真实渲染和 OCR 已在本机实测。未达到验收：真实原生重建未实现，也没有外部样例。

### P6 Worker 集群与运维

- [x] Worker 镜像或安装脚本：Windows、Office、微软雅黑与等线字体、Python venv、Node、
      pywin32、openpyxl；运行 `check_editable_backends.py` 自检后才上线接单
  - `scripts/install_worker.ps1`：建 venv、装两个仓库的依赖、运行自检、注册“登录时启动”的计划任务（不做 Windows 服务，Session 0 下 PowerPoint COM 会失败）；
    Office、字体、Node、Codex CLI 需事先装好（脚本未在本机执行，因为它会注册计划任务）
  - `python -m autoppt_service.worker_check`：Python 与依赖、Codex CLI ≥ 0.163.0、模板未被改动、账号目录、磁盘、PowerPoint COM、两种字体、
    Windows 中文 OCR、Node，可选调用原仓库 `check_editable_backends.py`；按结果决定 Worker 能力（intake/package/codex/office）
- [x] 每台机器同一时间只跑一个 Office 任务（机器级 Office 锁，进程崩溃后可接管）。规划阶段这类不需要 Office 的工作，以后可以拆到 Linux 机器上（按能力分派已支持）
- [x] 看门狗：PowerPoint 或 Excel 卡死时结束进程、回收 COM 对象；任务超时就中止；
      `TEMP`/`TMP` 指向任务目录；监控磁盘空间（`autoppt_service/watchdog.py`：按阶段设时限，超时结束本阶段启动的 Office 进程；
      阶段结束清理残留进程；磁盘不足时不领新任务）
- [x] 任务隔离：每个任务一个独立目录，跑完上传产物后清理（交付、失败、取消后删除本机副本）；条件允许时每个任务从虚拟机快照还原（未做）
- [x] 产物同步：阶段结束时上传，中间产物 7 天后清理（快照和预览），交付物按用户选择的保留期删除
- [x] 监控与告警：队列积压、任务卡住、失败率、Worker 掉线、账号限额延后（`/api/admin/health`；`maintenance alerts` 给管理员发通知）
  - [x] 单任务费用超标告警（`job_cost`）：单任务代理输入 token 超过 6,000 万（J-plan-real-001 全流程 4,400 万）即报；价格表填了 API key 费率后，模型 + Office 费用超过估价价值 1.5 倍也报（账号池费率为 0，不会误报）
- [x] 灰度与版本：Worker 模板固定版本号，任务记录所用版本；已开始的任务只交给同版本的 Worker

**验收**：连续 48 小时压力测试中，Office 卡死能自动恢复，没有任务丢失或重复扣费。
**2026-10-18 状态**：新增 `scripts/stress_test.py`（真实 API + 多个 mock Worker + 并发用户下单、审核、重做、取消，定时核对账本和卡住的阶段）和 20 秒的回归用例 `tests/test_stress_smoke.py`。第一次跑就发现并修好一个真实缺陷：异步接口（分片上传、Worker 上传产物、支付回调）在事件循环里等待 SQLite 写锁，整台服务卡住 30 秒后报“database is locked”/HTTP 500；Worker 上传产物还在接收整个快照期间持有写锁；会话提交也改到独立线程，不再与等锁的请求抢线程池。修复后 1 分钟 6 用户 3 Worker：交付 42 单、账本每次核对都平衡、最长持锁 1.05 s。48 小时连续压测（含真实 Office 卡死恢复）仍需在 Worker 机器上跑。2026-10-17 已有的自动化用例：已有自动化用例（`tests/test_ops.py`）：Worker 在阶段中途崩溃后租约过期，另一台空机器从快照恢复
并继续，阶段只结算一次；看门狗超时会结束卡住的进程；磁盘不足时不领任务；模板版本不同时不领已开始的任务。

### P7 安全与隐私

实测与剩余风险见 [security-model.md](security-model.md)。

- [ ] **防提示注入**：上传的文档可能夹带针对代理的指令。具体措施：
  - [x] 关闭代理的联网和网页搜索能力：不开 `--search`；实测发现本机默认配置下沙箱未生效，现强制 `windows.sandbox="unelevated"`，实测可拦截联网和任务目录外写入
  - [x] Worker 出网白名单脚本 `scripts/worker_firewall.ps1`：解析允许的主机（服务 API/存储、Codex 端点、Office 激活与证书吊销），默认只打印计划；`-Apply`（管理员）把出站默认改为阻止并只放行这些地址，`-Remove` 还原；CDN 地址会变，需每小时用计划任务重跑。未在本机应用（会断开本机网络），部署时在 Worker 机器上执行
  - [ ] 拒绝访问任务目录以外的路径：写入已拦截；**读取未拦截**（`deny_read` 需要 elevated 沙箱安装）。补偿措施：泄露拦截（扫描所有代理输出中的本机凭证和凭证形态，命中即失败且不上传）、本机只保留当前任务数据、阶段结束删除 rollout
  - [ ] 机器上不放长期凭证，只有每任务的临时令牌（ChatGPT 账号登录的刷新令牌存在 Worker 上，需 D2 改用 API key 才能解决）
- [x] 多租户隔离：不同用户的任务不共享目录、会话或参考图（每阶段新会话；上报后删除本机副本；模板默认不含 v66 等案例素材，见 D7）
- [x] 经验闭环改造：任务内经验只生成“经验候选”，自动去除姓名、题目、数据等内容；
      经管理员审核后才合并进主仓库的 `improvement/`，客户原文不进全局台账
- [x] 数据生命周期：到期自动删除、用户可一键删除、删除留审计记录；对象存储加密（可选 Fernet 静态加密，生产必须配置密钥）
- [x] 管理员操作审计；查看用户文件需要单独授权并留痕（必须填写原因，每次访问写审计日志）
- [x] 依赖与密钥扫描；`.env` 不入库（`.gitignore` 已覆盖）
  - [x] 测试扫描受跟踪文件中的密钥
  - [x] 依赖漏洞扫描：CI 对服务的 `requirements.txt` 跑 `pip-audit`（2026-10-18 本机结果：无已知漏洞）

**验收**：用一份带恶意指令的测试文档跑任务，代理无法读取其他目录、访问外网或拿到 key。
**2026-10-17 状态**：未通过。联网和目录外写入已实测拦截；读取其他目录和账号凭证仍然可以，靠泄露拦截兜底（自动化测试覆盖）。

### P8 测试与内测上线

上线前需要人来完成的事项见 [launch-checklist.md](launch-checklist.md)。

- [x] CI：现有 `pytest -q` 加新模块测试，加 `validate_skill_portability.py`，加 mock 端到端测试（`.github/workflows/ci.yml`，Windows 运行；
      私有的工作流仓库用只读 deploy key 拉取：`Zero-2X/autoppt` 上的 “autoppt-web CI (read-only)”，私钥存为本仓库 secret `AUTOPPT_REPO_SSH_KEY`，2026-10-18 添加）
- [ ] 金标准样例集：P1 的样例加上真实内测件（经用户授权），每次升级 Worker 模板都回归跑一遍，
      对比质量、耗时和费用（工具 `scripts/run_samples.py` 已写好，结果按模板版本记录；样例集还没准备）
- [x] 前端端到端测试（例如 Playwright），覆盖下单、三个检查点和下载（`tests/test_e2e_browser.py`：真实后端 + mock Worker + 无头 Chromium，45 秒）
- [x] 计费专项测试（见 P4）
- [ ] 内测：10–20 名用户，免费额度，收集每单满意度和返修原因，回填定价和默认参数
- [ ] 上线清单：合规材料、用户协议、客服渠道、退款流程、监控值班（清单已列出，需负责人完成）

**验收**：内测订单的交付率、平均返修轮次、单单毛利达到预先设定的目标。
**2026-10-17 状态**：未开始内测。

---

## 8. 风险清单

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| 订阅账号池被判违规 | 账号被封、服务中断 | 内测期接受风险；对外商用前改用 API key（D2） |
| Codex CLI 或 rollout 格式随版本变化 | 出图或来源校验失效 | 固定 CLI 版本（`AUTOPPT_CODEX_BIN`）；升级前用假 CLI 测试加一页真实出图回归；校验失败一律视为 blocked |
| 代理改写提示词 | 母版和审核过的内容不一致 | `prompt_match` 强制原样传递，不一致时重试，超过重试次数则 blocked |
| 单任务成本或耗时过高 | 毛利为负、用户等待太久 | P1 实测后再定价；设阶段预算和轮数上限；提供“图片版”低价档 |
| Office 自动化不稳定 | 任务卡死 | 看门狗、单机单任务、快照还原、重试 |
| 严格门禁达不到 | 用户期望落差 | 页面上如实说明层级和差异，CP3 由用户确认 |
| 上传文档含提示注入 | 数据泄露、费用失控 | 见 P7 |
| 客户数据写进全局经验库 | 隐私事故 | 经验候选先脱敏，再经人工审核 |
| 合规（备案、支付、模型可用地区） | 无法上线或被处罚 | P0 先确认目标市场和合规路径 |
| 学术诚信争议 | 声誉风险 | 用户协议写清责任；保留证据边界；不编造数据 |

---

## 9. 粗略工期（1 名全栈开发，仅供排期参考）

| 阶段 | 估计 |
| --- | --- |
| P0 决策与合规启动 | 0.5–1 周（合规事项会并行持续更久） |
| P1 仓库服务化与单机跑通 | 2–3 周 |
| P2 后端 API | 2 周 |
| P3 前端 | 2–3 周 |
| P4 积分与支付 | 1–2 周（测试期只做兑换码约 2 天） |
| P5 矢量化服务 | 1–2 周 |
| P6 Worker 集群与运维 | 1–2 周 |
| P7 安全与隐私 | 1 周，并贯穿各阶段 |
| P8 测试与内测 | 2 周 |

**最小可内测版本**：P0 + P1 + P2 的核心部分 + P3 的向导、详情和审核页 + 兑换码，单台 Worker，
预计约 6–8 周。真正的瓶颈是 P1 的实测结果：如果单页成本或耗时超出预期，需要先优化工作流
（减少迭代轮数、把按项目写的修复脚本沉淀成通用工具），再扩大规模。
