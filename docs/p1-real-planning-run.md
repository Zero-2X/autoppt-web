# P1 实测：真实 Codex 跑通 intake + planning（2026-10-17）

样例：虚构的硕士论文 docx（城市内涝预警，含 1 张结果表），`thesis_defense`、8–10 页、原生可编辑档、微软雅黑。
账号：本机默认 `CODEX_HOME`（ChatGPT Plus，积分余额 0），Codex CLI 0.163.0，模型 `gpt-6-astra`（账号配置），
沙箱 `-s workspace-write`，未开 `--search`。**更正（同日）：** 这次运行时本机配置为 `windows.sandbox = "elevated"` 且未完成安装，
后来的探针证明这种状态下沙箱没有生效（可联网、可写任务目录外）；现在所有服务调用都强制 `unelevated`，见 [安全模型](security-model.md)。Worker 模板 `real-planning-20261017b`（源提交 `482e7c7`，有未提交改动）。
任务目录 `D:/autoppt-jobs/J-plan-real-001`（仓库外，未入库）。

## 结果

| 阶段 | 耗时 | token | 结果 |
| --- | --- | --- | --- |
| intake | 2.9 s（阶段内 0.11 s） | — | 10 条证据（用户陈述 + 8 节 + 1 张表），`ready_with_warnings`（未上传图表数据） |
| planning 第 1 次 | 233.6 s | 输入 480,791（缓存 425,344），输出 2,990 | 第 13 次模型调用时触发账号用量上限（“try again at 2:21 PM”），未产出计划 |
| planning 第 2 次（重置后） | 460.8 s | 输入 688,210（缓存 644,736），输出 11,382（推理 2,220） | 9 页计划一次通过 Worker 校验（0 错误、0 警告），生成 `deck-spec.json`、`image-prompts.json`，停在 CP1 |

大纲质量（人工看过 `outline-preview.md`）：每页一个结论式标题；结果页的数字全部来自论文表格，并标明“结果均为回放仿真”；
本人贡献单页且区分了课题组已有代码；局限页如实列出 16 场测试、2 场特大暴雨、真值漏报。没有单独的总结页（档案的 `closing_summary`），可在 CP1 提出。

## 暴露的问题与处理

1. **用量上限当成了“需要用户处理”**：单账号限额后任务进入 `blocked`。已改为 `PlanningDeferred`：任务保持在 `planning`，
   写 `service/deferred.json`（含 Codex 给出的重置时间），后端据此延后重试；`run_job --resume` 可把旧的 blocked 任务拉回。
2. **失败的一轮没有 token 记录**：没有 `turn.completed` 时，现在从 rollout 的最后一个 `token_count` 读取（`usage_source: rollout.token_count`）。
3. **编码浪费上下文**：代理用 Windows PowerShell `Get-Content` 读 UTF-8 JSON，中文乱码后花了多步排查；`status.json` 的中文消息被写成 `????`。
   `SERVICE_AGENTS.md` 已要求用 Python 或 `-Encoding UTF8` 读写，并要求每个文件只读一次。
4. **重跑覆盖了上一轮的运行目录**（`codex-runs/planning-r1-a1`）：运行目录名已加唯一后缀，旧证据不再被覆盖。
5. **成本**：一次成功的规划约 69 万输入 token（94% 命中缓存）、1.1 万输出 token、7.7 分钟。主要开销是反复带着整份参考文档的上下文；
   下一步可以给规划阶段准备精简版参考（只含规划相关的规则），预计能明显降低输入 token。

## 未覆盖

- 只跑了 1 份样例、1 个账号；多账号轮换仍只在假 CLI 下验证过。
- 没有继续到 masters（需要用户在 CP1 确认，且会消耗出图额度）。
