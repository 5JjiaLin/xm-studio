---
id: text/reference_video_script_plan
category: text
title: 参考生视频 · 单元拆分
description: 把源文拆分为参考生视频单元表，每个单元含时长、原文锚与引用语法正文。
stage: script_plan
invoked_by:
  kind: agent_tool
  name: generate_script_plan
applies_to:
  content_mode: [drama, narration]
  generation_mode: [reference_video]
  source_kind: [novel, screenplay]
output_schema: lib.script.script_models:ReferenceScriptPlanFlatDraft
slots:
  source_kind: 源文类型；只有剧情演绎会取 screenplay
  target_language: 输出语言
  project_overview: 项目概述，键齐全的对象 {synopsis, genre, theme, world_setting}；缺值传 null
  assets: 已登记资产，对象 {characters, scenes, props}，每项是 [{name, aliases, appearance}] 列表（尖括号已中和；衍生写作「本体/衍生」）
  character_names: 角色候选引用名列表（含 本体/衍生）
  scene_names: 场景候选引用名列表
  prop_names: 道具候选引用名列表
  novel_text: 逐字源文
  episode_outline: 本集大纲，键齐全的对象 {title, story_beats, hook, next_episode_teaser}；无有效内容时传 null
  next_episode_outline: 下集大纲，形状同上；无有效内容时传 null
  episode: 当前集号
  durations: 排序去重后的合法秒数档位，已格式化为文本
  duration_tiers: 「参考图↔时长」两套档位，对象 {with_references, without_references, default_with_references_only, default_without_references_only}，档位已格式化为文本，后两项表示默认秒数只落在其中一套档位内；两套相同或型号未声明时传 null
  default_duration: 默认秒数偏好；未设置时传 null
  speech_rate: 口播语速（阅读单位 / 秒），已格式化为文本
  speech_unit: 阅读单位量词（字 / 词）
  max_duration: 单次生成的时长上限（秒）
  max_reference_images: 单个视频单元的参考图上限；不限时传 null
  episode_target_duration: 单集目标秒数；未设置时传 null
  instructions: 附加指令正文；缺值传 null
protected: false
---
# 角色与任务

{{ variant("text/reference_video_script_plan/task", source_kind) }}
每个 video_unit 对应**一次视频生成调用**，正文是一段连续的画面描述，一次生成完整覆盖它。
本阶段定的是**结构与内容契约**：unit 边界、时长（时长即计费单位）、台词落位、核心资产指认——用户会逐 unit 审阅确认这份契约。
视觉编排（景别 / 构图 / 运镜扩写）由后续 prompt_authoring 以你的拆分为基底生成，本阶段不写。

**输出语言**：所有字符串值必须使用 {{ target_language }}；JSON 键名保持英文。
例外（逐字保留、不翻译）：`@[名称]` 中的资产名须逐字等于下方候选表中的登记名或 `new_assets` 里的称呼；`source_text` 须逐字复制源文。
**结构约束**：字段 / 枚举 / 必填项由 response_schema 强制；本提示只解释**如何写好每个字段的内容**。

# 上下文

{{ partial("shared/overview_block") }}

{{ partial("shared/lists/asset_registry_blocks") }}

{{ variant("text/reference_video_script_plan/source_heading", source_kind) }}

<{{ source_kind }}>
{{ novel_text }}
</{{ source_kind }}>

{% if episode_outline %}
<episode_outline>
{% if episode_outline.title %}
标题：{{ episode_outline.title }}
{% endif %}
{{ partial("shared/lists/episode_outline_lines", outline=episode_outline) }}
</episode_outline>

{% endif %}
{% if next_episode_outline %}
<next_episode_outline>
{% if next_episode_outline.title %}
标题：{{ next_episode_outline.title }}
{% endif %}
{{ partial("shared/lists/episode_outline_lines", outline=next_episode_outline) }}
</next_episode_outline>

{% endif %}
# 拆分规则

当前正在生成第 {{ episode }} 集。请覆盖全部源文情节，按叙事顺序逐 unit 产出。

- **unit 边界**：{{ variant("text/reference_video_script_plan/unit_rule", source_kind) }}
- **source_text**：该 unit 所依据的源文片段，**逐字复制**（可截断首尾，但中间不得删字、改写、翻译或概括）。
  它是追溯锚，用于把生成结果对回原文；不逐字复制会被机械校验拒绝。
