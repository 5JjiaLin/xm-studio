# XM_Studio 主流程与后续迭代复用清单

本清单以 `plan.md` 的 E1～E5 及 2026-11-05 授权的 F1～F4 为当前范围：项目/单集管理、事后成本、DOCX、资产/完整提示词、人工剪辑与参考生视频框架优化。F1～F4 复用 `prompt_authoring_scope`、正式版本指纹、原 builder/validator/renderer 和请求投影，只补整集只读上下文、模板规范及已有卡片提示；不新增镜头/连续性持久化或生成旁路，F5 暂缓。现有源码副本不删除，不恢复原平台全部入口。

来源：`../ArcReel`，固定提交 `aeebacc7e04cba22184a793fd8a1fbd95b1b8301`。已导入 XM_Studio，同路径模块可直接复用，**不需要再次复制**。摘要见 `docs/upstream-baseline.json`。原项目只读。

G1～G5 已复用现有资产决定/引用改写、完整提示词、请求投影、脚本事务、VersionManager 与 api_calls。`lib/production_feedback/` 只补本机修订证据、对象关联、可恢复提交和离线导出；设置/审阅复用制作页，汇总使用 `scripts/merge_production_feedback.py`。参考生视频专用道具规则不影响其他创作路线。验证和使用说明见 `docs/production-mvp/g1-g5-verification.md`、`team-testing.md`。

## 1. 主流程需要的后端

以下为源码根相对路径；不声称每个文件独立无依赖。

| 主流程能力 | 复用位置 | 处理 |
| --- | --- | --- |
| 导入一集剧本 | `lib/script/source_loader/`、`lib/episode/`、`lib/project/`、`server/routers/_source_uploads.py`、`server/routers/episode_management.py` | 接项目/单集管理入口和显式集选择；DOCX 复用原抽取 |
| 拆解与资产识别 | `server/routers/script_plan.py`、`server/text_generation.py`、`server/tool_runtime.py` 的 `generate_script_plan` | 生成单元/镜头及资产建议；无需运行 Agent |
| 确认内容与登记资产 | `server/services/project/script_review.py`、`server/routers/script_review.py`、`lib/script/plan_new_assets.py` | 制作页直接调用原确认服务；补本集资产增删及引用校验 |
| 生成提示词 | `server/routers/prompt_authoring.py`、`lib/prompts/prompt_builders_reference.py`、`lib/prompts/prompt_templates/templates/` | 默认展开完整提示词；文本编写完成后停在审查，由用户确认后生成媒体 |
| 人物/场景/道具出图 | `server/services/admission/asset_sheet_batch.py`、`server/routers/asset_sheets.py`、`server/services/tasks/` | 本集缺图批量生成，保留成功结果 |
| 视频能力、报价及提交 | `lib/script/reference_video/`、`lib/generation/video_request_facts.py`、`server/services/admission/video_batch_admission.py`、`server/services/admission/video_quote.py` | 复用原请求投影和整批准入，不新写 H3 调用旁路 |
| 任务与恢复 | `lib/generation/`、`server/services/tasks/` | 用原批次/任务进度及恢复；失败在对应卡片处理 |
| 素材版本与结果复用 | `lib/artifacts/` | 后台保留版本依据，MVP 不建设版本管理页面 |
| 顺序合成 MP4 | `lib/edit_timeline/`、`lib/final_cut/`、`server/services/tasks/render_tasks.py`、`server/services/presentation/` | 初始化默认剪辑并渲染；成片页复用片段编辑，补字幕编辑并保护人工修订 |
| 模型配置与解析 | `lib/config/`、`lib/catalog/`、`lib/backends/`、`lib/custom_provider/`；`server/routers/providers.py`、`custom_providers.py`、`provider_models.py`、`custom_endpoints.py`、`endpoint_tests.py`、`system_config.py` | 保留原配置能力，不新建供应商系统 |
| 已有费用估算与记录 | `lib/billing/`、`lib/db/repositories/usage_repo.py`、`server/routers/usage.py` | 制作中不展示报价；原账本补单集归属，成片页/单集列表聚合累计成本 |

## 2. 前端复用零件，重新组织交互

| 零件 | 原位置 | 用法 |
| --- | --- | --- |
| 表单与基础 UI | `frontend/src/components/ui/`、`frontend/src/components/shared/` | 使用现有样式、表单、按钮、弹层、反馈，不换 UI 框架 |
| 内容确认 | `frontend/src/components/canvas/reference/ReferenceScriptPlanPreviewPanel.tsx`、`frontend/src/hooks/useScriptReviewDraft.ts` | 抽取表格/编辑能力，不带入原整页工作流面板 |
| 资产图片 | `frontend/src/components/canvas/lorebook/AssetBrowseCard.tsx`、`AssetImageViewer.tsx`、`AssetImageDialog.tsx` | 当前步骤按人物、场景、道具分组；不显示资产库导航 |
| 视频/报价 | `frontend/src/components/canvas/reference/ReferenceVideoCard.tsx`、`ReferenceBatchPreviewDialog.tsx`、`ReferenceBatchAdmissionDialog.tsx` | 复用播放器与准入校验；移除前台报价/付费确认，不直接复活原批次弹窗 |
| 供应商配置 | `frontend/src/components/pages/ProviderSection.tsx`、`ProviderDetail.tsx`、`frontend/src/components/pages/settings/` | 组合出只包含模型接入的配置页；端点为高级项，不复用全部 SystemConfigPage 菜单 |
| 数据/状态/进度 | `frontend/src/api/`、`frontend/src/stores/`、`frontend/src/hooks/useProjectEventsSSE.ts`、`useTaskRefresh.ts` | 保留所需请求和单一事件连接；不引入 Agent 会话初始化与自动教程 |

