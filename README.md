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

## 运行

需要 Python 3.11+、`autoppt` 仓库的依赖，以及 Codex CLI ≥ 0.163.0。

```powershell
python -m pip install -r requirements.txt -r ../autoppt/requirements.txt
python -m pytest -q
$env:AUTOPPT_CODEX_BIN = "<path to codex.exe 0.163.0+>"
python -m autoppt_service.codex_imagegen_step <job>/image-prompts.json --profiles profiles.json
```

任务目录要放在两个仓库之外，避免 Codex 加载仓库根目录的 `AGENTS.md`。不要使用 `codex exec --ephemeral`：
出图来源要从 rollout 中读取。
