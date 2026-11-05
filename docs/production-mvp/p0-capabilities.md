# P0 能力矩阵

状态：源码核对与替身回归已完成，真实账户能力全部待验收。目录声明与端点声明不能代替实际接入结果。

| 能力 | 继承依据 | 当前结论 |
| --- | --- | --- |
| 供应商配置 | `SystemConfigPage.tsx`、`ProviderSection.tsx`、`server/routers/providers.py`、`system_config.py`、`custom_endpoints.py` | 完整保留；实际接口与浏览器访问通过，含自定义、模型、端点等入口；未复制凭证 |
| 文本规划 | `server/tool_runtime.py::generate_script_plan`、`server/text_generation.py` | 路径可用且相关替身回归通过；项目已选 `minimax/MiniMax-M3.1-Flash`，真实结构化输出待测 |
| 内容与资产 | `confirm_script_review`、`lib/script/plan_new_assets.py` | 内容确认物化正式脚本并登记资产；继续编写只补待编写条目（ADR 0080） |
| 资产图 | `server/services/admission/asset_sheet_batch.py` | 复用整批服务；项目已选 `agnes/agnes-image-2.5-flash`，人物/场景/道具图实际效果待确认 |
| H3 协议 | `lib/custom_provider/builtin_endpoints/minimax-h3.json` | 提交 `/v2/video_generation`，按 task_id 轮询；源码声明可恢复，真实端点权限待测 |
| H3 图像引用 | 同上 | 声明首尾帧和最多 9 张参考图；实际限制与参考生视频桶待验收 |
| H3 时长与分辨率 | `lib/catalog/snapshot/catalog.json` | 随版快照声明 4～15 秒、768p/2k；运行时有效配置优先，不硬编码固定时长 |
| H3 原生声音 | H3 `audio_track=always_on`、参考音频 direct | 声明最多 3 段、合计 15 秒参考音频，提示词最多 7000 字符；真实音轨、对白、口型和多角色稳定性未验证 |
| 价格与并发 | 目录快照、配置解析、原 worker | 快照为 CNY 0.5/秒（768p）、0.8/秒（2k），仅目录参考；实际报价以本实例解析为准，实际账户价格/并发未知 |
| 旁白/TTS | `server/routers/generate.py` 的显式及批量 TTS、`lib/speech/` | 通用单元解析覆盖 reference_video、drama 等；支持 narrator-owned 单元，但目标配置、声音合成与最终混音未验收 |
| 字幕 | `lib/speech/speech_presentation.py`、ADR 0089 | 机械草稿，按单位实测时长与阅读单位比例排布，不能声称逐句对齐 |
| 视频整批准入 | `video_batch_admission.py`、ADR 0061 | 任何目标受阻时整批零视频任务；执行结果逐条报告，不用前端循环旁路 |
| 恢复 | `restart_recovery.py`、`video_resume.py`、执行 checkpoint | 已有 job 才接续；远端接受状态未知时不能自动重复付费提交 |
| 时间线与渲染 | `lib/edit_timeline/`、`lib/final_cut/`、`lib/jianying_draft/` | 保留原引擎；时间线跟随选中 current 素材，素材变更需复核裁切；自动粗剪与人工交付检查尚未实现 |

## 提示词与重试

实际规划入口为 `lib/prompts/prompt_builders_reference.py::build_reference_units_split_prompt`，渲染 `text/reference_video_script_plan` 及 `partials/text/reference_video_script_plan/` 下的 screenplay 变体。视觉层与请求沿用 `build_reference_video_prompt`、`templates/reference_video/unit.md`，资产图沿用 `templates/asset/sheet.md`。后续规则修改必须落在这些加载路径。

文本单次网络调用的瞬态重试有界：共享默认 3 次，OpenAI/Anthropic 等显式设置 4 次；prompt JSON 降级最多 2 次，Instructor 默认 `max_retries=2`。这些是各层上限，不代表一次规划只调用两次；实际调用费用需计入账本和后续自动运行授权。沿用 ADR 0047，禁止把整个多次调用编排包进重试后重放已计费的原生请求。

## 已确认缺口

- MiniMax、Agnes 凭证已配置，配置接口均返回 `ready`；项目已选文本与生图模型，视频能力解析为 `minimax/MiniMax-H3`。这些结果只证明配置和解析可用，尚无真实模型请求或样片证明。
- 已有视频执行期会按最新配置重投影能力、时长与引用，但尚无自动运行预算授权约束。P2 必须在提交与供应商调用前复核授权的费用范围，不能只靠预览报价。
- `create_generation_batch` 每次生成新 batch_id，任务在途去重不等于“批次已创建但运行未记录”的幂等。P2 需在原批次事务绑定操作身份并查询恢复。
- 首轮验收按用户上传剧本执行，优先含人物对白且不依赖额外画外音。若出现旁白，展示 TTS/外部后期配音范围，实际样片未通过前不能称为交付完成。
