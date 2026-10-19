# pdog-image2-mcp

Pdog GPT Image 2.5 生图/图像编辑的 **MCP server + ZCode 插件**。逻辑提取自
`pdog-gpt-image2` 技能的 `generate_image.py`，行为保持一致（网络兜底、档位分流、扣费守卫、原样交付），
并新增协议级的 4K 扣费确认、结构化返回与密钥隔离。

**本仓库即插件包**：安装插件一次性完成「MCP 注册 + 自动连接 + 判断层技能」。

## 目录结构

```
├── .zcode-plugin/plugin.json    # 插件清单：mcpServers + skills
├── skills/pdog-image/SKILL.md   # 判断层技能（4K 决策树、提示词语义、透明验收）
├── pdog_image_mcp/              # 服务端源码
│   ├── core.py                  # 引擎：配置/路由/HTTP/异步编排（PdogError 结构化）
│   ├── netlayer.py              # DoH Fake-IP 兜底 + 无 SNI + 证书强校验
│   ├── transparency.py          # 透明验证（alpha>=8 BBox / >=16 碰边 / 软边）
│   ├── chroma.py                # 动态色键转 Alpha（可选依赖 cv2）
│   ├── server.py                # FastMCP：5 工具 + 4 resources
│   └── cli.py                   # 技能对齐命令行（pdog-image）
├── docs/                        # MCP resources 暴露的参考文档
└── tests/                       # 单元 + stdio 协议 + 真实 API（门控）测试
```

## 工具面

| 工具 | 费用 | 说明 |
|---|---|---|
| `pdog_generate` | 计费 | 文生图；**立即返回 job_id**，后台渲染（约 20-120s）；4K 必须 `allow_4k=true`（协议级守卫） |
| `pdog_edit` | 计费 | 参考图整图编辑（默认 sunburst）；同样 job 模式；仅用户显性要求时传 `mask` |
| `pdog_task_status` | 免费 | 轮询 job_id（或上游 task_id）；wait_seconds≤25；完成返回文件与实际尺寸 |
| `pdog_verify_transparency` | 免费 | alpha 统计/BBox/碰边判定（pass / review / fail / opaque） |
| `pdog_chroma_key` | 免费 | 独立色键抠图，调参重试不再扣生成费（需 `--extra chroma`） |

## 超时适配（job 模式，v1.1.0）

MCP 客户端通常在 30s 处中止工具调用，而渲染需要 20-120s。因此计费调用**秒级返回
`job_id`**，渲染在服务端后台线程执行，模型用 `pdog_task_status(job_id)` 轮询——
单次调用永远远低于客户端超时，客户端超时也不再可能孤儿化已计费任务。
参数错误与 4K 守卫在开任务前同步报错（预检）；job 仅存在于当前会话进程内。

Resources：`pdog://docs/api-spec`、`pdog://docs/sizes`、`pdog://docs/errors`、`pdog://docs/chroma-key`。

返回值为结构化 JSON：`files[].path/width/height`、`actual_size`（上游实返尺寸，**可能与请求不符，交付时如实转述**）、`model`、`tier`、`transport`、`transparency_check`。错误统一 `{"ok": false, "error": {kind, message, hint}}`，kind 含 `cost_guard` / `invalid_parameter` / `http` / `network` / `upstream` / `config`。

## 安装

