# AutoPPT Web

AutoPPT 的网页服务：用户购买积分，提交资料和需求，后台用 Codex 运行 AutoPPT 工作流生成 PPT 并做矢量化。

本仓库**只放服务代码**。工作流和 skills 在独立的 `autoppt` 仓库里，这里只读使用、从不修改。
通过 `AUTOPPT_REPO` 指定它的位置（默认是同级目录 `../autoppt`）。

- 开发清单：[docs/development-checklist.md](docs/development-checklist.md)
- P0 实测（Codex 无人值守出图）：[docs/p0-codex-imagegen-probe.md](docs/p0-codex-imagegen-probe.md)

## 现状

| 模块 | 说明 |
| --- | --- |
| `autoppt_service/codex_exec.py` | 无头运行 `codex exec`，每个账号一个 `CODEX_HOME`，轮换池，用量记录 |
| `autoppt_service/codex_imagegen_step.py` | 母版生成：每页一个线程，调用内置 `image_gen` |
| `autoppt_service/provenance.py` | 从 rollout 校验 `exec-` 出图记录并写来源 sidecar |
| `autoppt_service/repo_paths.py` | 定位只读的 `autoppt` 仓库 |
| `autoppt_service/export_options.py` | 场景、风格和表单选项导出为前端 JSON |
| `autoppt_service/job_request.py` | `autoppt-job-request-v1` 的 JSON Schema 与表单规则校验 |
| `autoppt_service/parsers/` | 上传文件解析（PDF/PPTX/DOCX/XLSX/CSV/MD/TXT/TEX/图片/ZIP），输出 source manifest |
| `autoppt_service/intake.py` | 需求 + 上传 → 任务目录（证据台账、规划简报、任务状态） |
| `autoppt_service/status.py` | 任务状态机（`service/job-state.json`）与代理进度文件（`service/status.json`） |
| `autoppt_service/checkpoint.py` | 检查点与用户回答文件，CP3 必须由用户本人批准 |
| `autoppt_service/acceptance.py` | CP3 批准 → 验收记录（与原仓库验收记录同字段） |
| `autoppt_service/previews.py` | 缩略图、联系表、母版/原生并排图、差异热图 |
| `autoppt_service/usage.py` | token、Office 时长、阶段耗时记录与汇总 |
| `autoppt_service/run_job.py` | 按阶段运行任务；`--mock` 可跑通全流程 |
| `autoppt_service/planning.py` | 真实规划阶段：Codex 写 `deck-plan.json`，Worker 校验并生成出图提示词 |
| `autoppt_service/worker_template.py` | 从 `autoppt` 仓库按白名单构建 Worker 模板（脱敏、版本化），安装 skill 到各账号 |
| `autoppt_service/templates/` | `SERVICE_AGENTS.md`（任务目录的 `AGENTS.md` 模板）和脱敏词表 |
| `autoppt_service/worker_client.py` | Worker 进程：向后端领取阶段、运行、上传产物和快照、上报 |
| `autoppt_api/` | 后端 API（FastAPI）：登录、积分账本、上传、任务、检查点、Worker 接口、管理后台接口 |
| `web/` | 前端（无构建的 HTML/CSS/ES 模块，由后端托管在 `/`）：下单向导、审核、下载、积分、管理后台 |

## 运行

需要 Python 3.11+、`autoppt` 仓库的依赖，以及 Codex CLI ≥ 0.163.0。

```powershell
python -m pip install -r requirements.txt -r ../autoppt/requirements.txt
python -m pytest -q
$env:AUTOPPT_CODEX_BIN = "<path to codex.exe 0.163.0+>"
python -m autoppt_service.codex_imagegen_step <job>/image-prompts.json --profiles profiles.json
```

启动后端（开发模式会在接口返回登录验证码）和一个 mock Worker：

```powershell
$env:AUTOPPT_API_DEV_ECHO_CODES = "1"; $env:AUTOPPT_API_ADMIN_EMAILS = "you@example.com"
alembic upgrade head
uvicorn autoppt_api.app:create_app --factory --port 8000
python -m autoppt_service.worker_client --api http://127.0.0.1:8000 --token <管理员创建 Worker 时返回的 token> --jobs-root D:/autoppt-jobs/worker1 --mock
python -m autoppt_api.maintenance all
```

网页在 `http://127.0.0.1:8000/`，接口文档在 `http://127.0.0.1:8000/docs`。

构建 Worker 模板、把 skill 装进每个账号，然后用真实 Codex 跑接收和规划（规划结束后停在大纲检查点 CP1）：

```powershell
python -m autoppt_service.worker_template build --out D:/autoppt-worker/templates
python -m autoppt_service.worker_template install --template D:/autoppt-worker/templates/<version> --profiles profiles.json
python -m autoppt_service.run_job req.json --job-dir D:/jobs/J001 --job-id J001 --uploads uploads.json --phase intake
python -m autoppt_service.run_job --job-dir D:/jobs/J001 --job-id J001 --phase planning --template D:/autoppt-worker/templates/<version> --profiles profiles.json
```

构建前先在 `autoppt_service/templates/redactions.json` 里补全需要脱敏的姓名和项目名。

不接 Codex 和 Office 跑通全流程（结构验证用，产物全部标为 mock）：

```powershell
python -m autoppt_service.run_job req.json --job-dir D:\jobs\J001 --job-id J001 --uploads uploads.json --phase all --mock --auto-respond
```

任务目录要放在两个仓库之外，避免 Codex 加载仓库根目录的 `AGENTS.md`。不要使用 `codex exec --ephemeral`：
出图来源要从 rollout 中读取。
