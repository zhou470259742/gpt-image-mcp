# Pdog GPT Image 2.5 API 规范

完整的请求与脚本参数规范。默认使用 GPT Image 2.5（`flare` / `sunburst`），保留 `gpt-image-2` 兼容通道；支持 1K 默认生成、2K 普通档与 4K 显性生成分流，以及异步任务接口。

## 模型选择

| `--model` | 实际模型串 | 定位 | 备注 |
|---|---|---|---|
| `auto`（默认） | 按场景自动选择 | 智能路由 | 生成→`flare`；编辑→`sunburst`；4K→强制 `flare` |
| `flare` | `gpt-image-2.5-flare` | 快速档 | 日常生成、批量、草图 |
| `sunburst` | `gpt-image-2.5-sunburst` | 精准档 | 编辑、参考图一致性；**4K 账户组不支持** |
| `image-2` | `gpt-image-2` | 旧版兼容 | 复现历史产物 |

## 核心参数

| 参数 | CLI 选项 | 类型 | 必需 | 默认值 | 说明 |
|---|---|---|---|---|---|
| `prompt` | `--prompt` | string | 是 | — | 提示词，描述画面主体、构图、光影、风格或修改指令 |
| `model` | `--model` | `auto` / `flare` / `sunburst` / `image-2` | 否 | `auto` | 模型选择；见上表 |
| `resolution` | `--resolution` | `1k` / `2k` / `4k` | 否 | `1k` | 分辨率档位。1K/2K 用普通 Key，4K 用 4K Key |
| `4k` | `--4k` | flag | 否 | `false` | 显性开启 4K 模式：选用 4K Key、默认尺寸 3840x2160、强制 `flare`、自动走异步 |
| `image` | `--image` | path / URL / data URL | 否 | `null` | 参考图，触发以图生图 / Edit 模式。支持多张；脚本解析后以 multipart 文件上传 |
| `mask` | `--mask` | path / URL / data URL | 否 | `null` | 显性蒙版图（仅 Edit 模式）。默认不使用；只有用户明确要求蒙版、局部重绘或指定区域时才传入。透明区域表示编辑修改区，且必须与 `--image` 同时使用 |
| `size` | `--size` | string | 否 | 1K 默认 `2048x1152` / 4K 默认 `3840x2160` | 见 `size-resolution-fallback.md` 的完整尺寸矩阵（1K / 2K / 4K 三档） |
| `quality` | `--quality` | string | 否 | 配置值（`xhigh`） | `low` / `medium` / `high` / `xhigh`（默认）/ `max` / `auto`。官方文档仅列前三档，网关实测后三档可用 |
| `n` | `--n` | int | 否 | `1` | 生成张数（1-10）。官方文档称 `n` 只能为 1，但网关实测 `n=2` 正常返回 2 张；如需稳定，优先并发多次 `n=1` |
| `background` | `--background` | string | 否 | `null` | 背景模式，如 `transparent` |
| `transparent_strategy` | `--transparent-strategy` | `auto` / `native` / `chroma-key` | 否 | `auto` | `auto`/`native` 要求上游真实 Alpha；`chroma-key` 仅为显式接受的回退 |
| `async` | `--async` | `auto` / `always` / `never` | 否 | `auto` | `auto`：4K 自动走异步，其余同步；`always`：强制异步；`never`：强制同步 |
| `poll_interval` | `--poll-interval` | float | 否 | 配置值（`5`） | 异步轮询间隔（秒） |
| `task_timeout` | `--task-timeout` | float | 否 | 配置值（`1800`） | 异步任务最长等待（秒）；服务端单任务上限 30 分钟 |
| `key_color` | `--key-color` | `auto` / `#RRGGBB` | 否 | `auto` | 动态选择或显式指定色键颜色 |
| `chroma_tolerance` | `--chroma-tolerance` | float | 否 | `30` | LAB 色差内的边缘连通色键区域完全透明 |
| `chroma_feather` | `--chroma-feather` | float | 否 | `40` | 抗锯齿边缘羽化与去色边范围 |
| `keep_keyed` | `--keep-keyed` | flag | 否 | `false` | 同时保留未经 Alpha 转换的上游色键图 |
| `output` | `--output` | path | 否 | `./output/pdog-image-<ts>.png` | 输出文件路径 |
| `config` | `--config` | path | 否 | `../config.json` | 配置文件路径 |
| `dry-run` | `--dry-run` | flag | 否 | `false` | 仅打印请求 Payload、模型路由与传输方式，不实际消耗 API |

## 配置文件结构

```json
{
  "base_url": "https://ai.whyshy.cn",
  "model": "gpt-image-2.5-flare",
  "models": {
    "flare": "gpt-image-2.5-flare",
    "sunburst": "gpt-image-2.5-sunburst",
    "image-2": "gpt-image-2"
  },
  "model_default_alias": "flare",
  "model_auto_routing": {
    "generation": "flare",
    "edit": "sunburst",
    "edit_4k": "flare"
  },
  "async": {
    "mode": "auto",
    "poll_interval_seconds": 5,
    "task_timeout_seconds": 1800
  },
  "default_resolution": "1k",
  "default_quality": "xhigh",
  "api_keys": { "1k": "sk-...", "4k": "sk-..." },
  "api_key": "sk-..."
}
```

## 交付与分流原则

- **分流原则**：默认 1K 模式及普通鉴权 Key；只有用户显性要求 4K 时才调用 4K 模式及 4K 鉴权 Key。2K 尺寸（如 `2048x1152`）使用普通 Key。
- **禁止伪 4K**：不允许将最大边不超过 2048 px 的尺寸与 `--resolution 4k` / `--4k` 组合；脚本会直接拒绝。
- **模型约束**：`sunburst` 不在 4K 账户组，4K 请求会强制切到 `flare`；显式 `--model sunburst --4k` 会被拒绝并给出提示。
- **编辑原则**：提供参考图的普通编辑默认只传 `image + prompt`。除非用户显性要求蒙版、局部重绘或指定编辑区域，否则不创建、不推断、不传入 `mask`，也不自动追加蒙版编辑调用。
- **交付原则**：脚本 100% 原始透传上游返回的图像数据，不做任何本地拉伸或插值，如实向用户反馈物理像素尺寸、模型、档位与传输方式。

## 请求端点与编码

| 场景 | 端点 | Content-Type |
|---|---|---|
| 纯文生图（同步） | `POST /v1/images/generations` | `application/json` |
| 图片编辑（同步） | `POST /v1/images/edits` | `multipart/form-data` |
| 纯文生图（异步） | `POST /v1/images/generations/async` | `application/json` |
| 图片编辑（异步） | `POST /v1/images/edits/async` | `multipart/form-data`（支持本地文件） |
| 异步轮询 | `GET /v1/images/tasks/{task_id}` | — |

- 异步提交返回 `202` 与 `task_id`；轮询 `status` 为 `processing` / `completed` / `failed`，完成后从 `image_url` 或 `result.data[0].url` 下载。
- 异步任务与结果 24 小时后过期；单任务最长运行 30 分钟；必须使用提交时同一把 Key 轮询。
- 异步依赖服务端 S3 兼容对象存储；未配置时返回 404 且不创建任务，此时用 `--async never`。
- `--image` 与 `--mask` 可接收本地路径、HTTP(S) URL 或 data URL；脚本统一解析为二进制文件后上传。