- **时长决策序**（自上而下，高优先级是硬边界，低优先级在其内做优化）：
  1. 硬约束：`duration_seconds` 是 unit 时长（一次生成调用一个时长），必须取支持档位（{{ durations }}）中的值。
     叙事需要的时长放不下时，把该 unit 按叙事顺序重拆为多个 unit，**不得违约时长**。
{% if duration_tiers %}
     本型号下该档位还随「有无参考图」分两套，按该 unit **画面描述里有没有 `@` 资产引用**取用（台词记号 `@[角色]{台词}` 的说话人不计入——它不生成参考图，只驱动音色声明）：带 `@` 引用取（{{ duration_tiers.with_references }}），不带取（{{ duration_tiers.without_references }}）。服务端按该 unit 此刻**可用的参考图**定档：引用的资产还没有生成参考图时，该 unit 按不带引用的那套档位执行。两者取其一：要么改取该 unit 引用状态对应档位内的值，要么调整引用——把次要资产融入描述文字、不用 `@` 引用，从而适用不带引用的那套档位。
{% endif %}
  2. 台词下界：先估算该 unit 全部台词与画外音念完约需的秒数（口播语速约 {{ speech_rate }} {{ speech_unit }}/秒），
     取**不低于**这个秒数的档位。这是单向下界——台词永不压进念不完的短档；无台词的 unit 没有此下界。
     台词量超过最长档（{{ max_duration }} 秒）时把该 unit 拆开，不要把台词硬塞进一个 unit。
  3. 叙事切点：在合法档位与对白下界内，围绕本组的主要叙事任务组织连续动作，在动作结果、关系变化或信息揭示处收束。一个 unit 可以包含必要的多个镜头；不按固定镜头数拆组。反应、凝视和静默也占时间，不为填满最长档增加情节。
  4. 默认偏好：{% if default_duration %}unit 默认取 {{ default_duration }} 秒{% if duration_tiers and duration_tiers.default_with_references_only %}（该默认值只落在带 `@` 引用的 unit 的档位内，另一种状态的 unit 按上面的硬约束取值）{% endif %}{% if duration_tiers and duration_tiers.default_without_references_only %}（该默认值只落在不带 `@` 引用的 unit 的档位内，另一种状态的 unit 按上面的硬约束取值）{% endif %}，内容需要时可选更短或更长合法档位（叙事切点优先于默认偏好，硬约束不可覆盖）{% else %}按叙事需要从档位中取值，不强制默认值{% endif %}。
  5. 打包效率：在 1-4 之内组织正文内容，{% if episode_target_duration %}按本集目标时长打包——{{ partial("shared/episode_target_duration_rule") }}；单个 unit 在目标之内可长可短，不必贴近单次上限 {{ max_duration }} 秒，也{% else %}不要求贴近单次上限 {{ max_duration }} 秒，也{% endif %}不要默认选最短 / 保守值。
{% if max_reference_images %}
- **references 上限**：一个 unit 的**画面描述里** `@` 引用的资产名（去重后）不超过 {{ max_reference_images }} 个（台词记号 `@[角色]{台词}` 的说话人不计入——它不生成参考图）；超出时把次要角色融入背景描述（不用 `@` 引用），不要压缩主体资产。
{% endif %}

# 正文书写语法

{{ partial("shared/writing_syntax") }}

# 本阶段的正文写作指引

- 画面描述聚焦当下瞬间的**可见动作**：谁做了什么、物件互动、环境动态；动词描述物理可观察动作
  （伸手 / 转身 / 推门 / 投向），避免「陷入 / 回忆 / 意识到 / 决定」等内心动词。
- 资产名逐字取自下列候选；候选里没有的写原文称呼，并列进 `new_assets`（见下文），台词记号的说话人同样如此：
  - character: {{ partial("shared/asset_name_candidates", names=character_names) }}
  - scene: {{ partial("shared/asset_name_candidates", names=scene_names) }}
  - prop: {{ partial("shared/asset_name_candidates", names=prop_names) }}
{{ variant("text/reference_video_script_plan/dialogue_rule", source_kind) }}
- 每个 unit 的正文都要 `@` 引用它发生地的场景资产；地点切换到新 unit 时换成新地点的场景，
  地点不变的连续 unit 逐条重复引用同一个场景（候选表里没有匹配的场景时才用文字写地点）。
- 本阶段不写景别 / 构图 / 运镜（prompt_authoring 补），把叙事内容与动作过程写清楚即可。
- 每组写明当下已成立的状态、主要动作及其可承接结果。动作跨组时，下一组从前组的结果继续，不重复执行已完成动作；持物、接触关系、衣襟开合、伤痕和关键道具去向不得无因重置。原文未交代的处置不能靠自行新增事件补齐。
- 资产外观信息来自登记文本，不代表已经看过参考图片。附加指令若提供人工核对的图像事实，按其来源与对应图片理解；图片更换后旧事实需重新核对。“图中未出现”不等于剧情世界中不存在。图文初态冲突应在内容确认时解决，不能静默改台词或补剧情。

{{ partial("shared/new_assets_rule") }}
{% if instructions %}

{{ partial("shared/additional_instructions") }}
{% endif %}
