# P0 实测：Codex 无人值守调用内置 image_gen

> 日期：2026-10-16 至 10-17 · 运行机器：用户本机（Windows，Codex 以 ChatGPT 账号登录）
> 结论：**可行**。`codex exec` 能在无人值守模式下调用内置 `image_gen`，rollout 里留下可校验的来源记录，
> 已接入仓库 handoff 和 ImageGen-first 强校验。

## 决策背景

2026-10-16 用户决定：规划、出图、重建、验收全部由 Codex 执行，账号使用本机的 Codex 账号池，出图使用
Codex 内置 `image_gen`；Claude Agent SDK 路线暂停。账号池对外商用的条款风险见
[开发清单](development-checklist.md) 第 0 节第 1 条，本次只做内测验证。

## 实测记录

| # | 内容 | 结果 |
| --- | --- | --- |
| 1 | 全局 CLI `codex-cli 0.155.1` 执行 `codex exec` | **失败**：不认识配置里的 `gpt-6.1-sol`，返回 400 “not supported when using Codex with a ChatGPT account”。桌面版自带更新的 runtime，装在受保护的 WindowsApps 目录，无法直接调用 |
| 2 | 用 `npx @openai/codex@0.163.0` | **失败**：缺少可选依赖 `@openai/codex-win32-x64` |
| 3 | 把 0.163.0 安装到独立目录 `D:/autoppt-jobs/_tools/codex-0.163.0`，全局安装保持不变 | 可用，`codex login status` 显示 ChatGPT 登录 |
| 4 | 单图探针：`codex exec --json -s workspace-write`，推理强度 low | **成功**，49 秒；输入约 6.1 万 token（缓存 2.7 万），输出 308 token |
| 5 | 正式出图步骤 `autoppt_service.codex_imagegen_step`，2 页中文提示词，每页一个独立线程 | **成功**，每页约 48 秒，均一次成功；`prompt_match=exact`；每页输入约 3.6 万 token；`verify` 结果 pass |
| 6 | 用仓库 `run_imagegen_ppt.py` 组装图片版 PPTX，并跑 ImageGen-first 门禁 | 组装成功。该封装脚本写死了 `--require-strong-ig-id`，只认 `ig_`，所以原版门禁在强校验模式下不接受 `exec-` 记录（本次曾临时修改原仓库让它 2/2 通过，按用户要求已全部撤回）。原版门禁在普通模式下接受服务写出的 sidecar（有测试覆盖）。“整页约定”检查失败，原因是探针提示词没写约定语句，属于夹具问题，提示词没有事后改写 |

生成图片尺寸为 1672×941。两页中文标题、正文、方框和箭头都与提示词一致，没有多余元素。

## 关键发现

1. **来源记录的格式**：当前的 Codex 主机（桌面版和 `codex exec` 都是如此）通过 code mode 调用
   `tools.image_gen__imagegen`。rollout 中写入一条 `event_msg/item_completed` 事件，其 `item.type`
   为 `Extension`、`kind` 为 `image_gen.generation`，包含 `exec-…` id、`status`、`revisedPrompt`、
   **base64 图片原文** `result`，以及 `savedPath`（`<CODEX_HOME>/generated_images/<thread>/<exec-id>.png`）。
   仓库原来的 ingest 只认旧版的 `image_generation_call`/`ig_` 记录。
2. **`--json` 事件流里看不到出图事件**，代理自己报告的 id 也填错了（把 thread id 当成了生成 id）。
   所以来源必须从 rollout 读取，不能相信代理的回答。
3. **代理会改写提示词**：探针中代理把提示词转述成了另一句英文。`revisedPrompt` 记录的是代理实际传给工具的
   文本，可以用它校验提示词是否原样传递。
4. **Windows 换行转换**：`subprocess` 的文本模式会把 `\n` 改成 `\r\n`，导致传给 Codex 的提示词和存档不是
   逐字节一致。执行器现已改为按 UTF-8 字节发送。
