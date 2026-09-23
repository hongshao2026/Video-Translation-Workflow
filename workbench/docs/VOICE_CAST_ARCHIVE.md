# 中文译配角色与音色存档

## 1. 文档用途

本文集中记录本工作区各视频项目已经确认并实际用于中文 TTS 的“人物／角色 → 音色”映射，同时标明尚未进入选音阶段的项目和已被后续版本替换的历史音色。

- 存档日期：`2026-09-01`（Asia/Shanghai）
- 当前盘点范围：工作区根目录现存的全部 `*_run` 项目，共 5 个。
- “当前有效”判定顺序：用户最新明确决定 → 当前项目 `PROJECT.md` → 正式生产音色锁定／全文 TTS manifest → 状态为 `pass` 的 TTS 生成 QA。
- 候选试听音色不等于正式音色；只有已锁定并被正式 TTS 清单或实际 TTS manifest 采用的音色才记入“当前有效映射”。
- 本文只保存角色、音色、版本和证据路径，不记录 API Key、Cookie、Authorization Header、临时媒体直链或其他凭证。

## 2. 项目总览

| 视频 ID | 视频／主题 | 配音状态 | 当前有效角色数 |
|---|---|---|---:|
| `7nR7573HjvE` | `Podcast ep17: Juan Pardo / Instincts, Study Ethic & Exploits` | 全文 TTS 已生成，QA `pass` | 3 |
| `jp4eDjr27Ow` | `Podcast ep11: Stephen Chidwick / Finding Balance in Everything` | 新音色全文 TTS 与 v4 成片已生成，机器 QA `pass` | 2 |
| `USHR-lJ25Qo` | `The Return to High-Stakes Poker \| Fedor Holz` | 全文 TTS 171/171 批完成，QA `pass` | 3 |
| `wqdL4AbY8s0` | `Podcast ep1: Fedor Holz / GG Poker Security Ambassador` | 尚在翻译审核阶段，未选音、未配音 | 0 |
| `1FpvdrDnI-Q` | `Beneath the Cards: Garrett Adelstein` | 尚在 Agent T 翻译阶段，未选音、未配音 | 0 |

## 3. 当前有效角色与音色

### 3.1 `7nR7573HjvE`：Juan Pardo 访谈

| 人物／角色 | 中文职责 | 当前音色名称 | MiniMax voice ID | 正式 TTS 片段数 |
|---|---|---|---|---:|
| Jonathan Jaffe | 主持人／采访者 | 电台男主播 | `Chinese (Mandarin)_Radio_Host` | 428 |
| Juan Pardo | 嘉宾；口译后的 Juan 回答仍归入 Juan | 沉稳高管 | `Chinese (Mandarin)_Reliable_Executive` | 784 |
| Ellie | 现场口译 | 温暖闺蜜 | `Chinese (Mandarin)_Warm_Bestie` | 3 |

范围说明：

- 插播赛事解说 A/B 共 13 个父槽按用户决定排除，不翻译、不生成中文 TTS。
- 正式全文录音共 1,215 个片段；TTS 生成 QA 状态为 `pass`。

权威证据：

- 项目记录：`7nR7573HjvE_run/PROJECT.md`
- 当前音色交接：`7nR7573HjvE_run/work/voice_selection_handoff_v6.json`
- 正式全文 TTS 清单：`7nR7573HjvE_run/work/full_tts_segment_manifest_v1.json`
- TTS 生成 QA：`7nR7573HjvE_run/qa/full_tts_generation_v1.json`

### 3.2 `jp4eDjr27Ow`：Stephen Chidwick 访谈

| 人物／角色 | 中文职责 | 当前音色名称 | MiniMax voice ID | 当前全文 TTS 片段数 |
|---|---|---|---|---:|
| Jonathan Jaffe | 主持人／采访者 | 电台男主播 | `Chinese (Mandarin)_Radio_Host` | 949 |
| Stephen Chidwick | 嘉宾 | 播报男声 | `Chinese (Mandarin)_Male_Announcer` | 985 |

范围说明：

- 赛事解说稳定 ID 2–4 保留原声和现有中文字幕，不生成中文 TTS。
- 当前新音色全文清单共 1,934 个录音片段；TTS 生成 QA 状态为 `pass`。
- 当前有效成片是使用上述映射的 `jp4eDjr27Ow_run/deliverables/jp4eDjr27Ow_中文译配成片_v4_新音色.mp4`。

音色变更历史：

