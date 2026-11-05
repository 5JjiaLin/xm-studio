# XM_Studio 实施与验证索引

先读 [团队测试说明](team-testing.md) 和 [项目目录说明](../project-structure.md)。功能范围及实施状态以根目录 [plan.md](../../plan.md)、[reuse-map.md](../../reuse-map.md) 为准。

## 说明与报告

| 材料 | 内容 |
| --- | --- |
| [team-testing.md](team-testing.md) | 本机测试身份、修改记录、ZIP 导出与离线汇总 |
| [asset-breakdown-and-feedback-comparison.md](asset-breakdown-and-feedback-comparison.md) | ArcReel 资产提取对比与 G 阶段设计依据 |
| [reference-video-framework-comparison.md](reference-video-framework-comparison.md) | 巨日禄参考案例、提示词文件与 F 阶段设计依据 |
| [e3-e5-verification.md](e3-e5-verification.md) | DOCX、完整提示词、人工剪辑及显式生成的历史验证 |
| [reference-video-f1-f4-verification.md](reference-video-f1-f4-verification.md) | 参考生视频框架与整集上下文验证 |
| [g1-g5-verification.md](g1-g5-verification.md) | 资产收敛、反馈记录与导出汇总的历史验证 |
| [character-angle-verification.md](character-angle-verification.md) | 人物角度与裁切验证 |
| `p0-*.md` | MVP 最初的基线、合同草案、回归样例与验收记录；与当前规则冲突时按当前计划执行 |

## 固定样例与验证证据

```text
fixtures/
├── p0-dialogue-fixture.txt       人工回归剧本
└── asset-rule-baselines/         B0/B1 模板快照、标注案例与说明
evidence/
├── e5/                          本地 FFmpeg 样片、字幕截图与检查结果
├── f1-f4/                       五组样例、渲染文本、机械检查与当时运行快照
└── g1-g5/                       运行检查、页面检查与截图
```

`fixtures/` 保存固定测试输入；`evidence/` 保存供报告引用的输出和历史快照。样例不是模型实际输出，机械检查不证明画面质量。保留原始证据内容，新增验证按专题归档，避免截图、JSON 和样片散落在说明文件之间。

`scripts/verify_reference_video_framework.py` 默认读取与写入 `evidence/f1-f4/` 的离线样例结果，不调用模型。需要保留既有快照时，将样例复制到临时目录后调用脚本的 `verify(output_dir)`。