5. **不能使用 `--ephemeral`**：这个选项不写 rollout，会让来源无法校验。
6. **磁盘空间**：D 盘剩 22 GB（已用 93%），C 盘剩 9.6 GB（已用 96%）。生成的图片存放在
   `CODEX_HOME` 所在的 C 盘，批量运行前需要清理或把 `CODEX_HOME` 迁到大容量磁盘。

## 代码位置（独立仓库，原仓库不改动）

2026-10-17 用户要求：网页服务放在独立仓库 `D:/autoppt-web`，原来的工作流和 skills（`D:/autoppt`）一律不改，
只作为只读依赖（`AUTOPPT_REPO`，默认是同级目录 `autoppt`）。因此：

- `autoppt_service/provenance.py`：服务自己的 `exec-` 来源 ingest 和校验。
  - 交叉校验：thread id 必须出现在 rollout 文件名中；`savedPath` 的文件名和目录必须与 id、thread 对应；
    保存文件的 sha256 必须等于 rollout 中 base64 解码后的 sha256。
  - 提示词必须原样传递（`prompt_match`）。
  - sidecar 使用工作流原有的字段，并附加 `host_record`、`provenance_strength`、`prompt_match`、`model: unconfirmed`。
  - `verify_masters` 再次核对图片哈希、sidecar 和 host 记录三者一致。
- handoff 队列仍由原仓库未修改的 `prepare_handoff` 生成。原仓库的 `verify_handoff` 和 `validate_builtin_imagegen_outputs`
  只认 `ig_`，服务流程不调用它们。后续组装图片版 PPTX 时，用 `build_image_deck.py` 加普通模式的
  `verify_imagegen_first_gate.py`，不用写死强校验的 `run_imagegen_ppt.py`。
- `autoppt_service/codex_exec.py`：账号目录（`CODEX_HOME`）、轮换池、限额后冷却、无头执行、事件解析、用量记录。
- `autoppt_service/codex_imagegen_step.py`：每页一个独立线程；要求线程内恰好一次出图调用；按 record_index 精确 ingest；
  提示词被改写时重试；输出步骤报告和 `service/usage.jsonl`。
- 测试 15 项（出图步骤用假 CLI，不消耗额度），包括“原版门禁在普通模式下接受服务写出的 sidecar”。

## 运行方法

```powershell
cd D:/autoppt-web
$env:AUTOPPT_REPO = "D:/autoppt"
$env:AUTOPPT_CODEX_BIN = "D:/autoppt-jobs/_tools/codex-0.163.0/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
python -m autoppt_service.codex_imagegen_step <job>/image-prompts.json --profiles profiles.json --scratch-root D:/autoppt-jobs/_scratch/<job>
```

`profiles.json` 的格式是 `[{"name": "acct1", "codex_home": "D:/codex-homes/acct1"}, ...]`。每个目录由你自己执行
`$env:CODEX_HOME="D:/codex-homes/acct1"; codex login` 登录，程序不接触任何凭证。账号目录里还需要放一份 `config.toml`
（模型等设置），否则会使用 CLI 的默认值。任务目录和临时目录建议放在仓库外面，避免 Codex 加载仓库根目录的 `AGENTS.md`。

## 未验证与后续

- 多账号轮换只在假 CLI 测试中验证过；真实环境只用了一个账号。
- 参考图（`reference_images` 通过 `-i` 传入）是否会被 `image_gen` 用作风格参考，还没有实测。
- 规划、重建、验收阶段改由 `codex exec` 执行的部分还没开始（P1 后续）。
- 已安装的 skill 副本（`~/.codex/skills/autopptskills`）与原仓库不一致（缺 6 个文件、4 个文件不同，与本次无关），
  是否同步由用户决定。
- 本次经验没有写进原仓库的 `improvement/` 台账（用户要求原仓库不改），记录在本文件中。
- 实测产物在被忽略的 `D:/autoppt-jobs/_probes/`；图片版 PPTX 的 sha256 为
  `75d10157e0d6e3464fe517dfe372963c93f78374fc97ba9bd23ae0d7133135f8`。
