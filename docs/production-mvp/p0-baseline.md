# P0 基线验证记录

状态：独立运行和配置模块回归通过；文本、生图凭证与代表剧本已就绪，真实模型接入、样片和对白验收待执行。P0 尚未整体通过，P1～P5 未开始。

## 来源与隔离

- 上游：https://github.com/ArcReel/ArcReel ，固定提交 `aeebacc7e04cba22184a793fd8a1fbd95b1b8301`。
- 本地基线提交：`3391617`、`78faf48`；独立 Git，无远端、无推送。
- 来源摘要见 [upstream-baseline.json](../upstream-baseline.json)。3979 个登记文件均存在，核验时仅 `frontend/vite.config.ts` 因独立端口而不同；`missing_files` 中的开发规则 `docs/api-docs/AGENTS.md` 有意不继承。
- 原项目 `../ArcReel` 只读，验证前后 `git status --short` 均为空。
- `.env` 指定本项目 `projects/` 为数据根、`agent_runtime_profile/` 为 profile。数据库 `projects/arcreel.db`、日志 `projects/logs/`、认证运行数据 `projects/runtime/` 均独立。
- 后端 `127.0.0.1:1242`，Vite `5174`，三个代理均指向 `1242`。端口与原实例、浏览器 origin 分离。
- 未继承供应商密钥、原项目数据库、会话、剧本或素材；配置功能完整继承，真实配置需在本实例设置。

## 本地验证（2026-11-02）

| 验证 | 结果 |
| --- | --- |
| Python 冻结依赖 | `uv sync --frozen` 完成，Python 3.12.13 |
| 前端冻结依赖 | Corepack 使用 pnpm 10.33.2，Node 24.14.1；锁文件未变 |
| 新库迁移 | `alembic upgrade head` 完成，版本 `b5d1e8f3a627` |
| 后端启动、worker、事件服务 | 成功；FFmpeg 7.1 可用 |
| HTTP | `/health`、认证状态、登录、认证验证、系统设置、供应商列表、MiniMax 配置、默认模型候选均 200；错误密码 401 |
| 前端生产构建 | `pnpm build` 通过，含应用与 E2E typecheck |
| 完整闸门 | `scripts/gate.py backend frontend` 通过；后端 17679 passed、12 skipped，前端 298 文件 / 3969 tests passed |
| 页面级回归 | login、settings、providers 五种视口，160 passed；本地 Chromium，未生成或更新容器截图基线 |
| 真实浏览器 | 实际登录成功；供应商页面 MiniMax 可见，无 pageerror；首次引导只在测试上下文中屏蔽 |
| 真实模型 / 实际成片 | NOT_RUN，无付费模型提交 |

后端 12 项由原套件跳过，未在本记录逐项核验原因，不计为通过。完整闸门不依赖远端 ref，直接点名域可在本地基线历史运行；如需增量，显式使用 `--changed 78faf48`，不能默认依赖不存在的 `origin/main`。

首轮启动和测试因父 shell 继承 `ANTHROPIC_AUTH_TOKEN` 被原安全检查拒绝。清理继承变量后重跑通过，未降低安全检查。供应商凭证依旧只在本地设置页填写。pnpm 11 与继承锁文件不兼容，运行命令必须使用 Corepack 锁定的 pnpm 10。

## 运行与配置

项目根目录启动后端；若由带供应商密钥的开发 shell 启动，先仅在该进程清除继承变量：

```bash
env -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL -u ANTHROPIC_MODEL -u ANTHROPIC_SMALL_FAST_MODEL -u ANTHROPIC_DEFAULT_HAIKU_MODEL -u ANTHROPIC_DEFAULT_OPUS_MODEL -u ANTHROPIC_DEFAULT_SONNET_MODEL uv run uvicorn server.app:app --host 127.0.0.1 --port 1242
```

已有构建时直接访问 `http://127.0.0.1:1242`。开发前端在 `frontend/` 运行 `corepack pnpm dev --host 127.0.0.1 --strictPort`，访问 `http://127.0.0.1:5174`。受限缓存环境可给 Corepack 设置本项目专用的可写 `COREPACK_HOME`。

配置入口：`/app/settings?section=providers`；默认模型仍在原设置页。按用户要求，未跟踪 `.env` 已设置 `AUTH_ENABLED=false`、`LISTEN_HOST=127.0.0.1`；本地无需登录。已核对认证状态返回 `enabled=false`，无令牌访问系统配置返回 200。停止当前服务使用其终端的 Ctrl+C；前后端分开启动时，两处均需停止。

## 用户接手验收

用户已选择完整保留参考项目供应商配置模块，自行上传剧本。未要求复制真实密钥或旧实例配置数据。

1. 在本地设置页填写文本、生图、MiniMax H3 的账户与模型，检查实际端点和默认模型。
2. 上传代表单集，选择 screenplay / drama / reference_video，先记录原流程操作数、时间、失败和成本。
3. 在明确本轮付费范围与预算后，用少量单元确认参考图、时长、原生音轨、对白正确性及可恢复性。
4. 记录配置组合、报价、样片和人工审片结果后评定 P0。当前不记录虚构节省比例，不进入 P1。

能力核对见 [p0-capabilities.md](p0-capabilities.md)，拟新增接口见 [p0-contract-draft.md](p0-contract-draft.md)。

## P0 验收准备补充

回归小样与已有替身测试定位见 [p0-regression-cases.md](p0-regression-cases.md)，真实接入记录模板见 [p0-acceptance-record.md](p0-acceptance-record.md)。初始空库检查时，供应商凭证、默认设置、项目与付费调用均为 0；该记录仅描述初始状态。2026-11-02 UTC 复核时，MiniMax、Agnes 各有一条凭证，项目 `proj-51ab0ffa` 已导入第 1 集原文，任务和调用记录仍为 0。项目模型、能力解析与验收缺口见验收记录；尚未发起真实模型验证。