- Stephen Chidwick 最初使用“抒情男声” `Chinese (Mandarin)_Lyrical_Voice`，曾用于旧版全文 TTS 和 v1–v3 成片／音量调整版本。
- 用户后来将 Stephen Chidwick 改为“播报男声” `Chinese (Mandarin)_Male_Announcer`。
- Jonathan Jaffe 最终继续使用“电台男主播”；曾经把两人都改成“播报男声”的理解只产生未执行 dry-run，MiniMax 调用数为 0，随后已被用户明确纠正。
- 因此旧“抒情男声”只保留为历史记录，不再是当前有效映射。

权威证据：

- 项目记录：`jp4eDjr27Ow_run/PROJECT.md`
- 当前音色交接：`jp4eDjr27Ow_run/work/voice_selection_handoff_v11.json`
- 当前全文 TTS 清单：`jp4eDjr27Ow_run/work/full_tts_segment_manifest_v2.json`
- 当前 TTS 生成 QA：`jp4eDjr27Ow_run/qa/full_tts_generation_v2.json`
- 当前成片机器 QA：`jp4eDjr27Ow_run/qa/final_machine_qa_v4.json`

### 3.3 `USHR-lJ25Qo`：Fedor Holz 访谈

| 人物／角色 | 中文职责 | 当前音色名称 | MiniMax voice ID | 正式 TTS 子项数 |
|---|---|---|---|---:|
| René Kuhlman | 主持人 | 电台男主播 | `Chinese (Mandarin)_Radio_Host` | 324 |
| Adam Carmichael | 主持人 | 播报男声 | `Chinese (Mandarin)_Male_Announcer` | 178 |
| Fedor Holz | 嘉宾 | 精英青年音色-beta | `male-qn-jingying-jingpin` | 848 |

范围说明：

- 正式角色图共 1,344 个稳定字幕槽，拆分为 1,350 个连续 TTS 子项。
- 实际 TTS manifest 共 171 批，171/171 为 `ready`，覆盖 1,350/1,350 个 TTS 子项；失败、待处理和不确定批次均为 0。

权威证据：

- 项目记录：`USHR-lJ25Qo_run/PROJECT.md`
- 正式角色图：`USHR-lJ25Qo_run/work/role_map_production_v1.json`
- 正式音色锁定：`USHR-lJ25Qo_run/work/voice_selection_production_v1.json`
- 流水线音色映射：`USHR-lJ25Qo_run/work/voice_map_production_v1.json`
- 实际 TTS manifest：`USHR-lJ25Qo_run/full_dub_v1/tts/manifest.json`
- 正式 TTS 完成 QA：`USHR-lJ25Qo_run/qa/formal_tts_completion_v1.json`

## 4. 尚未建立音色映射的项目

### 4.1 `wqdL4AbY8s0`

- 当前仍在翻译审核阶段。
- 本项目的 `wqdL4AbY8s0_run/qa/translation_gate.json` 尚未生成和放行。
- 工作区内没有本项目的 `voice_selection`、生产 `voice_map` 或正式 TTS manifest。
- 当前不得把任何候选声线记作已选音色。

证据：`wqdL4AbY8s0_run/PROJECT.md`

### 4.2 `1FpvdrDnI-Q`

- 当前仍在 Agent T 全文翻译阶段。
- TTS 执行器在项目记录中仍为“待记录”。
- 工作区内没有本项目的 `voice_selection`、生产 `voice_map` 或正式 TTS manifest。
- 当前角色与音色数量均记为 0。

证据：`1FpvdrDnI-Q_run/PROJECT.md`

## 5. 仅存在流程摘要的早期案例

通用流程文档提到早期完整测试案例 `jLV1OL1Y8uA`，并保存了人物数量、TTS 批次和成片指标摘要；但当前工作区根目录不存在 `jLV1OL1Y8uA_run/`，现存摘要也没有给出可核验的具体 MiniMax voice ID。因此本存档不推测其人物音色，只记录为“历史案例存在，具体音色映射不可由当前文件可靠复核”。

参考：

- `dub_workbench/docs/LOCAL_DUBBING_WORKFLOW.md`
- `dub_workbench/docs/workflow.definition.json`

## 6. 后续维护规则

1. 新项目完成选音后，必须同时记录人物、角色、可读音色名、精确 voice ID、锁定版本和证据路径。
2. 只有试听但未锁定的声音写入项目候选记录，不进入本文“当前有效映射”。
3. 更换音色时不得覆盖旧记录；在本节增加“旧音色 → 新音色”、用户决定和实际生效成片版本。
4. 正式 TTS 完成后，以实际 manifest 和状态为 `pass` 的生成 QA 反向核对锁定映射。
5. 赛事解说、广告口播或其他明确排除角色要单独记为“不配音”，不得误算为缺失音色。
6. 本文是跨项目检索索引；单项目最新明确决定仍以该项目 `<video_id>_run/PROJECT.md`、正式生产锁定文件和用户最新指令为最高优先级。
