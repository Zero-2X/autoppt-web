# 实测记录：样例集 2026-10-18（规划 3 份 + 矢量化 1 份）

账号：默认 `CODEX_HOME`；CLI 0.163.0（`AUTOPPT_CODEX_BIN`）；模型 `gpt-6-luna`（`AUTOPPT_CODEX_MODEL`；
账号已不再提供配置里的 `gpt-6.1-sol`，见 [母版与重建实测](p1-real-run-masters-reconstruct.md)）。
Worker 模板 `real-recon-20261017b`。记录追加在 `D:/autoppt-jobs/samples/sample-results.jsonl`。

## 规划（intake + planning，停在 CP1）

| 样例 | 任务 | 页数 | 墙钟 | 规划尝试 | 输入 token（缓存） | 输出 token | 结果 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 纯描述无文档 | description-only-20261018181240 | 8 | 181 s | 1 | 494,203（441,600） | 20,301 | CP1，校验一次通过 |
| 组会报告（md + xlsx） | group-meeting-20261018181541 | 7 | 212 s | 1 | 484,137（420,736） | 19,707 | CP1，校验一次通过 |
| 国自然申请（docx 节选） | nsfc-application-20261018181913 | 11 | 303 s | 1 | 523,719（436,096） | 34,246 | CP1，校验一次通过 |

和 2026-10-17 的论文答辩样例（7.7 分钟，约 69 万输入 token，校验一次通过）相比，单份规划 3–5 分钟、约 50 万输入 token。

大纲检查（Claude 目检，三份都留在 CP1 等用户本人决定，没有代为通过；通过后会启动约 26 页 ImageGen 母版）：

- 三份都守住了证据边界：组会样例的数字（0.83、0.41、56、200）来自上传材料；国自然样例把“实施条件”写成证据仍需补充；纯描述样例的效果全部定性，符合请求里“不能编造用户数量、交易量或满意度”。
- **纯描述样例缺两项**：请求要求讲清“关键功能实现”和“本人完成的部分”，大纲里没有对应页；S04 写“尚不能证明学术研究空白”，对本科毕设场景不合适。用户在 CP1 批注即可修改。

## 矢量化（3 页图片版 PPT → 原生可编辑，停在 CP3）

- CP-V 文字核对：Claude 按用户授权完成，记录为 `responder: "admin"` 并在回复里写明不是用户本人的话。对照页面改正两处 OCR 拆字：S01“城市内氵劳频发”→“城市内涝频发”，S02“雷达夕卜推”→“雷达外推”。
- 第一次重建：3 次会话、882 s、452 万输入 token（426 万缓存）、8.5 万输出 token，最后**阻塞**。渲染其实和原页一致；可见性检查误报：Windows OCR 把白字深底的标题“研究背景”读成“宀 匕 石 开 京”，又把“外”拆成“夕卜”，三次会话都在追看得见的文字。
- 修正（`a8d5c97`）：OCR 找不到的行，只有在该行母版框内的“墨量”（灰度标准差）也明显少于母版时才报告。实测可见的原生文字为母版的 0.76–1.07 倍，r3 被遮住的 S07 标题为 0.05 倍；阈值 0.5，母版框本身平坦（< 10）时不作判断。渲染 OCR 也套用已知拆字表。在真实数据上：矢量化样例通过、J-plan 最终稿通过、r3 被遮住的 S07 标题和 0.26 仍被拦下。
- 恢复后 Worker 复查 33 s 进 CP3（不调代理）：10 个原生文本框、10 个原生形状、0 张图片。等待用户本人看 `service/previews/native/` 后回复。

## 仍需用户或运维处理

- 4 个 CP：三份规划样例的 CP1、矢量化样例的 CP3（只能由用户本人回复）。
- `~/.codex/config.toml` 里的模型 `gpt-6.1-sol` 已不可用；是否改配置由用户决定（Worker 现在按次指定模型，不依赖它）。
- 开发清单里的运维项不变：CI 仓库密钥是否已配置、elevated 沙箱和 deny_read、C 盘只剩约 8 GB、法务和支付主体。

## 2026-10-18 晚：继续推进（Claude 按用户“不需要我确认”的指示代为回复 CP1，记为 `responder: "admin"`，回复里写明不是用户本人的话）

| 样例 | CP1 回复 | 之后 | 结果 |
| --- | --- | --- | --- |
| 国自然申请（规划包） | 通过 | 打包 | **已交付**（`output/nsfc-application-20261018181913.zip`） |
| 组会报告（图片版） | 通过 | 母版 | 两次会话都没有 `image_gen` 调用 → `image_gen_unavailable`，任务阻塞（没有每页各花一次会话） |
| 纯描述无文档 | 返修：补“关键功能实现”“本人完成的部分”，改 S04 | 规划第 2 轮 | 规划代理阻塞：资料只有项目级进展，没有本人分工，不能归属个人贡献。符合“不编造”的规则，需要用户补充分工 |
| 矢量化 | — | — | 仍停在 CP3（只能由用户本人看预览后回复） |

- 当前账号的所有模型都没有 `image_gen`（`gpt-6-luna`、`gpt-5.6-terra`、`gpt-5.6-luna`、`gpt-reserve` 实测；10-17 出图用的
  `gpt-6-astra` 现在对 ChatGPT 账号返回 400）。`image_generation` 功能开关是开着的，所以是服务端按账号/模型提供的问题，见
  [P0 复测](p0-codex-imagegen-probe.md)。
- 发现并修复：规划包的交付包只有 `planning-brief.json`、`slide_brief.json`、证据台账和清单，**缺了档位承诺的大纲、逐页精确文字
  （`deck-plan.json`）和出图提示词**。现在所有新建任务都打包 `deck-plan.json`、`outline-preview.md`，真实运行还打包
  `image-prompts.json`。国自然样例是修复前打的包，保留作为问题证据。

