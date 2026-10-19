---
name: pdog-image
description: 使用 Pdog GPT Image 2.5 生成或编辑图片（文生图、参考图编辑、透明切图素材、1K/2K/4K 档位、异步任务）。当用户要求生成图片/配图/切图素材、编辑修改图片、透明背景去背、4K 超清图时使用。底层通过 pdog-image MCP 工具调用（pdog_generate / pdog_edit / pdog_task_status / pdog_verify_transparency / pdog_chroma_key），本技能只承载工具描述装不下的判断准则。
---

# Pdog 图像生成与编辑（MCP 工具判断层）

工具已由 pdog-image MCP server 提供，直接调用即可；参考文档见 resources `pdog://docs/*`。

## 工具选择

| 场景 | 工具 |
|---|---|
| 纯文生图 | `pdog_generate`（立即返回 job_id） |
| 参考图修改/编辑 | `pdog_edit`（默认只传 images + prompt） |
| 轮询生成任务 | `pdog_task_status(job_id)`（wait_seconds≤25） |
| 交付前透明验收 | `pdog_verify_transparency`（免费） |
| 色键素材重新抠图/调参重试 | `pdog_chroma_key`（免费，不重新生成） |

## 计费调用纪律（job 轮询模式）

- `pdog_generate`/`pdog_edit` **立即返回 job_id**，渲染在后台进行（约 20-120s）：之后只调 `pdog_task_status(job_id, wait_seconds=20)` 反复轮询直到 `job_status` 变为 done/failed；**绝不重复提交同一请求**（job 已计费）。
- 参数错误与 4K 守卫会同步报错（ok=false + error.kind）；一旦返回了 job_id 就说明任务已启动，只能轮询或放弃，不能重发。

## 编辑提示词语义（最重要的行为准则）

- 默认编辑只传 `images + prompt`，把保留项、删除项、修改项、禁止新增项完整写进 prompt。**绝不自行制作、推断或传入 mask**。
- 仅当用户显性说"使用蒙版""局部重绘""只修改这个区域"，或直接提供蒙版文件时才传 `mask`（透明区域=编辑区）。
- "只保留必要元素""删除所有文字""方便切图""清理背景"等整图语义属于 prompt 编辑，不因出现"只保留/删除"字样就用蒙版。
- 用户已给出简洁可执行的提示词时**逐字使用**，不要主动扩写成长清单；技术参数（透明、色键、尺寸）由工具参数承载，不混入内容判断。
- 不在一次整图编辑后自动追加第二次调用；需要重做时基于成图质量与用户反馈重新执行。

## 4K 决策（协议级守卫：不传 allow_4k=true 无法调用 4K）

- 默认 1K（`2048x1152`）；2K 尺寸走普通 Key 不加价。4K 约 ¥0.08/张，1K/2K 约 ¥0.02/张。
- 用户指定档位时照办；用户允许智能选择时按"目标渲染尺寸 ÷ 上游实际返回尺寸 + 细节密度"决定，**判断必须用实际返回尺寸**（如请求 2048 实返 1774，目标宽 3500 → 预计放大 1.97 倍 → 升 4K）。
- 单纯要求透明不构成 4K 理由。自动升级最多一次，且必须有目标 BBox 或放大 QA 失败支撑。
- **网关实况（2026-09-16 实测）**：4K 账户组当前不提供 2.5 模型族（flare/sunburst 全路由
  model_not_found），仅旧版 `gpt-image-2` 可出真 4K。**4K 的 auto 路由已默认直连 `gpt-image-2`（生成与编辑同，不再先撞 2.5 再回落）**；
  显式指定的 2.5 模型若不可用仍会自动回落并在 `model_fallback` 标注——交付时必须向用户转述实际使用的模型。
- **4K quality 降级**：网关 4K 异步校验仅接受 low/medium/high，引擎自动把 xhigh/max 降为 high 并在 `quality_note` 注明；交付时无需特别说明。

## 透明素材验收

1. 交付前必跑 `pdog_verify_transparency`：`fail`（实质轮廓 alpha≥16 碰边）→ 重新生成；`review`（软边）→ 汇报 softEdgeContact 并在棋盘格/白底/黑底人工复核；`pass` → 交付。
2. 本网关实测不出原生 Alpha：`background='transparent'` 会失败并把原图存成 `-noalpha.png`。要交付透明素材，直接走 `transparent_strategy='chroma-key'`；文生图必须显式给 `key_color`（`auto` 需要参考图取色），交付说明写明经过色键转换。
3. 修复旧色键素材时，重做请求不得再上传旧色键图作参考（模型会继承残留色），应回到干净原始资产。

## 交付规范（Zero-Tampering）

- 如实报告：保存路径、所用模型、档位（1K/2K/4K）、传输方式（sync/async 任务号）、**上游实际返回的物理像素尺寸**（可能小于请求尺寸，绝不虚标）。
- **多路由兜底**：结果里的 `route` 是实际执行的路由。若存在 `route_fallback`，说明主路由失败后切到了备用路由，交付时必须一并转述原因；若 `model_fallback` 非空，说明备用路由使用了不同的模型，也必须转述。
- 图像字节 100% 透传上游；色键转换只做"色键转 Alpha + 去色边"，严禁缩放。
- 400/401/402/404 立即报告原因不重试；502/503/504 脚本自动退避重试；等待任务时只轮询、绝不重复提交付费任务。
- 请求超时不会自动切换备用路由（可能已计费）；遇到超时先向用户确认，再决定是否重试。

## 没有 MCP 时的降级路径（CLI）

MCP 工具不可用、或目标客户端不支持 MCP 时，用同引擎的 CLI（参数一一对应，输出同样的结构化 JSON）：

```bash
# 提示词务必写入文件：内联会丢引号、被 shell 展开
printf '%s' "$PROMPT" > /tmp/pdog-prompt.txt
uv run --project <插件目录> --extra chroma pdog-image \
  --prompt-file /tmp/pdog-prompt.txt --size 2048x1152 --output out.png
```

- **必须用 `--prompt-file`，不要用 `--prompt`**：提示词里的 `"`、`` ` ``、`$`、`|`、`>` 会在 shell 层被截断或展开，且**不会报错**——生成的图内容不对但调用显示成功。
- 编辑模式加 `--image <路径>`（可多次）；透明走 `--transparent-strategy chroma-key --key-color "#00FF00"`；4K 加 `--4k`。
- 退出码 0=成功，1=失败；失败时 stderr 是 `{"ok": false, "error": {...}}`，按 `kind` 处理。
- CLI 没有 `job_` 概念，`--task-status <上游 task_id>` 用于续查异步任务。**job_id 不能跨进程使用**。
