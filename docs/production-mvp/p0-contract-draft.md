# 单集自动推进接口草案

> 已被当前 `plan.md` 的最小 MVP 范围替代：本草案中的通用运行/授权/预算模型及控制接口不再作为实施要求。新方案只补主流程必需的服务衔接，本文保留为历史设计参考。

状态：P0 设计约定，未实现、未创建迁移、未注册路由。实施在 P2，交付验证在 P4/P5。

## 复用与文件位置

| 位置 | 职责 |
| --- | --- |
| `server/services/production/service.py` | 读取原 workflow plan，单步推进、恢复和授权校验 |
| `server/services/production/contracts.py` | 运行命令、检查点请求与只读返回契约 |
| `server/services/production/policy.py` | 动作允许清单、范围/币种/额度/修订核对 |
| `server/routers/production.py` | 认证后的开始、读取、继续、停止与确认 |
| `lib/db/models/production.py`、`lib/db/repositories/production_repo.py` | 自动执行控制、授权、操作身份及执行关联 |
| 新 Alembic revision | 从当前 head 新增表、约束及原 batch 操作身份关联 |
| `frontend/src/components/production/` | 组合已有内容、资产、视频与时间线组件 |

不新增任务队列、供应商客户端、价格表或内容状态机。`server/tool_runtime.py` 的 `CallerContext` 与现有服务命令继续承担权限和准入。稳定集身份复用 `lib/episode/episode_ids.py` 的只增不复用内部 `episode` ID，不能使用数组位置。

## 最小持久化

运行保存 run_id、user_id、project、episode、控制状态（running / waiting / stopped / error / complete）、revision、当前允许动作、预算币种与上限、数量/重做上限、最近错误和时间戳。状态只描述受托执行；内容现状实时读取原 workflow plan。

每个用户/项目/集至多一个当前运行，数据库唯一约束保障并发开始返回同一运行。操作记录保存 run/action/目标/依据摘要组成的唯一 operation_key、现有 batch_id/job 关联、费用承诺与提交结论。授权记录保存确认类型、范围、内容或素材修订、配置/选项、报价摘要及用户身份；费用使用十进制字符串，不用浮点。

原批次创建事务中绑定唯一 operation_key，响应丢失后按键查回既有批次。不得先提交再单独记录关联；若恢复无法确认远端是否接受，报告人工核对，不生成替代请求。已执行任务信息继续以原 tasks、batches、账本为准，不复制到控制表。

## HTTP 草案

根路径：`/api/v1/projects/{project_name}/episodes/{episode}/production`。复用原鉴权和项目迁移检查。

| 方法与路径 | 输入与行为 |
| --- | --- |
| GET 根路径 | 当前运行、原 workflow 摘要、检查点、已有批次、费用与问题 |
| POST `/start` | expected_revision、请求身份、范围及估算额度；重复开始返回已有运行 |
| POST `/continue` | run_id、expected_revision；先查回原操作与批次，判断可继续动作 |
| POST `/stop` | run_id、expected_revision；先停止后续自动提交，报告原队列取消结果和在途任务 |
| POST `/confirm` | run_id、expected_revision、checkpoint、basis_digest、目标/选项、报价授权；内容确认必须复用原内容确认服务 |

返回 run、revision、control_status、checkpoint、batch 关联、problems、next_action、estimated/recorded 费用及币种。无报价返回 unknown，不返回零费用。内容现状来源仍是原 workflow plan；前端经既有项目事件刷新，不增加重复 SSE 连接。修订或确认依据冲突返回 409；模型配置不足、待人工和生成整批准入结果沿用原服务的结构化问题，不把部分入队结果包装成整批失败重试。

动作允许清单仅包括脚本规划、内容确认后的提示词编写、补缺失资产图、已确认目标的视频批次、首次粗剪创建/渲染和最终输出。新操作必须是显式映射的共享命令，不能直接执行 workflow 的任意字符串。

## 授权与恢复验收

- 第一检查点确认内容与资产身份后，按授权补提示词/缺失资产图；估算未知、混币或超额度时等待人工。
- 第二检查点绑定资产版本、视频目标、实际请求档位与报价。worker 按最新内容投影；超出授权范围时阻止新的供应商调用。
- 第三检查点绑定时间线 revision、选中素材与输出选项；成片必须实际渲染并通过媒体检查、人工审片。
- 同一操作的并发、刷新、响应丢失、批次落库后进程重启不能重复提交；测试注入在替身环境进行。
- 停止后不新增自动任务；成功素材和在途费用保留；继续不会自动重做失败或 stale 产物。
- 人工合法编辑不受这些自动检查点锁定；改变了授权依据时重新判断受影响范围。
