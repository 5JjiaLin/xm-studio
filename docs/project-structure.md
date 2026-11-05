# XM_Studio 目录与开发入口

代码按现有核心库、服务端和前端分层；制作页按用户流程归组。目录名称表达实现职责，页面范围以根目录 `AGENTS.md`、`plan.md` 为准。

## 根目录

| 路径 | 用途 |
| --- | --- |
| `AGENTS.md`、`CODING_STANDARDS.md` | 当前项目规则与按领域加载的代码规范 |
| `plan.md`、`reuse-map.md` | 迭代范围、验收状态和原能力复用位置 |
| `frontend/` | React 页面、三语文案、Vitest 与 Playwright 测试 |
| `server/` | FastAPI 路由、服务编排、任务准入与执行适配 |
| `lib/` | 项目、脚本、提示词、媒体版本、生成、剪辑、计费与反馈核心逻辑 |
| `alembic/` | 数据库迁移链，已有迁移保持原顺序与身份 |
| `packages/` | 供应商配置等仍依赖的两个 workspace 子包 |
| `tests/` | 后端单元、集成与外部服务测试，目录镜像所属模块 |
| `scripts/` | 审计、验证、维护与离线反馈汇总命令 |
| `docs/production-mvp/` | XM_Studio 实施说明、验证报告、固定样例与证据 |
| `docs/adr/`、`docs/standards/` | 架构决定与领域规范 |
| `projects/` | 本机运行数据根，含项目、数据库、媒体与反馈；由 `.gitignore` 排除 |
| `.env`、`.venv/`、`frontend/node_modules/`、`frontend/dist/` | 本机配置、依赖与构建产物，不作为源码迁移 |
| `agent_runtime_profile/`、`skills/`、`public/`、`website/`、`deploy/` | 上游运行依赖或参考材料；目录存在不代表产品提供相关入口 |
| `LICENSE`、`NOTICE`、`LICENSES/` | 上游许可与署名 |

## 制作页组件

`frontend/src/components/production/` 根目录只保留五步页面壳、页面数据加载、路由和跨步骤资产引用工具：

```text
production/
├── ProductionPage.tsx
├── useProductionProject.ts
├── production-paths.ts
├── asset-references.ts / asset-references.test.ts
├── management/  项目、单集、作品选择与累计成本
├── script/      剧本输入与 DOCX 导入
├── breakdown/   拆解确认、资产采用和拆解任务状态
├── assets/      资产卡、完整生图提示词、人物角度及裁切
├── videos/      视频卡、提示词审阅与视频任务进度
├── output/      成片片段与字幕编辑
└── feedback/    本机测试身份、可用性审阅与记录导出
```

文件按实际职责放入子目录，单元测试与实现相邻。路由直接引用 `management/ProjectsPage.tsx`，不保留旧路径转导出。页面级回归位于 `frontend/e2e/regions/production.spec.ts`；接口录制夹具位于 `frontend/e2e/fixtures/recorded/`，仅由录制脚本生成。

## 后端与提示词

| 需求 | 首要位置 |
| --- | --- |
| 制作页 API | `server/routers/production.py` |
| 单集任务衔接 | `server/services/project/production.py` |
| 项目与资产采用 | `lib/project/`、`lib/script/plan_new_assets.py`、`server/services/project/script_review.py` |
| 拆解与视觉扩写 | `lib/script/script_generator.py`、`lib/prompts/prompt_builders_reference.py` |
| 拆解模板 | `lib/prompts/prompt_templates/templates/text/reference_video_script_plan.md` |
| 视觉扩写模板 | `lib/prompts/prompt_templates/templates/text/reference_video_prompt_authoring.md` |
| 视频最终提示词 | `lib/prompts/prompt_templates/templates/reference_video/unit.md`、`lib/script/reference_video/prompt_render.py` |
| 生成、恢复、版本有效性 | `lib/generation/`、`lib/artifacts/`、`server/services/currency/` |
| 剪辑与最终渲染 | `lib/edit_timeline/`、`lib/final_cut/` |
| 本机修改记录 | `lib/production_feedback/`、`server/services/production_feedback.py` |
| 离线汇总入口 | `scripts/merge_production_feedback.py` |

`lib/` 与 `server/` 的边界沿用 [ADR 0084](adr/0084-keep-lib-and-server-roots-group-by-glossary.md)。已有依赖不能仅因页面暂不展示就删除；运行数据目录不参加源码整理。

## 验证入口

在项目根使用 `.venv/bin/python -m pytest` 运行指定后端测试，避免搬迁前的脚本 shebang 指向旧目录。前端使用 `frontend/package.json` 锁定的 pnpm 版本，执行 `typecheck`、`lint` 和与变更相关的页面测试。

报告、样例和生成证据的区别见 [实施与验证索引](production-mvp/README.md)。历史报告中的 PID、任务数和验证结果只代表报告日期的快照；运行状态应重新查询。
