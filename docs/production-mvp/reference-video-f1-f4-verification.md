# 参考生视频 F1～F4 交付记录

日期：2026-11-05。F1～F4 已实现并完成自动检查、离线文本审阅及本机正式预览核对；F5 时间码暂缓。没有发起文本、图片或视频模型生成。

## 实现

- F1：编写输入分为整集有序只读上下文和本次目标。保留原始组号、ID、时长、待编写状态、人工创作段；输出仍只含目标的 `text`。继续复用正式版本指纹、范围校验和冲突隔离，不新增持久化镜头表。
- F2：拆组优先考虑叙事切点；正文按开场、必要镜头、可见收尾组织，区分景别、构图、运镜和动作。共享规则禁止重新设计静态外观，允许剧情必要的动态状态。保留对白、说话人、时长档位和引用校验，不强制镜头数量或绝对时间码。
- F3：资产描述可以记录人工核对的图片事实；模板明确本次文本编写没有直接看图。视频卡说明默认/人工来源，正文未保存修改时提示核对旧人工段，保存和刷新保留人工内容，只有显式恢复默认才清空。资产、风格或素材指纹变化会刷新现有只读预览；图片/声音继续从正文派生。
- F4：自动回归、五组离线样例、实际服务/worker 模板核对和真实项目预览完成。旧素材和任务记录保留，未批量重写存量正文。

## 自动检查

| 检查 | 结果 |
| --- | --- |
| 后端 prompts、reference_video、script generator、参考视频路由、附件解析与时长服务 | 918 passed |
| 前端 `pnpm check`：TypeScript、ESLint、Knip、Vitest | 300 文件、3970 测试通过 |
| 最后 UI 调整后 TypeScript、ESLint | 通过 |
| 制作页 Playwright，5 种视口 | 200 passed |
| 本轮 Python 文件 Ruff check / format | 通过 |
| 离线五组 builder / validator / renderer | 通过 |

后端认证用例要求认证开启；本机产品 `.env` 为免登录。测试进程单独设置 `AUTH_ENABLED=true` 后通过，未修改产品配置或削弱测试。首次页面运行受沙箱监听限制，随后发现锁定 Playwright 期望的浏览器版本未安装；使用配置已有的 `PLAYWRIGHT_EXECUTABLE_PATH` 指向本地 Chromium headless shell 1248 完成测试，无下载、无测试豁免。页面回放不等于真实模型验证。Vite 原有 native loader / chunk-size 警告和 jsdom 媒体日志不影响通过结果。

关键新增回归：只写第 3 组、非连续第 2+4 组时五组上下文都在；非目标及人工段不变；邻组人工段并发修改时拒绝覆盖并保留隔离产出；正文修改、保存、刷新保留人工段，显式恢复默认才清空；未提交媒体任务。

后端复现（项目根）：

```bash
AUTH_ENABLED=true uv run --no-sync python -m pytest tests/unit/lib/prompts tests/unit/lib/script/reference_video tests/integration/lib/script/test_script_generator_reference_branch.py tests/integration/lib/script/reference_video tests/integration/server/routers/test_reference_videos_router.py tests/integration/server/services/tasks/test_resolve_reference_assets.py tests/integration/server/services/tasks/test_reference_video_duration_resolution.py -q
uv run --no-sync python -m scripts.verify_reference_video_framework
```

前端在 `frontend/` 使用锁定的 pnpm 10.33.2：`pnpm check`、`pnpm e2e e2e/regions/production.spec.ts`。本机浏览器路径按已安装版本设置，CI 使用项目原有官方镜像配置。

## 离线五组审阅

输入与人工目标：[sample.json](evidence/f1-f4/sample.json)。这是依据已分析的巨日禄动作结构编写的缩减回归样例，**不是原文档全文或实际模型输出**。同一输入、同一资产清单，固定合成能力档位 `[8,16]`、图片上限 4；不声称竞品的 12/9/10/11/7 秒就是本项目模型档位。第 3 组采用 16 秒容纳英文对白与反应，总计 48 秒；其余每组 8 秒。

