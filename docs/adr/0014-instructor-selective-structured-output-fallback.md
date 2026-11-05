---
status: accepted
---

# 结构化输出：选择性降级而非统一入口，降级手段按 wire 通道可用性分化

原生支持 `structured_output` 的模型走原生 wire 级结构化参数（`response_format` / `response_json_schema`）更快更准。降级只在两种情形触发：**事前能力门控**——registry 未声明支持的模型（含未注册模型，保守降级，宁可降级也不调会报错的原生 API）；**事后运行时复验**——原生调用 HTTP 200 后校验返回是否真满足 schema，第三方中转/代理可能静默忽略 wire 级参数返回散文或违例 JSON（复验用 `strict=False` 容忍可强转值，避免对供应商已接受的合法响应误降级多计费；触发降级时原生调用的 token 并入降级结果，不漏记）。决定不让所有 backend 统一走降级路径——原生路径再套一层只会更慢、更不准；PydanticAI / BAML 等备选过重或 DSL 不兼容。

降级手段按后端分化，判据是**降级是否必须绕开 wire 通道**：openai / ark 走 Instructor（`lib/backends/text_backends/instructor_support.py` 纯函数：prompt 注入 schema + 解析 + 校验重试，对上层透明）；gemini 走后端内的 prompt 注入（schema 写进 prompt 重发纯文本调用，剥 markdown 栅栏后校验，失败带错误反馈重试）——它的降级触发场景正是中转静默忽略 wire 级参数，而 Instructor 的 genai 集成（JSON/TOOLS 模式）同样落在 response_schema / function calling 这些 wire 级参数上，会被同一中转忽略；把 schema 约束写进 prompt 是唯一不依赖 wire 参数的手段。

重试与降级的组合结构（重试装饰器只包单次网络调用、降级自带重试）见 `docs/adr/0047`。

## Consequences

- 多一个第三方依赖，且能力判断要查 registry（按模型 capabilities 门控，与 `docs/adr/0013`「能力声明在模型级」配套）。
- 无原生结构化输出的模型、以及原生参数被中转静默忽略的场景，都能产出结构化结果，且不破坏原生路径。
- 降级不再唯一等于 Instructor：gemini 的降级校验/重试逻辑收在其后端内（`_prompt_json_fallback`），不进 `instructor_support`。

## MiniMax OpenAI 兼容通道

本地失败日志确认 MiniMax-M3.1-Flash 的原生 schema 拒收整数 enum；TOOLS 接受请求但把 aliases 数组反复生成为 `{"item":"..."}`，校验失败。MiniMax 的结构化请求直接复用 `prompt_json_fallback`，schema 写入提示词，不发送 `response_format` 或 tools。保留 Pydantic 校验、最多两次带反馈尝试、累计用量、结构化截断硬错误；不修改业务数组类型或跳过台词保真检查。普通文本及其他供应商保持原调用路径。此为接入修复，真实模型验收仍待用户页面重试。