前置：[uv](https://docs.astral.sh/uv/)（`uv --version` 自检）。安装插件本身不需要先装 uv，
但**首次启动 MCP 时**会用它自动创建环境并安装依赖（含色键），所以目标机最终必须有 uv。

**方式 A — 用 Git 地址添加市场（给别人分发用这个）**：设置 → 插件管理 → 发现 → 「+」，
选择 **Git / GitHub 仓库**，填：

```
https://github.com/zhou470259742/gpt-image-mcp
```

仓库根目录的 `marketplace.json` 就是市场清单，`plugins[].source` 为 `./`，即插件本体在仓库根。
添加市场后在发现页点 `pdog-image` 的 **Get** 即可；后续更新走同一个入口。
对方机器上**不需要**提前下载仓库，ZCode 会自行 clone。

**方式 A2 — 本地目录市场**：把一个包含 `marketplace.json` 的目录添加为市场，
即可在发现页列表中安装/更新 `pdog-image`；往市场清单里继续加插件条目即可扩展。

**方式 A3 — 直接装插件目录**：设置 → 插件管理 → 发现 → 「+」添加本地目录，
选择本仓库根目录。适合自己开发调试。

安装后密钥放 `~/.pdog-image/config.json`（见下文「配置与密钥」），**不需要重新安装**——
配置与代码分离，改 Key、加路由都不用升级插件。

**方式 B — 仅注册 MCP**：在 `~/.zcode/cli/config.json` 的 `mcp.servers` 加入
（把路径替换为本仓库在你机器上的实际位置）：

```json
{
  "pdog-image": {
    "command": "uv",
    "args": ["run", "--project", "/path/to/pdog-image2-mcp", "--extra", "chroma", "pdog-image-mcp"]
  }
}
```

**方式 C — 任意其他 MCP 客户端**：同上的 `command/args` 形式接入 stdio。

## 可移植性（移动位置 / 发给别人）

- **插件内零绝对路径**：MCP 启动声明在根级 `.mcp.json`，使用 `${CLAUDE_PLUGIN_ROOT}`
  变量（与官方 android-emulator 插件同款机制）——插件装到哪，根目录解析到哪；
  移动目录、换机器、通过 marketplace（本地目录/Git）分发都不会失效。
- **分发前提**：目标机器装有 [uv](https://docs.astral.sh/uv/)（首次连接时 `uv run`
  会按 `pyproject.toml` 自动构建环境）；密钥用 `PDOG_API_KEY_*` 环境变量或目标机的
  config.json 提供，包内不含任何密钥。
- 方式 B 中的绝对路径只是"本机用户级注册"的示例，属于用户自己的配置，不随插件分发。

## 更新已安装的插件（重要）

ZCode 的 directory 类市场在**添加时会把清单与插件内容快照**到客户端缓存
（`~/.zcode/cli/plugins/marketplaces/hx-zcode-work/`），发现页读的是快照而非工作空间。
因此改完工作空间代码后需要发布一步：

```bash
# 1) 改代码并验证：uv run pytest tests/ -q --ignore=tests/test_live_api.py
# 2) 版本号 +1（两处保持一致）：.zcode-plugin/plugin.json 和 ../marketplace.json
./scripts/publish.sh    # 3) 同步到市场快照
# 4) 设置 → 插件管理 → 发现 → pdog-image → 更新
# 5) 开新会话（MCP server 是会话级进程）
```

若发现页迟迟不刷新出新版本，重启 ZCode 客户端，或删除市场后重新添加。

## 配置与密钥

优先级：环境变量 > 显式路径（`$PDOG_CONFIG`）> **用户级 `~/.pdog-image/config.json`**（推荐的本机密钥存放处，跨插件更新/市场快照刷新稳定）> `<插件目录>/config.json` > 旧技能目录（已废弃）。

已安装插件（缓存副本）建议把密钥放用户级位置：
```bash
mkdir -p ~/.pdog-image && cp config.example.json ~/.pdog-image/config.json  # 填入真实 Key
```

### 多路由配置（推荐）

`routes` 是一个**有序数组**：第一个能服务该档位的路由是主路由，失败时按顺序尝试后面的。
每条路由有自己的 `base_url`、Key 和模型，因此 1K/2K 与 4K 可以指向完全不同的网关：

```json
{
  "routes": [
    {
      "name": "main",
      "base_url": "https://ai.whyshy.cn",
      "api_keys": { "1k": "sk-...", "4k": "sk-..." },
      "generation": "gpt-image-2.5-flare",
      "edit": "gpt-image-2.5-sunburst",
      "generation_4k": "gpt-image-2",
      "edit_4k": "gpt-image-2"
    },
    {
      "name": "backup",
      "base_url": "https://backup.example/v1",
      "api_keys": { "1k": "sk-...", "4k": "sk-..." },
      "generation": "gpt-image-2.5-flare",
      "edit": "gpt-image-2.5-sunburst",
      "tiers": ["1k", "2k"]
    }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `name` | 路由名，出现在结果与日志里 |
| `base_url` | 该路由的网关地址；缺省时继承顶层 `base_url` |
| `api_keys.1k` / `api_keys.4k` | 该路由的普通档 / 4K 档 Key；`1k` 同时服务 2K |
| `api_key` | 单 Key 简写，未分档时两个档位共用 |
| `generation` / `edit` | 文生图 / 编辑各自的模型 ID，可以不同 |
| `generation_4k` / `edit_4k` | 4K 档专用模型，缺省时回落到对应的通用模型 |
| `tiers` | 该路由只服务哪些档位（`1k`/`2k`/`4k`），省略表示全部 |

**兜底规则**（重要）：

- 路由链按 `routes` 数组顺序执行，第一条是主路由。
- 只有**不可能已计费**的失败才会切换：连接失败、HTTP 4xx/5xx 拒绝、模型不存在。
- **请求超时不会切换**——请求可能已送达上游并计费，换路由重试会重复扣费。
- **异步任务一旦拿到 `task_id` 就不会切换**：任务已计费，只轮询或放弃。
- 切换后结果里 `route` 是实际使用的路由，`route_fallback` 记录原因；模型不同时另有 `model_fallback`。交付时应如实转述。

旧的扁平配置（顶层 `base_url` + `api_keys` + `models`）仍然支持，会被自动当作单条路由，
行为与之前一致。

| 环境变量 | 作用 |
|---|---|
| `PDOG_BASE_URL` | 覆盖顶层网关地址（未显式配置 `base_url` 的路由会继承） |
| `PDOG_API_KEY_1K` / `PDOG_API_KEY_4K` | 普通档 / 4K 档 Key（作用于顶层 `api_keys`） |
| `PDOG_CONFIG` | 指定配置文件路径 |

密钥永不进入工具返回值与日志（`mask_key` 掩码 + 错误信息清洗）；多路由下所有路由的 Key 都会被清洗。
分发部署时用 `config.example.json` 作模板，**不要提交真实 `config.json`**（已 gitignore）。

## 本地开发与测试

```bash
uv sync --extra chroma          # 引擎 + 色键依赖；主链路仅 mcp+pillow

uv run pytest tests/ -q --ignore=tests/test_live_api.py   # 免费全套（50 例）
uv run pdog-image --prompt "测试" --dry-run               # CLI 预览路由，不扣费

RUN_LIVE=1 uv run pytest tests/test_live_api.py -q -s     # 真实 API（约 ¥0.12/轮）
```

免费协议测试使用指向本地不可达端点的隔离配置 + 假 Key，任何意外调用都会在
连接阶段本地失败，**不可能产生真实流量或计费**。

真实 API 覆盖：1K 同步生成 → 透明验证 → multipart 编辑（sunburst）→
4K 异步（提交/轮询/下载）。产物在 `output/live/`。

## 行为准则（与原技能一致）

- 编辑默认只传 images + prompt；不自制、不推断 mask，不自动追加第二次调用。
- 4K 仅显性请求 + `allow_4k=true`；伪 4K（≤2048 最大边走 4K 路由）直接拒绝。
- 透明背景：安装版启动脚本已带 `--extra chroma`，色键可直接用。网关实测**不出原生 Alpha**，所以 `transparent` 请求会先按原生路线尝试，失败时把原图存成 `-noalpha.png` 并报错；要真正拿到透明图，用 `transparent_strategy='chroma-key'`。文生图用色键时必须显式给 `key_color`（如 `#00FF00`），`auto` 需要参考图来取背景色。转换不缩放。
- 异步任务已计费：超时返回 `task_id`，只轮询不重提（`pdog_task_status`）。
- 400/401/402/404 不重试；502/503/504 指数退避最多 2 次。
- DoH Fake-IP 兜底 + 无 SNI 连接强制证书校验（CA 链 + SAN），校验失败立即中止。

## 网关实况（2026-09-16 实测，原技能文档已过时的部分）

- **4K 账户组当前不提供 2.5 模型族**：`gpt-image-2.5-flare` / `sunburst` 在 4K 组的
  同步（HTTP 404）与异步（task failed: model_not_found）路由均不可用；
  旧版 `gpt-image-2` 在 4K 组同步路由实测可出真 4K（3840x2160）。
- 引擎对策（v1.2.0）：4K 的 auto 路由**默认直连 gpt-image-2**（配置键 `generation_4k`/`edit_4k`，
  生成与编辑同），不再先试注定失败的 2.5；显式指定 2.5 模型遇 `model_not_found` 仍自动回落一次
  并带 `model_fallback` 标注。非 4K 档不受影响。
- **quality 降级**：4K 异步校验仅接受 low/medium/high（xhigh 会在渲染前被拒），
  引擎自动降为 high 并在结果 `quality_note` 注明。
- 异步链路本身完好：普通组 1K `--async always` 实测提交→轮询→completed→下载全通。
  4K 自动异步保留（若服务商恢复 4K 组的 2.5 配置即自动受益）。
