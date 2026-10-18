# 安全模型与实测结果（P7）

编写日期 2026-10-17。本文记录服务实际做到了什么、实测结果，以及还没解决的风险。改动安全相关代码时同步更新。

## 1. Codex 沙箱实测（Windows，Codex CLI 0.163.0）

探针：在一个任务目录里让代理依次 ① 访问 `https://example.com` ② 读取任务目录外的金丝雀文件 ③ 往任务目录外写文件 ④ 往任务目录内写文件 ⑤ 读取 `~/.codex/auth.json`。

| 运行方式 | 联网 | 读目录外 | 写目录外 | 写目录内 | 读 auth.json |
| --- | --- | --- | --- | --- | --- |
| `codex exec -s workspace-write`，本机配置 `windows.sandbox = "elevated"`（elevated 沙箱从未完成安装） | **可以** | 可以 | **可以** | 可以 | 未测（同样可以） |
| `codex sandbox -P :workspace` | 拦截 | 可以 | 拦截 | 可以 | 可以 |
| `codex exec -s workspace-write -c windows.sandbox="unelevated"` | 拦截 | 可以 | 拦截 | 可以 | 可以 |

结论：

1. **本机默认配置下沙箱没有生效。** 2026-10-17 的真实规划运行（[p1-real-planning-run.md](p1-real-planning-run.md)）就是在这种状态下跑的；当时文档里“只能写任务目录、不能联网”的说法不成立，现已更正。
2. 现在所有服务调用都强制 `-c windows.sandbox="unelevated"`（`codex_exec.windows_sandbox_mode()`，可用 `AUTOPPT_CODEX_WINDOWS_SANDBOX` 覆盖）。实测可拦截联网和目录外写入。
3. **读取不受限制（2026-10-18 复测，结论不变，补充了能否修的实测）。** 用真实 `codex exec -s workspace-write` 会话
   （`windows.sandbox="unelevated"`）读三处金丝雀文件：用户目录、`~/.codex`（与 `auth.json` 同目录）、另一个任务目录，
   **全部读得到**。试过的修法都不成立：
   - 本机已不是管理员会话；这个版本的 Codex 0.163.0 不再提供 elevated 沙箱安装命令（`elevated_windows_sandbox` 标为 removed，
     `codex sandbox` 只剩 `uninstall`）；二进制里写明 “deny-read overrides require the elevated Windows sandbox backend”。
   - 新的 MXC 后端（`windows.sandbox="mxc"`）在本机报 “native MXC is unavailable on this executor”。
   - 给 Codex 沙箱能力 SID（`~/.codex/cap_sid` 里的通用 workspace/readonly SID，以及该任务目录专属的 SID）加“拒绝读取”ACE：
     代理照样读得到（受限令牌的访问检查不靠这些 SID）。
   - 桌面版 Codex 自带的 `0.163.0-alpha.10.2` 同样不行：`windows.sandbox="elevated"` 下三处金丝雀仍全部读得到（elevated 沙箱没有安装，它附带的 `codex-windows-sandbox-setup.exe` 只接受 Codex 内部的编码请求，且需要管理员）。同一版本下 `image_gen` 也仍不可用。
   - 可行的替代：**模型网关模式**（开发清单 P2，已实现）下 Worker 不再有 ChatGPT 登录凭证，任务令牌只在该阶段的环境变量里且
     Codex 不传给代理命令；加上专用 Worker 账号（读不到其他用户的文件）、一机一任务、阶段后删除本机副本，凭证与其他客户数据就不在
     代理可读的范围内。内测期仍用账号池时，这一项是已知残留风险。
3a. 原记录： Codex 的 `permissions.filesystem.deny_read` 只在 elevated 沙箱下生效（“unelevated restricted-token sandbox cannot enforce deny-read restrictions”）。elevated 沙箱需要管理员一次性安装，本机未安装，未实测。

## 2. 由此采取的措施

| 风险 | 措施 | 位置 |
| --- | --- | --- |
| 上传文档里的提示注入让代理联网或改写其他目录 | 强制 restricted-token 沙箱；不开 `--search`；AGENTS.md 规定上传内容一律当数据 | `codex_exec.py`、`templates/SERVICE_AGENTS.md` |
| 代理读取账号凭证（`auth.json` 及其备份）并写进交付物 | 泄露拦截：上传任何产物前，扫描代理可写的所有文件（含 PPTX 内部 XML），比对本机账号凭证的真实字符串（只在内存中，不记录）和通用凭证形态；命中即判失败、不上传任何东西、删除本机副本 | `leakguard.py`、`worker_client.py` |
| 代理读取其他客户的数据 | 数据最少化：每次上报后删除本机任务副本（后端有快照），同一台机器上最多只有一个客户的数据；阶段结束后删除该任务在 `CODEX_HOME` 中的 rollout 和生成图片；原始事件日志（含代理读到的内容）不进快照 | `worker_client.py` |
| 经验候选里带出客户内容 | 只保留经验字段，去掉封面文字、文件名、数字、路径和凭证形态；管理员审核后才进主仓库 | `leakguard.sanitize_lesson`、`/api/admin/lessons` |
| 管理员越权查看客户资料 | 查看他人任务必须填写原因（`X-Access-Reason`），每次访问写审计日志，即使后续操作被拒也保留；管理员不能代用户批准 CP3 | `app.py` |
| 存储泄露 | 可选静态加密（Fernet，`AUTOPPT_API_STORAGE_KEY`）；病毒扫描在加密前进行；按保留期和一键删除清理 | `storage.py`、`lifecycle.py` |
| 只凭哈希取得他人文件 | 不提供“秒传”，必须上传字节 | `app.py` |
| 仓库里提交了密钥 | 测试扫描所有受跟踪文件中的 JWT、API key、私钥 | `tests/test_security.py` |

## 3. 尚未解决（上线前必须处理）

1. **读取隔离**：完成 elevated 沙箱安装后，用 `deny_read` 拒绝读取 `CODEX_HOME`、其他任务目录和用户目录，并重跑第 1 节探针确认。在此之前，账号凭证对代理可读，只能靠泄露拦截兜底；泄露拦截挡不住经过改写（例如分段、编码）后的凭证。
2. **专用机器和账号**：Worker 机器只跑 Worker，不放个人文件；Worker 用专用的 Windows 账号自动登录。
3. **出网白名单**：`scripts/worker_firewall.ps1` 把整台 Worker 的出站默认改为阻止，只放行服务 API/存储、Codex 端点和 Office 激活/证书吊销的地址（防火墙只认 IP，主机名在运行时解析，需每小时重跑刷新）。脚本已写好并测试了计划输出和拒绝路径；尚未在任何 Worker 上应用，应用前 Worker 进程本身仍可联网，只靠沙箱拦截代理联网。
4. **凭证形态**：ChatGPT 账号登录的长期刷新令牌就存在 Worker 上，与“机器上不放长期凭证”的目标冲突。改用 API key + 每任务临时令牌（D2）后才能真正解决。
5. **带恶意指令的文档实跑**：P7 验收要求用一份带恶意指令的测试文档跑完整任务；目前只做了沙箱探针和泄露拦截的自动化测试。
6. **依赖扫描**：CI 每次推送对服务的 `requirements.txt` 跑 `pip-audit`（未锁定版本，按最新可装版本检查）；工作流仓库的依赖不在本仓库扫描范围内。