| 组 | 开场与变化 | 可见收尾及下一组接力 | 图片引用 |
| --- | --- | --- | --- |
| 1 | 建立殿内左右位置；衣襟已开，命令后铁链反应 | 尚未身体接触，保持面对面 | 殿、吉、埃 |
| 2 | 逼近、后退抵柱；银针落柱脚，右手捏下巴 | 右手钳制建立，背贴柱面 | 殿、吉、埃、针 |
| 3 | 延续同一右手接触；原对白及反应 | 不重新抓取，仍被捏住 | 殿、吉、埃 |
| 4 | 沿原接触收紧，靠近耳侧，怒转恐惧 | 右手仍钳制，背贴原柱 | 殿、吉、埃 |
| 5 | 先松右手、再退开，埃沿柱瘫坐 | 双方分开，目光落回原柱脚银针 | 殿、吉、埃、针 |

审阅结论：接触建立→保持→加强→解除有明确承接；固定同一只手、同一柱面与相对位置，没有突然换手或重新抓取。第 2 组银针落地后，第 3、4 组不为记账补引用，第 5 组才再出镜。对白逐字及顺序经真实校验器检查；镜头内只写可见结果，不提前演下一组。该结论是文本审阅，尚未证明模型会生成相同动作。

图文冲突处理：此样例输入采用人工确认的“衣襟已经敞开”，没有继续写解扣；这一步是人工修正输入，**不是模型自动看图或解决矛盾**。“埃图未展示木箱”不推导为世界无木箱，摘录中不含木箱事件；若原内容契约有上/下箱动作，须在内容/资产审查中明确去向，不能由视觉扩写暗补。换图后仍需人工复核旧事实。

对照产物：

- [旧目标区投影](evidence/f1-f4/old-target-projection.md)：按已核实旧机制重建；只选目标、重新编号，缺少整集和人工段。不是历史实发请求。
- [新编写输入](evidence/f1-f4/new-authoring-input.md)：当前真实 builder 输出，第 2、4 组保留原位置，第 3 组仍作为只读上下文。
- [最终渲染文本](evidence/f1-f4/rendered-targets.md)：人工目标经真实 renderer 渲染的默认/人工预览，含图片和声音声明、Style/Avoid。
- [机械检查](evidence/f1-f4/checks.json)：合法档位、对白保留、引用顺序、人工覆盖不改声明、纯说话人不加图、无声和裸旁白后期路径。音频就绪为固定离线输入，无音频生成。

## 本机正式预览与模板生效

核对前没有 XM_Studio Uvicorn/worker 运行进程；SQLite 活动任务为 0，lease 为空，`quick_check=ok`。在独立 XM_Studio 数据/profile 上启动 1242，无 reload。使用新进程加载全部模板，并在服务 lifespan 内调用真实编写 builder，输出摘要与离线输入一致；同进程 worker 持有有效 lease。未运行或修改 `../ArcReel`。

[启动证据](evidence/f1-f4/runtime-start.json) 记录 PID、worker owner、6 个模板 SHA256 和编写渲染摘要；[实际预览检查](evidence/f1-f4/runtime-preview-checks.json) 记录正式 API 的核对结果。这些是本次启动时的快照，未来重启后 PID/lease 需要重查。

真实项目 `proj-583d457a` 第 1 集 `E1U8`：默认和人工创作段预览均成功，图片编号与实际附件顺序一致，所有附件文件存在，人工覆盖不改变声明、图片、音频或时长参数。有效时长 15 秒；本单元实际没有参考音频附件，不能把离线 native 档位样例当成该项目的实际音频输入。项目页与预览返回 200。预览前后 API 调用记录均为 93、任务总数均为 94、活动任务均为 0；正式 scripts 哈希不变。

## 验证边界

代码、自动检查、离线文本和只读正式预览已完成。**真实文本模型结果未验证，真实视频画面连续性未验证**；本次未产生新模型调用，不以模板措辞或替身测试宣称成片质量已经提升。旧人工创作段继续覆盖默认创作文本，用户需要复核或显式恢复默认才能采用新正文。F5 时间码仍为后续可选项。
