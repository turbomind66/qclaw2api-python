<div align="center">

# qclaw2api-python

**把本机 QClaw（openclaw 桌面客户端）的本地网关封装成标准 OpenAI API 的反向代理**

[![Python](https://img.shields.io/badge/Python-3.9%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Dependencies](https://img.shields.io/badge/dependencies-requests%20%2B%20redis-brightgreen)](./requirements.txt)

</div>

---

> **⚠️ 免责声明**
> 本项目为**个人学习与技术研究用途**的第三方工具，与腾讯官方无任何关联，非官方出品、不受官方支持。
> 本项目不破解、不绕过任何付费机制，仅复用你本人已取得的使用权，把官方网关能力转换为标准 OpenAI 协议接口。
> 使用本项目即表示你承诺遵守 QClaw 的服务条款。因使用本项目产生的任何账号风险或损失，由使用者自行承担。
> 若收到官方的停止使用要求，请立即停止使用并删除本项目。

---

## 这个项目的定位

QClaw（openclaw 桌面客户端）会在本机 `127.0.0.1:62522` 起一个 `loopback` 模式的本地网关，
其 `/v1/chat/completions`、`/v1/models` **本身就是 OpenAI 兼容协议**。
所以本项目**不做协议转换** —— 它的价值在「多 key 运营」：

| 能力 | 说明 |
| --- | --- |
| **多 key 账号池** | `auths/` 下放多个 apiKey，三因子加权挑选 + 防惊群 + 在途租约限流 |
| **熔断与冷却** | 连续失败指数退避熔断（30m → 6h 封顶）；额度耗尽冷却至次日 04:00；429 短冷却 |
| **失效 key 自动隔离** | 401/403 立即禁用；定期探活，避免请求时才撞上死 key |
| **粘性会话** | 同一对话尽量路由到同一 key（TTL + 自动 GC） |
| **QClaw 约束修正** | 自动补 system 消息、max_tokens 可按需钳制（见下文） |
| **Redis 镜像** | 池状态可选镜像到 Upstash；未配置自动降级为纯内存模式 |

> 默认上游即本地网关 `http://127.0.0.1:62522/v1`（由 `cli/key.py extract` 自动读取
> `~/.qclaw/openclaw.json` 里的 `gateway.auth.token`）。如果你的 QClaw 是远程 API 形态，
> 把 `upstream.base_url` 改成对应的 OpenAI 端点即可。

### 与 workbuddy2api-python 的关系

本项目的账号池（`pool.py`）、粘性会话（`session.py`）、Redis 镜像（`redisstore.py`）、
路径锚定（`projpath.py`）**直接移植自** [workbuddy2api-python](https://github.com/turbomind66/workbuddy2api-python)，
仅改包名与 logger 名。差异集中在凭证与上游两层：

| 维度 | workbuddy2api | qclaw2api |
| --- | --- | --- |
| 上游协议 | 私有协议 → 需改写 + SSE 再封装 | **原生 OpenAI**，直接透传 |
| 凭证形态 | OAuth access/refresh token，需刷新 | **静态 apiKey**，无刷新 |
| 挑号依据 | 积分 credits | 剩余额度点数 |
| 定时任务 | 每日签到 + 保活 | **定期探活**（无签到概念） |
| 流式实现 | 解析上游 SSE 再重新封装 | **字节流直接转发** |

---

## 📦 目录结构

```
qclaw2api-python/
├── cli/
│   ├── server.py          # 启动代理主服务
│   ├── key.py             # 凭证管理（extract / add / list / remove / gen-fake）
│   └── probe.py           # 对所有 key 做一次探活
├── q2api/
│   ├── config.py          # 配置加载（JSON + Q2A_* 环境变量覆盖）
│   ├── auth.py            # apiKey 凭证解析（静态，无 refresh）
│   ├── pool.py            # 账号池（状态机 / 熔断 / 租约 / 加权挑选）
│   ├── session.py         # 粘性会话路由
│   ├── redisstore.py      # Upstash 镜像 / Noop 降级
│   ├── scheduler.py       # 定期探活
│   ├── netutil.py         # 本机回环的代理绕过
│   ├── server.py          # HTTP 服务与端点
│   └── upstream/          # 上游客户端（透传 + 两条硬约束修正）
├── scripts/
│   ├── fake_upstream.py   # 假 QClaw 上游（复刻真实约束，供联调）
│   └── smoke.py           # 端到端冒烟（无需真实 key）
└── config.example.json
```

---

## ⚠️ QClaw 的硬约束（本项目已自动处理）

QClaw 网关与标准 OpenAI 协议存在两处**不一致**，不了解会一直吃 400：

1. **只传一条 `user` 消息会返回 `400 invalid request`**
   必须带至少一条 `system` 消息。
   → `upstream/client.py` 在转发时自动补一条最小 system（可用 `upstream.auto_system_prompt` 关闭）。

2. **`max_tokens` 可按需钳制**
   本地 openclaw 网关由自身限制出参长度；若你对接的是远程 QClaw 且它要求 ≤8192，
   把 `upstream.max_tokens_cap` 设为 `8192` 即可自动钳制（`0` 表示不钳制，原样透传）。

模型 id 规则：本地网关接受的模型是 **`openclaw` / `openclaw/main` / `openclaw/default`**，
`openclaw` 等价于默认 agent。客户端传 `auto` / 空 / `gpt-4o` / `claude` 等常见别名会
自动归一化为 `upstream.default_model`（默认 `openclaw`）；其余模型名原样透传。

---

## 🚀 快速开始

### 1. 安装依赖

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS
pip install -r requirements.txt
```

### 2. 准备配置

```bash
copy config.example.json config.json   # Windows
```

至少修改 `api_key` 为你自己的密钥（客户端接入时用）。

### 3. 获取 apiKey 并落盘

本机已安装 QClaw（openclaw 桌面客户端）时，网关 token 就在 `~/.qclaw/openclaw.json` 里，
一条命令即可提取并写入 `auths/`：

```bash
py cli/key.py extract        # 自动探测 ~/.qclaw/openclaw.json → 读 gateway.auth.token → 写入 auths/
py cli/key.py list           # 确认已写入（key 显示时会自动脱敏）
py cli/probe.py              # 探活，确认本地网关可达且 token 有效
```

`extract` 默认会直连 `http://127.0.0.1:<port>/v1/models` 验证连通性（`--no-verify` 可跳过）。
若 QClaw 装在非默认位置，用 `py cli/key.py extract --path <openclaw.json绝对路径>`。

> 说明：本命令**只读取明文可读的本地网关 token**，不解密 Electron `safeStorage`
> （`%APPDATA%\QClaw\app-store.json` 的 DPAPI 密文）。绝大多数桌面客户端场景用 `extract` 即可。

拿到明文 key 后也可手动录入（或用于远程 QClaw）：

```bash
py cli/key.py add sk-你的key --nickname 主账号
```

需要多 key 轮换就重复 `add`，每个 key 一个文件。

> ⚠️ `auths/` 已在 `.gitignore` 中排除，**请勿将凭证提交到任何公开仓库**。

### 4. 启动服务

```bash
py cli/server.py -config config.json
```

看到 `loaded N account(s)` 且 `listening on :7864` 即启动成功。

---

## 🔌 客户端接入

任意 OpenAI 兼容客户端（Cherry Studio、ChatBox、LobeChat、OpenClaw 等）：

| 配置项 | 值 |
| --- | --- |
| Base URL | `http://127.0.0.1:7864/v1` |
| API Key | `config.json` 里的 `api_key` |
| 模型 | `/v1/models` 返回的任意 id，或填 `auto` |

```bash
curl http://127.0.0.1:7864/v1/chat/completions \
  -H "Authorization: Bearer 你的api_key" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"
```

---

## 🌐 HTTP 端点

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| GET | `/healthz` | 否 | 健康检查；无可服务账号时返回 503 |
| GET | `/status` | 是 | 完整账号池状态（uid / 脱敏 key / 冷却 / 熔断 / 在途） |
| GET | `/v1/models` | 是 | 模型列表（动态拉取 + 静态兜底） |
| POST | `/v1/chat/completions` | 是 | 对话（`stream:true/false` 均可） |

---

## 🛠 CLI

| 命令 | 说明 |
| --- | --- |
| `py cli/server.py -config config.json` | 启动代理主服务 |
| `py cli/key.py extract [--path x] [--no-verify]` | 从本机 QClaw 提取网关 token 并落盘 |
| `py cli/key.py add <apiKey> [--nickname x]` | 手动新增凭证 |
| `py cli/key.py list` | 列出凭证（key 脱敏） |
| `py cli/key.py remove <uid>` | 删除凭证 |
| `py cli/key.py gen-fake <N>` | 生成 N 个假凭证（仅供联调） |
| `py cli/probe.py [--json]` | 对所有 key 探活 |

> **路径说明**：所有 CLI 的相对路径都由 `q2api/projpath.py` 统一解析 —— 先按当前目录找，
> 找不到自动回退项目根。因此 `cd cli` 后直接跑 `server.py` / `probe.py` 也能正确定位账号。

---

## ⚙️ 配置说明

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `listen` | `:7864` | 监听地址 |
| `api_key` | — | 客户端鉴权密钥 |
| `auth_dir` | `./auths` | 凭证目录 |
| `state_file` | `./data/state.json` | 池状态持久化 |
| `cooldown.soft_rate` | `60s` | 429 短冷却 |
| `schedule.probe_interval` | `30m` | 定期探活间隔 |
| `upstream.base_url` | `http://127.0.0.1:62522/v1` | 上游前缀（自带 `/v1`）；远程 QClaw 改成对应端点 |
| `upstream.timeout_seconds` | `120` | 上游超时 |
| `upstream.max_tokens_cap` | `0` | 输出上限钳制，`0` 表示不钳制；远程 QClaw 可设 `8192` |
| `upstream.auto_system_prompt` | `true` | 自动补 system 消息 |
| `upstream.auto_system_text` | `You are a helpful assistant.` | 补充的 system 内容 |
| `upstream.default_model` | `openclaw` | `auto`/空/常见别名归一化到的目标模型 |
| `upstash.url` / `upstash.token` | 空 | Redis 镜像（留空则纯内存） |
| `pool.*` | 见模板 | 限流 / 熔断 / 权重参数 |
| `session_sticky.*` | 见模板 | 粘性会话开关与 TTL |

环境变量覆盖（`Q2A_*` 前缀）：`Q2A_LISTEN`、`Q2A_API_KEY`、`Q2A_AUTH_DIR`、
`Q2A_STATE_FILE`、`Q2A_SOFT_RATE`、`Q2A_UPSTREAM_BASE`、`Q2A_TIMEOUT_SECONDS`、
`Q2A_AUTO_SYSTEM_PROMPT`、`Q2A_UPSTREAM_DEFAULT_MODEL`。

---

## 🧪 冒烟测试（无需真实 key）

```bash
py scripts/smoke.py
```

`scripts/fake_upstream.py` 是一个刻意复刻了 QClaw 真实约束的假上游
（无 system → 400、max_tokens 超上限 → 400、key 含 `dead` → 401、含 `quota` → 402）。
冒烟覆盖 25 项断言：

- 账号池错误策略（死 key 禁用 / 额度耗尽长冷却 / 正常 key 仍可选）
- HTTP 全链路（healthz、models、非流式、流式 SSE、自动补 system、max_tokens 钳制、
  模型名归一化、鉴权、换号、粘性会话）
- 全部 key 失效时的兜底（透传上游 401 而非 500，healthz 转 503）

---

## ❓ 常见问题

<details>
<summary><b>上游返回 400 invalid request</b></summary>

大概率是「只传了 user 消息」。确认 `upstream.auto_system_prompt` 为 `true`；
若客户端确实需要纯 user，可自行在消息里加一条 system。
</details>

<details>
<summary><b>连本地自建网关出现 404 / Bad request syntax / RemoteDisconnected</b></summary>

办公环境常设了 `HTTP_PROXY`，`requests` 默认信任环境变量，连 `127.0.0.1` 也会被劫到代理。
本项目已处理：回环地址自动 `trust_env=False`，CLI 侧补 `NO_PROXY`（见 `q2api/netutil.py`）。
</details>

<details>
<summary><b>启动提示未找到凭证</b></summary>

`auths/` 下需要 `qclaw-*.json`，最少内容 `{"api_key": "sk-xxx"}`。
用 `py cli/key.py add <key>` 生成。
</details>

---

## ⚠️ 已知风险

- 本项目依赖**本机已安装并登录的 QClaw 桌面客户端**，其本地网关 `127.0.0.1:62522`
  必须处于运行状态，代理才能转发请求。`cli/key.py extract` 提取的 token 由 QClaw 生成，
  若客户端重装/退出登录可能失效，届时重新 `extract` 即可。
- 多 key 轮换的收益取决于你手上是否仍有有效订阅或额度；投入前建议先用
  `py cli/probe.py` 确认 key 能正常返回 200。

---

## 📄 许可证

MIT。软件按「原样」提供，不含任何明示或默示担保。

---

<div align="center">

Made with ❤️ by the community

</div>