表中省略文件目录前缀时沿用该行最近完整路径。实施应检查组件的实际 props 和依赖，过重的整页拆用其子组件，不强行嵌入。

## 3. 主动替换或停止挂载

| 位置 | MVP 调整 |
| --- | --- |
| `frontend/src/router.tsx`、`frontend/src/app-routes.ts` | 挂项目/单集管理、五步制作、模型配置及必要系统路由；未授权的旧补充地址仍不挂载 |
| `frontend/src/components/layout/StudioLayout.tsx` | 不作为制作页壳；取消多栏导航和聊天栏 |
| `frontend/src/components/canvas/episode-page/EpisodePage.tsx` | 不整页照搬；新五步视图仅复用必要的内容零件 |
| `frontend/src/components/pages/ProjectsPage.tsx`、`CreateProjectModal.tsx` | 复用项目 CRUD、集账本/回收能力；重做简洁项目/单集页，不恢复多模式向导 |
| `frontend/src/components/pages/SystemConfigPage.tsx` | 模型配置页去掉 Agent、市场、记忆、访问令牌、模板、用量等栏目；供应商相关能力保留 |
| `frontend/src/components/canvas/edit/`、`edit-render/` | 复用播放器、片段编辑、修订和渲染；补字幕编辑；暂不呈现剪映导出入口 |
| 路由根的 OnboardingTour、demo、Agent 会话加载 | 制作页不初始化；去掉相关自动请求与教程弹层 |

## 4. 暂存依赖不等于产品功能

`lib/config/`、`lib/backends/`、`lib/custom_provider/`、`lib/generation/` 存在跨目录依赖。初始可保留其所需 `lib/infra/`、`lib/db/`、`lib/i18n/`、`lib/references/`、`lib/speech/`、`lib/subtitle_style/`、两个 workspace 包、模板/字体/快照及迁移链。

`arcreel-market-core` 的端点校验器可能仍被供应商模块依赖，它存在不意味着需要“市场页面”。项目/单集页只呈现当前计划要求的管理动作，不据内部字段恢复其他创作路线。

已导入的 Agent、市场、其他模式、剪映、文档站、部署材料暂不批量删除，也不将其功能、测试或维护作为新 MVP 交付项。新入口不挂载对应产品功能，不发起无关服务调用。依赖裁剪若影响启动，只做服务装配的必要调整，不顺势建设新的插件框架。

不清空已有本地配置、项目、素材、数据库；测试、生成和修改只在独立 XM_Studio 环境进行。保留许可与 NOTICE 署名。

## 5. 已有基线与新增复用点

1. **五步制作页**：新的简单布局和按钮，把原分散页面中的必要内容放在当前步骤。组件位于 `frontend/src/components/production/`。
2. **薄的服务衔接**：在原服务之上衔接“确认生图提示词 → 资产”和“确认视频提示词 → 视频批次 → 顺序合成”，现有状态足够就复用；需要批次关联/幂等才补最少字段。现有衔接位于 `server/services/project/production.py`。

上述两项为已实现 MVP 基线。本轮新增点如下，具体阶段以 `plan.md` 为准：

| 阶段 | 优先复用 | 明确需要补的衔接 |
| --- | --- | --- |
| E1 项目/单集 | `server/routers/projects.py`、`episode_management.py`、`project_trash.py`、`lib/episode/` | 已实现管理页与显式集参数；只对恰好一集的旧链接解析该集。长期归档使用 `lib/project/production_management.py` 可选标记，复用原准入锁保护；不使用有期限的删除/回收站作为归档 |
| E2 成本 | `lib/billing/ledger.py`、`usage_summary.py`、`lib/db/repositories/usage_repo.py`、`server/routers/usage.py` | 已实现原账本 `episode_id` / `generation_target` 快照、任务认领关联文本与资产发起集；`lib/billing/production_costs.py` 只读聚合，成片/列表共用；历史未知单列，前台不展示报价 |
| E3 DOCX | `lib/script/source_loader/docx.py`、`server/routers/_source_uploads.py`、`files.py` | 已实现只读 `/production/source-preview` + 目标集 `/production/source`；复用原抽取/源文件锁，原文件内容寻址保留，正文编辑和版本确认，不再用 `file.text()` 读取 DOCX |
| E4 资产/提示词 | `BreakdownEditor.tsx`、`server/services/project/script_review.py`、`lib/script/plan_new_assets.py`、`server/routers/_asset_prompt_preview.py`、`lib/prompts/`、`lib/artifacts/generation_input.py`、`lib/script/reference_video/prompt_render.py` | 已实现未登记草稿完整预览、完整文本持久化与真实请求共源；公共资产改名复用 `EditableAssetName` 和原级联接口，采用关系移除不删公共资产 |
| E5 剪辑 | `lib/edit_timeline/model.py`、`operations.py`、`service.py`、`server/routers/edit_timelines.py`、`lib/final_cut/` | 已实现 `ProductionEditor`，原时间线增加字幕和 included 操作；字幕/样式/时间持久化并进入 FFmpeg，自动衔接保留人工音量和修订，版本失效显式修复 |

不按旧草案建立另一套运行、授权、账本、预算或渲染系统。字幕及费用归属等当前缺口按实际数据契约补最少字段/迁移。

## 6. 复用是否成功的判据

- 用户从项目/单集列表进入指定集，核心制作仍在五步页完成。
- 模型配置能力可用；没有重写供应商、队列、准入、版本和渲染系统。
- 自动生成可用剪辑初稿，人工编辑可保存、预览并导出，不要求理解内部工作流状态。
- 失败项就地修复，成功素材保留；刷新、重启、重复点击不会自动重复付费。
- 同一人拿到剧本能得到合格 MP4，才能算复用达成目标；文件复制数量不作为进度指标。