## 2026-10-19：矢量化样例作废，换成真实的生成图片页

- 用户 CP3 判定（原话，记为 `revise`）：“质量太差了，这根本就不是从生成图片然后矢量化的ppt的效果，这样是不可行的”。
  原因在样例本身：`vectorize-image-deck` 是脚本画的 3 页纯色框加一行字，不是生成图片；它通过 CP3 不能说明真实矢量化可行。任务已标记 `cancelled`。
- 新样例 `_samples/vectorize-imagegen-deck/`：J-plan-real-001 的 5 张 ImageGen 母版（封面照片、地图与时间轴、三步方法图、表格与消融、贡献地图），
  深度 `text_shapes_charts`。任务 `vectorize-imagegen-img-202610191327`（以 5 张图片上传；同内容的 PPTX 版本 `vectorize-imagegen-202610191312` 卡在 PowerPoint 上）。
- CP-V：真实页面的 OCR 明显更差：漏掉封面两行标题和“示例学生”、表格里的 18/12、图例和编号；拆字（雷卜推、管网鲒构）、全角逗号和中点、
  项目符号残留、地图噪声行。Claude 按用户授权逐页核对后回复（`admin`）：改 31 行、删 4 行、补 21 行。
- **PowerPoint 在本机起不来**（13:12 起）：COM 报 0x80080005，系统日志 DCOM 10010（服务器没有按时注册），POWERPNT.EXE 正常启动 4 秒内以代码 15 退出，
  `/safe` 安全模式能启动；Click-to-Run 显示 `ApplyInitiated=1`。没有改 Office 设置或加载项，需要用户处理。
- 新增 PowerPoint 预检：重建开始前先渲染一页测试文件，失败就延后、不启动代理。本次实测：任务延后，0 次代理会话。
- 13:34 用户手动打开 PowerPoint 并处理了提示，桌面启动恢复，但自动化仍然起不来：系统日志 DCOM 10000，启动
  `POWERPNT.EXE /AUTOMATION -Embedding` 时报 0x80070005（拒绝访问），之后是 10010 超时；从 Worker 进程直接启动（含经 explorer 启动）
  仍在 4 秒内以代码 15 退出。排除了：管理员权限不一致（两边都不是管理员）、环境变量、作业对象的 UI 限制、48 小时压测（mock，不用 Office）。
  没有改动 Office、注册表或系统设置；建议用户做 Office 快速修复或重启（重启会中断正在跑的 48 小时压测）。

## 2026-10-19 下午：Codex 接入 zpproxy；Office 自动化诊断

**接入**（用户要求，原话：“帮我把我电脑的codex连接到这个上去”）：`~/.codex/config.toml` 加 `model_provider = "zpproxy"` 和
`[model_providers.zpproxy]`（`base_url = https://webaiproxy.top/v1`、`wire_api = "responses"`、`env_key = "ZPPROXY_API_KEY"`）；
密钥存进用户环境变量 `ZPPROXY_API_KEY`，没有写进任何仓库或文档，也没有覆盖原有的 `OPENAI_API_KEY`。改前备份 `config.toml.zpproxy-bak-20261019-134935`。

- `/v1/models` 返回 13 个模型（含 `gpt-6.1-sol`、`gpt-6-astra`、`gpt-image-*`）；`gpt-6.1-sol` 文字回合正常。
- 会话里有 `image_gen.imagegen` 工具；明确要求调用后，rollout 里出现 `response_item` / `image_generation_call`（`ig_` id，PNG 内嵌在记录里，
  不写 `generated_images`）。只说“用 image_gen 画”时，模型两次直接回答 DONE、没有调用：出图步骤本来就按“恰好一次调用”校验，会重试。
- 服务的来源校验原来只认 `exec-` 宿主记录 → 现在也接受 `ig_` 的 `image_generation_call`，并在旁注里记 `record_kind` 和
  `model_provider`（`63fd379`）。真实验证：J-plan-real-001 的 S03 经代理重画，112 s，`prompt_match=exact`，`validated-host-record`。
- **与项目规则的冲突，已告知用户**：原仓库 AGENTS.md 写“不得用 image API、代理或其他提供方作为后备”。走代理时母版的来源旁注
  会写明 `model_provider: zpproxy`，不会冒充直连账号出图；代理方能看到代理会话里读到的所有客户资料。

**Office 自动化**（还没解决）：PowerPoint、Excel、Word 三个都在 `New-Object -ComObject` 时失败（0x80080005；系统日志 DCOM 10000
“拒绝访问 0x80070005”和 10010 超时）；直接启动 4 秒内以代码 15 退出；`/safe` 能启动。从 Codex（经代理）跑同一个渲染脚本，也是
UnauthorizedAccessException，所以不是 Claude 进程特有的问题。最后一次成功是 00:17，第一次失败是 13:12。已排除：Office 程序与用户
目录的权限（Codex 沙箱只加了允许项）、环境变量（含 explorer 和注册表）、管理员权限不一致、作业对象 UI 限制、Defender（期间只有 03:17 的病毒库更新，
没有拦截记录）、48 小时压测（mock）。机器从 10-17 12:48 起没有重启过，有 124 条待重启文件替换（与 Office 无关）。仍可能相关：
Click-to-Run `ApplyInitiated=1`、联想电脑管家 / Sangfor aTrust 的进程管控。下一步需要用户：重启电脑（会中断另一会话的 48 小时压测），
或 Office 快速修复。没有改动任何 Office、注册表、安全软件或系统设置。

