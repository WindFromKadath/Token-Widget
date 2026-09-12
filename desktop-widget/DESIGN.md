# Token 桌面挂件 · 完整设计文档（实现交付版 v3）

> 版本：v3（2026-08-03） 状态：**定稿，可直接交付实现**
> 读者：实现者（人或编码 Agent）。本文档自包含，不依赖对话上下文。
> 参考实现：`../legacy/opencode-go-bridge/`（OpenCode Go 采集器已按本文档 §4.4 实现并线上验证）。

---

## 1. 产品概述

一个常驻 Windows 桌面的小窗挂件，**镜像用户在 CC Switch 中已配置的 Coding / Token Plan**。
用户在小窗中勾选要盯的套餐，即可一眼看到每个套餐的剩余额度、重置倒计时与当日消耗。

### 1.1 核心原则（不可违背）

| # | 原则 | 说明 |
|---|---|---|
| P1 | **CC Switch 是唯一配置源** | 供应商增删改只能在 CC Switch 中进行。挂件对 `cc-switch.db` 只读，永不写入 |
| P2 | **配置零重复** | 连每个 plan 的用量查询脚本都复用 CC Switch 已存的（`providers.meta.usage_script`） |
| P3 | **只读展示** | 不做供应商切换、不碰 MCP/代理配置、不当中转（不当第二个 New API） |
| P4 | **用户选择显示谁** | 勾选状态是挂件唯一的自有持久化数据 |
| P5 | **失败降级不阻断** | 单个 plan 失败不影响其他 plan 的展示 |

### 1.2 范围

**M1 做**：plan 勾选、额度条（自定义脚本类 + Kimi 模板 + OpenCode Go）、当日消耗摘要、托盘、自动刷新、DB 变更跟随。
**M1 不做**：官方订阅 OAuth 额度、历史趋势图、通知提醒、打包分发、多设备、火山方舟签名模板。

---

## 2. 数据模型（统一契约）

以下类型为挂件内部统一表示，各采集器必须归一化到此模型（TypeScript 记法，实现语言可映射）：

```ts
// 一个可展示的套餐（来自 CC Switch providers 表 + 挂件勾选状态）
interface Plan {
  id: string;                 // providers.id
  appType: string;            // providers.app_type: claude|codex|gemini|opencode|...
  name: string;               // providers.name
  category: string | null;    // official|cn_official|third_party|aggregator
  icon: string | null;        // providers.icon
  iconColor: string | null;   // providers.icon_color
  isCurrent: boolean;         // providers.is_current（该 app 下当前启用的供应商）
  quotaKind: QuotaKind;       // 见下，决定走哪个采集器
  selected: boolean;          // 挂件勾选状态（自有 config）
  autoQueryIntervalMin: number; // meta.usage_script.autoQueryInterval，默认 5
}

type QuotaKind =
  | 'custom_script'   // meta.usage_script.templateType 为 null/'custom' 且 code 非空
  | 'token_plan'      // templateType === 'token_plan'，按 codingPlanProvider 路由（§4.3）
  | 'official_sub'    // category === 'official'（§4.5）
  | 'balance'         // templateType === 'balance'，原生余额查询（§4.7）
  | 'consumption_only';// 无 usage_script 或脚本未启用

// 归一化配额结果（所有采集器的输出）
interface QuotaResult {
  planId: string;
  ok: boolean;
  windows: QuotaWindow[];     // ok=true 时至少一项
  planLabel?: string;         // 如 "OpenCode Go"、"Kimi For Coding (active)"
  extra?: string;             // 附加展示文本（脚本 extra 字段原样透传）
  fetchedAt: number;          // unix 秒
  stale: boolean;             // 本次失败、展示的是上次成功数据
  error?: QuotaError;         // ok=false 时必填
}

interface QuotaWindow {
  key: string;                // '5h'|'weekly'|'monthly'|'balance'|...
  label: string;              // 展示名，如 '5小时'、'每周'、'余额 CNY'
  usedPercent: number | null; // 0-100，已用百分比；null = 余额类（无总量概念，
                              // 以 usedText 展示金额，不参与最紧张排序）
  resetInSec?: number;        // 距重置秒数（余额类无）
  usedText?: string;          // 如 "$1.23 / $12.00"、"¥207.37"
}

type QuotaError =
  | 'cookie_expired'   // OpenCode Go 凭据失效（或 workspace_id 错）
  | 'no_subscription'  // 该账号无此套餐
  | 'auth_failed'      // API key 无效（HTTP 401/403）
  | 'fetch_failed'     // 网络/超时/5xx
  | 'parse_failed'     // 响应结构变化
  | 'script_failed'    // JS 执行错误
  | 'db_locked'        // cc-switch.db 暂时不可读
  | 'unsupported'      // M1 不支持的模板类型
  | 'upstream_error';  // 上游业务级错误（HTTP 2xx 但业务码失败，按其 message 展示）
  | 'oauth_login_required'; // 官方 CLI 未登录/凭据失效（Claude/Codex/Gemini OAuth）

// 当日消耗（来自 usage_daily_rollups）
interface ConsumptionSummary {
  date: string;               // YYYY-MM-DD（本地时区）
  totalCostUsd: number;
  totalTokens: number;        // input+output+cache_read+cache_creation 求和
  byPlan: { planId: string; costUsd: number; tokens: number; models: string[] }[];
}
```

---

## 3. 数据源规范

### 3.1 CC Switch 数据库

| 项 | 值 |
|---|---|
| 路径（Windows） | `%USERPROFILE%\.cc-switch\cc-switch.db` |
| 路径（macOS/Linux） | `~/.cc-switch/cc-switch.db` |
| 打开方式 | SQLite，URI `file:<path>?mode=ro`（只读）+ `busy_timeout=3000` 失败重试 3 次 |
| 并发 | CC Switch 运行中会写入；只读连接 + 重试即可，绝不以读写模式打开 |
| 变更跟随 | 轮询文件 mtime（2s 间隔），变化后重读 Plan 清单并刷新勾选面板 |

**Schema 漂移防御**：所有查询按列名读取；启动时探测 `providers` / `usage_daily_rollups`
是否存在、所需列是否存在；缺失时该功能降级（如只显示 OpenCode Go 桥接模式），不崩溃。

### 3.2 providers 表（Plan 清单来源）

查询：

```sql
SELECT id, app_type, name, category, icon, icon_color, is_current,
       settings_config, meta
FROM providers ORDER BY app_type, sort_index;
```

字段语义：

| 字段 | 说明 |
|---|---|
| `settings_config` | JSON 字符串，形态随 app_type 变（§3.3），含 API key 等敏感信息——**只允许内存使用，禁止落盘/日志** |
| `meta` | JSON 字符串，其中 `usage_script` 子对象定义用量查询（§3.4） |
| `is_current` | 1 = 该 app_type 下当前启用的供应商，UI 上加标记 |

### 3.3 settings_config 形态（实测样例，用于凭证解析）

| app_type | 实测键结构 | API key 取法 | base_url 取法 |
|---|---|---|---|
| `codex` | `{"auth":{"OPENAI_API_KEY":"..."},"config":{...}}` | `auth.OPENAI_API_KEY` | `provider_endpoints` 表该 provider 的 url（兜底空串） |
| `gemini` | `{"env":{"GOOGLE_GEMINI_BASE_URL":"...","GEMINI_API_KEY":"...","GEMINI_MODEL":"..."}}` | `env.GEMINI_API_KEY` | `env.GOOGLE_GEMINI_BASE_URL` |
| `claude`（官方） | `{}`（官方登录，无 key） | — | — |
| `claude`（第三方） | 通常 `{"env":{"ANTHROPIC_AUTH_TOKEN":"...","ANTHROPIC_BASE_URL":"..."}}` | `env.ANTHROPIC_AUTH_TOKEN` | `env.ANTHROPIC_BASE_URL` |

> 凭证解析顺序与 CC Switch 保持一致：脚本内显式值 > settings_config 推导值。
> 参考 CC Switch 源码 `Provider::resolve_usage_credentials`（`src-tauri/src/provider/`）。

### 3.4 meta.usage_script 结构（实测）

```jsonc
{
  "usage_script": {
    "enabled": true,
    "language": "javascript",
    "code": "({ request: {...}, extractor: function(response){...} })",
    "timeout": 10,                  // 秒
    "templateType": "custom",       // null|'custom'|'token_plan'|'new_api'|'通用'等
    "autoQueryInterval": 5,         // 分钟，0=不自动刷
    "codingPlanProvider": "kimi",   // 仅 token_plan 模板有：kimi|zhipu|zhipu_team|minimax|zenmux|volcengine
    "apiKey": "...",                // 可选显式覆盖
    "baseUrl": "...",               // 可选显式覆盖
    "accessToken": "...", "userId": "..." // 可选（new_api 模板）
  }
}
```

`templateType` 路由规则（决定 QuotaKind）：

- `templateType` 为空/`custom` 且 `code` 非空 → `custom_script`（§4.2）
- `templateType == 'token_plan'` → `token_plan`，按 `codingPlanProvider` 分发（§4.3）
- `templateType == 'balance'` → `balance`，原生余额查询（§4.7，DeepSeek 等无 code）
- `enabled == false` 或无 `usage_script` → `consumption_only`（§4.6）
- `category == 'official'` → `official_sub`（§4.5）

### 3.5 usage_daily_rollups 表（消耗来源，免解析任何日志）

列：`date, app_type, provider_id, model, request_model, pricing_model,
request_count, success_count, input_tokens, output_tokens, cache_read_tokens,
cache_creation_tokens, total_cost_usd, avg_latency_ms, ...`

- `provider_id` 为 `_session` / `_codex_session` 表示来自会话导入（非代理流量）
- 当日汇总 SQL（按 provider + model 分组，Python 侧聚合出 by_plan / by_model 两级）：

```sql
SELECT provider_id, model,
       SUM(total_cost_usd) AS cost,
       SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) AS tokens
FROM usage_daily_rollups
WHERE date = :today          -- 本地时区 YYYY-MM-DD
GROUP BY provider_id, model
ORDER BY cost DESC;
```

---

## 4. 配额采集器规范

### 4.1 总流程

```
for each selected Plan:
  match plan.quotaKind:
    custom_script    → §4.2 执行 CC Switch 存的 JS
    token_plan       → §4.3 内置模板（M1 仅 kimi 必做）
    official_sub     → §4.5 返回 unsupported（UI 只展示消耗）
    consumption_only → §4.6 返回 None（UI 只展示消耗）
  归一化为 QuotaResult
```

### 4.2 custom_script 执行器

**协议**（与 CC Switch 完全一致，参考 `src-tauri/src/usage_script.rs`）：

1. 取 `code`（形如 `({ request: {...}, extractor: function(response){...} })`）
2. 占位符替换（在 request 的字符串值中）：
   `{{apiKey}}`、`{{baseUrl}}`、`{{accessToken}}`、`{{userId}}`
   —— 兼容变体 `{{ apiKey }}`、`{{api_key}}`、`{{base_url}}`（CC Switch #2954 已修，跟随）
   - apiKey/baseUrl 解析顺序：usage_script 显式值 > §3.3 推导值；baseUrl 去掉尾部 `/`
3. 按 `request` 发起 HTTP（默认方法 GET；超时 `timeout` 秒）
4. 以响应 JSON（或文本）为入参调用 `extractor(response)`，返回值字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `isValid` | bool | false 时展示 `invalidMessage` |
| `invalidMessage` | string | 失效提示 |
| `remaining`/`used`/`total` | number | 额度数值 |
| `unit` | string | `USD`/`%`/`次` 等 |
| `planName` | string | 套餐名 |
| `extra` | string | 附加文本（原样透传到 QuotaResult.extra） |
| （数组） | — | 返回数组 = 多套餐，逐个展示 |

**执行环境**：优先系统 Node.js（`node --version` 探测），用如下包装器以子进程执行：

```js
// runner.js —— argv: [scriptFile, inputJsonFile]
// inputJson: { code, request, timeoutSec }
// 向 stdout 输出 extractor 的返回值 JSON；错误输出到 stderr 并非零退出
const fs = require('fs');
const { code, request, timeoutSec } = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const script = eval(code);                       // code 是完整 ({request, extractor}) 对象
const req = script.request, url = req.url;
const ctrl = new AbortController();
const timer = setTimeout(() => ctrl.abort(), (timeoutSec || 10) * 1000);
fetch(url, { method: req.method || 'GET', headers: req.headers || {},
             body: req.body ? JSON.stringify(req.body) : undefined, signal: ctrl.signal })
  .then(async res => {
    const text = await res.text();
    let parsed; try { parsed = JSON.parse(text); } catch { parsed = text; }
    const out = script.extractor(parsed);
    process.stdout.write(JSON.stringify(out));
  })
  .catch(err => { console.error(String(err)); process.exit(1); })
  .finally(() => clearTimeout(timer));
```

无 Node 环境时：`custom_script` 整体降级为 `unsupported`（OpenCode Go 例外，走 §4.4 原生实现）。

**错误映射**：进程非零退出/超时 → `fetch_failed`；`isValid:false` → 以 `invalidMessage`
展示（不定 error code，UI 按"凭据/配置问题"样式）；JSON 解析异常 → `script_failed`。

### 4.3 token_plan 内置模板

路由：`codingPlanProvider` 字段为准；缺失时按 base_url 子串兜底
（`api.kimi.com/coding`→kimi，`bigmodel.cn`→zhipu，`api.z.ai`→zhipu，
`minimax`→minimax，`volces.com/api/coding`→volcengine；
参考 CC Switch `src/config/codingPlanProviders.ts`）。

#### 4.3.1 Kimi（M1 必做，已核实自 CC Switch `coding_plan.rs`）

```
GET https://api.kimi.com/coding/v1/usages
Authorization: Bearer {apiKey}
```

- 401/403 → `auth_failed`；非 2xx → `fetch_failed`；JSON 解析失败 → `parse_failed`
- 响应含多个窗口的用量百分比与重置时间；映射到 QuotaWindow（key 用窗口语义命名）
- 请求频率：≤ 每 5 分钟一次

#### 4.3.2 Zhipu GLM（M2，端点已核实）

```
GET {base}/api/monitor/usage/quota/limit
Authorization: {apiKey}          // 注意：智谱不加 Bearer 前缀（源码核实，勘误）
base = https://open.bigmodel.cn （供应商 base_url 含 bigmodel.cn）
       https://api.z.ai        （供应商 base_url 含 api.z.ai）
```

- 团队版（`codingPlanProvider == 'zhipu_team'`）：固定走国内站 `open.bigmodel.cn`，
  URL 加 `?type=2`，并需请求头 `bigmodel-organization` / `bigmodel-project`
  （值存于 `usage_script.teamOrganizationId` / `teamProjectId`，用户在 CC Switch
  用量脚本中填写；三者缺一即报错引导补全）
- 响应 `data.limits[]`：`type`（大小写不敏感）为 `TOKENS_LIMIT` 的条目即额度窗口；
  `unit == 3` → 5 小时，`unit == 6` → 每周（`percentage` 即已用百分比 0-100，
  `nextResetTime` 为毫秒时间戳）；兜底启发式：未分类条目按（无重置时间优先、
  重置时间升序）填入空槽；`data.level` 为套餐等级
- 业务错误：HTTP 2xx 但 `success == false` → 以其 `msg` 展示（`upstream_error`）

#### 4.3.3 MiniMax（M2，端点已核实）

```
GET https://{domain}/v1/api/openplatform/coding_plan/remains
Authorization: Bearer {apiKey}
domain 按供应商 base_url 区域判定：含 api.minimaxi.com → 国内站，否则国际站 api.minimax.io
```

响应外层有 `base_resp.status_code` 业务码，非 0 → 以其 `status_msg` 展示
（`upstream_error`）。额度取 `model_remains[]` 中 `model_name == "general"` 的条目：
- `current_interval_remaining_percent` 为**剩余**百分比 → 已用 = 100 − 剩余；`end_time`（毫秒）为重置
- 周桶仅 `current_weekly_status == 1` 时激活（无周限额套餐该字段为 3，不展示）；
  `current_weekly_remaining_percent` + `weekly_end_time`

#### 4.3.4 ZenMux（M2，端点已核实）

```
GET {baseUrl}        // 该供应商的 base_url 本身就是配额端点
Authorization: Bearer {apiKey}
```

- `success != true` → 以其 `message` 展示（`upstream_error`）
- `data.quota_5_hour` / `data.quota_7_day`：`usage_percentage`（0-1 小数，已用
  百分比 ×100）、`resets_at`（ISO 字符串）、`used_value_usd` / `max_value_usd`
  → 两个 QuotaWindow（含 used_text）
- `data.plan.tier` + `data.account_status` → 套餐信息（planLabel/extra）

#### 4.3.5 Volcengine 火山方舟（M2+，复杂）

AK/SK 签名鉴权（类 AWS SigV4），`GetAFPUsage` 等动作。M1/M2 标记 `unsupported`，
实现时移植 CC Switch `src-tauri/src/services/coding_plan.rs::query_volcengine`。

### 4.4 opencode_go 采集器（原生实现，已完成并验证）

> 参考实现：`../legacy/opencode-go-bridge/bridge.py`（16/16 测试通过，真实账号端到端跑通）。
> 挂件应将其逻辑内化为 collector，而非依赖独立服务。

| 项 | 规范 |
|---|---|
| 请求 | `GET https://opencode.ai/workspace/{workspace_id}/go`，Cookie：`auth={auth_cookie}; oc_locale=zh` |
| workspace_id | **必须 `wrk_` 前缀 ID**（地址栏跳转后的值），用工作区显示名称会被 302 到登录页 |
| 重定向 | 手动跟随 ≤5 次；终落在 `auth.opencode.ai` 或 `opencode.ai/auth*` 或含 `<title>OpenAuth</title>` → `cookie_expired` |
| 解析（主） | 内联 hydration：`rollingUsage`/`weeklyUsage`/`monthlyUsage` 键后的 `{...}` 块（允许 `$R[n]=` 前缀，跳过 `:null` 重复项），正则取 `usagePercent`、`resetInSec`、`status` |
| 解析（兜底） | DOM：3 个 `data-slot="usage-item"` 块（顺序固定 rolling→weekly→monthly），`data-slot="usage-value"` 或 `width:N%` 取百分比，`data-slot="reset-time"` 取重置原文 |
| 无订阅判定 | 存在 `data-slot="subscribe-button"` 且 `lite: null` 且 `liteSubscriptionID: null` → `no_subscription` |
| 输出 | 3 个 QuotaWindow：5h/weekly/monthly，`usedPercent=usagePercent`，`resetInSec` 直给 |
| 凭据来源 | 挂件自有 config（CC Switch 库中无此项）；M3 起内嵌 webview 一键重登：持久化 profile（opencode-login）打开 opencode.ai 登录页，cookieStore 自动捕获 `auth` cookie（仅采信登录后 opencode.ai 域上的值），urlChanged 自动识别 `/workspace/wrk_XXX/`；对话框内保留手动粘贴兜底 |
| 过期处理 | `cookie_expired` 时自动弹重登对话框（非模态，5 分钟冷却防刷屏）；保存后立即重试采集。cookie 失效属正常现象：opencode 已迁移官方 OAuth 会话，裸 `auth` cookie 会随会话过期/被 302 到登录页（独立项目 TokenTracker #225 同样踩坑；"默认浏览器 loopback 授权"不可行——`client_id=app` 为机密客户端、会话为 HttpOnly 域内 cookie，桌面端无法换取） |
| 频控 | 缓存 ≥5 分钟；失败时回退上次成功数据（`stale: true`） |

凭据获取指引（写给 UI 的帮助文案）：浏览器登录 opencode.ai → F12 → Application →
Cookies → `https://opencode.ai` → 复制 `auth` 的值；workspace_id 取地址栏跳转后
`/workspace/wrk_XXXX/` 段。

### 4.5 official_sub（M2 已实现）

`category == 'official'`（Claude/Codex/Gemini 官方登录）：复用各官方 CLI 的
**登录凭据文件**查询订阅额度（与 CC Switch `subscription.rs` 一致；只读、凭据
仅内存使用）：

| 工具 | 凭据文件 | 端点 |
|---|---|---|
| claude（含 claude-desktop） | `~/.claude/.credentials.json`（`claudeAiOauth`/`claude.ai_oauth` 键，含 accessToken/expiresAt） | `GET https://api.anthropic.com/api/oauth/usage`，Bearer + `anthropic-beta: oauth-2025-04-20`；顶层键即窗口（five_hour/seven_day/seven_day_opus/seven_day_sonnet 及未知键）`{utilization 0-100, resets_at}`；`extra_usage` 为超额用量 |
| codex | `~/.codex/auth.json`（仅 `auth_mode == "chatgpt"`；tokens.access_token + account_id） | `GET https://chatgpt.com/backend-api/wham/usage`，Bearer + `User-Agent: codex-cli` + `ChatGPT-Account-Id`（可选）；`rate_limit.primary/secondary_window{used_percent, limit_window_seconds, reset_at}`，窗口名 18000s→5h / 604800s→7天 / 2592000s→30天 |
| gemini | `~/.gemini/oauth_creds.json`（access_token + refresh_token + expiry_date ms） | 两步：`POST .../v1internal:loadCodeAssist` 取 project id → `POST .../v1internal:retrieveUserQuota` 取 `buckets[]`（modelId + remainingFraction 0-1 + resetTime），按 Pro/Flash/Flash Lite 分类、同类取最紧张；过期时用 refresh_token 调 Google OAuth 刷新（Gemini CLI 公开 client 凭据） |

- 错误映射：凭据文件缺失/解析失败 → `oauth_login_required`；API 401/403 或
  文件标记过期且 API 拒绝 → `oauth_login_required`（过期时仍先尝试 API，与
  CC Switch 一致）；非 2xx → `fetch_failed`；200 但无任何窗口 → `no_subscription`
- 未知 app 类型（如 opencode 官方）仍降级 `unsupported`，仅展示消耗

### 4.6 consumption_only

无 `usage_script` 或 `enabled == false`：不请求任何网络接口，仅展示 §3.5 消耗数据。

### 4.7 balance 原生余额模板（已实现，移植 CC Switch `balance.rs`）

`templateType == 'balance'`：无 JS code，按 **base_url 子串**识别供应商（与
CC Switch `detect_provider` 一致），走各供应商余额端点：

| 供应商 | 识别（base_url 含） | 端点 | 输出 |
|---|---|---|---|
| DeepSeek | `api.deepseek.com` | `GET https://api.deepseek.com/user/balance`（Bearer） | `balance_infos[]`（currency + total_balance）→ 每币种一个余额窗口；`is_available=false` → extra"余额不足" |
| StepFun | `api.stepfun.ai` / `.com` | `GET https://api.stepfun.com/v1/accounts` | `balance`（CNY） |
| SiliconFlow | `api.siliconflow.cn`（CN）/ `.com`（EN） | `GET https://{域}/v1/user/info` | `data.totalBalance`（CNY/USD） |
| OpenRouter | `openrouter.ai` | `GET https://openrouter.ai/api/v1/credits` | `total_credits - total_usage`，可算出已用百分比 |
| Novita AI | `api.novita.ai` | `GET https://api.novita.ai/v3/user/balance` | `availableBalance / 10000`（0.0001 USD 单位） |

- 余额类窗口 `usedPercent = null`：行内直接展示金额（`¥207.37`），无进度条百分比、
  无重置倒计时；多币种（DeepSeek）逐币种一窗
- 未识别供应商 → `unsupported`（"未识别的余额供应商"）；401/403 → `auth_failed`
- 自定义脚本返回 `remaining + unit`（无 total/percent）时同样归为余额窗口
  （custom_script 执行器已兼容）

---

## 5. 刷新调度器

| 规则 | 说明 |
|---|---|
| 基础间隔 | 每 plan 独立计时，取 `autoQueryInterval`（分钟；0 = 不自动刷），默认 5 |
| 近限升频 | 任一窗口 usedPercent ≥ 90% → 该 plan 间隔降为 1 分钟 |
| 失败退避 | 连续失败：间隔 ×2，上限 30 分钟；成功后恢复 |
| 单飞 | 同一 plan 并发只许一个在途请求；新 tick 到来时跳过 |
| DB 变更 | mtime 变化 → 重读 plan 清单，新增 plan 立即首刷，消失的 plan 移除 |
| 启动 | 启动即全量首刷（各 plan 错峰 0–3s 随机延迟，避免突发并发） |
| 手动刷新 | 托盘/按钮触发该 plan 立即刷新（不受间隔限制，但遵守单飞） |

---

## 6. UI/UX 规范

### 6.1 主窗

- 尺寸约 340×(80 + 44×行数)，无边框、圆角、半透明深色底、置顶（可切换）、拖拽移动、Esc/点外不关闭（收托盘靠托盘菜单或关闭按钮）
- 每行（44px）结构：`[图标] [名称(当前启用带●)] [额度条] [百分比] [重置倒计时]`
- 图标：vendored CC Switch 的 `src/icons/extracted/*.svg`（MIT，`token_widget/assets/icons/`），
  按 `providers.icon` 名加载；缺失时回退"首字母 + icon_color 圆底"
- 余额类（`usedPercent == null`）：无进度条语义，行内直接展示金额
  （如 `余额 CNY ¥207.37`），无百分比与重置倒计时
- 颜色：usedPercent <70 绿 / 70–89 橙 / ≥90 红（余额类同样按已用百分比）
- 多窗口 plan 行内轮换显示最紧张窗口，点击行展开全部窗口 + 当日按模型拆解消耗
  （`消耗：k2 $1.50 · 465 tokens；k1 $3.00 · 50 tokens`，按 cost 降序；无配额行的
  官方订阅/仅消耗同样可展开看拆解）
- 底部一行：`今日 $x.xx · N tokens`（ConsumptionSummary）
- 状态视觉：加载中（骨架）、stale（行标灰 + "更新于 HH:mm"）、错误（行尾 ⚠，tooltip 显示文案与处理建议 §8）、unsupported（仅消耗，小字标注）

### 6.2 Plan 勾选面板

- 从主窗按钮或托盘菜单打开；按 appType 分组列出全部 Plan（图标 + 名称 + category 徽标）
- 勾选即写挂件 config（§7），主窗即时增删行；默认全不选，首次启动引导勾选
- OpenCode Go 行若未配置凭据，行尾显示"设置凭据"入口（弹出内嵌 webview 登录，
  自动抓 auth cookie + workspace_id；"手动粘贴凭据…"为兜底）

### 6.3 托盘

菜单：`刷新全部` / `选择套餐…` / `暂停自动刷新`（切换） / `置顶`（切换） / `退出`。
托盘图标显示最紧张窗口的颜色点。

---

## 7. 挂件自有配置（唯一自有数据）

`config.json`（挂件目录下，不进 git；开发期 = 项目根，PyInstaller 打包后 = exe
同目录，`TOKEN_WIDGET_CONFIG` 环境变量可覆盖）：

```jsonc
{
  "selectedPlanIds": ["prov-id-1", "prov-id-2"],
  "window": { "x": 1200, "y": 60, "alwaysOnTop": true },
  "refresh": { "globalEnabled": true },
  "opencodeGo": {
    "accounts": { "main": { "auth_cookie": "...", "workspace_id": "wrk_..." } },
    "default": "main"
  }
}
```

要求：文件权限 600（Unix）/ 仅当前用户（Windows ACL 尽力）；含凭据字段，**禁止**随日志输出。

---

## 8. 错误分类与 UI 文案

| code | UI 文案（中文） | 建议动作 |
|---|---|---|
| `cookie_expired` | 凭据失效（或工作区 ID 有误） | 自动弹出一键重登（内嵌 webview 重新登录）；也可手动粘贴 |
| `no_subscription` | 该账号无此套餐 | 确认登录账号 |
| `auth_failed` | API Key 无效 | 去 CC Switch 检查该供应商 Key |
| `fetch_failed` | 网络异常，展示上次数据 | 自动恢复，无需操作 |
| `parse_failed` | 上游结构变化，等待适配更新 | 反馈 issue |
| `script_failed` | 用量脚本执行失败 | 在 CC Switch 里"测试脚本"核对 |
| `db_locked` | CC Switch 数据库暂不可读 | 自动重试 |
| `unsupported` | 暂不支持该类型的额度查询 | 展示消耗数据 |
| `upstream_error` | 上游业务返回错误 | 按返回信息处理；多为凭据或额度问题 |
| `oauth_login_required` | 官方账号未登录或凭据失效 | 用官方 CLI 重新登录（claude login / codex login / gemini login）后重试 |

---

## 9. 安全与隐私

1. CC Switch 库以只读打开；`settings_config` 中的 key 只在内存中用于构造请求，
   永不写盘、永不入日志（日志输出前对 `sk-*`、Bearer 值正则脱敏）
2. 挂件 config.json 含 OpenCode cookie，同 §7 权限要求
3. 全部网络请求仅发往各 plan 自身端点（与 CC Switch 行为一致），无任何遥测
4. 托盘/主窗不展示完整 key；凭据输入框用密码样式
5. 重登 webview 使用独立持久化 profile（`opencode-login`），登录态存于
   QtWebEngine 用户数据目录（与挂件 config 分离）；捕获的 auth cookie 写入
   config 时按 §7 权限要求处理

---

## 10. 测试规范

| 层 | 内容 | 资产 |
|---|---|---|
| 解析单测 | OpenCode Go 内联/DOM/无订阅/登录页四类金样本 | `../tests/fixtures/*.html`（现成，源自 legacy 参考实现） |
| 采集器契约 | 各模板 mock HTTP 响应 → QuotaResult 字段断言 | 按 §4 端点构造样例 JSON |
| 脚本执行器 | 内置样例脚本（含占位符替换、超时、isValid:false、数组返回） | runner.js 配套 fixture |
| Store 层 | 用本文件 §3 的表结构创建临时 SQLite（造 7 行 providers 样例） | 测试内联 DDL |
| 调度器 | 注入假时钟验证升频/退避/单飞 | — |
| DB 降级 | 缺表/缺列的 SQLite → 不崩溃、功能降级 | — |

---

## 11. 里程碑与验收标准

### M1（首版）

- [ ] 只读打开 cc-switch.db，列出 ≥ 实测 7 个供应商，勾选状态持久化
- [ ] OpenCode Go 行显示三档额度条 + 重置倒计时（真实账号验证）
- [ ] custom_script 类 plan 通过 Node runner 执行库中脚本并展示结果
- [ ] Kimi For Coding（token_plan）显示额度（真实 key 验证）
- [ ] 主窗/勾选面板/托盘/自动刷新/DB 变更跟随全部可用
- [ ] 单 plan 失败不影响其他行；stale 标灰显示

### M2

- [x] zhipu / minimax / zenmux 模板（2026-08-03 实现，契约测试 25 例）
- [x] 官方订阅（Claude/Codex/Gemini）OAuth 额度（2026-08-03 实现，复用官方
  CLI 凭据文件 + 配额接口，契约测试 25 例）
- [x] 行详情面板（点击行展开按模型拆解消耗，来自 rollups，2026-08-03 实现）

### M3

- [x] OpenCode Go 凭据过期一键重登（内嵌 QtWebEngine webview：登录后自动捕获
  auth cookie 与 workspace_id；仅采信登录后 opencode.ai 域 cookie；持久化
  profile 复用会话；手动粘贴兜底；cookie_expired 自动弹窗 + 5 分钟冷却）
- [x] PyInstaller 打包单 exe（`TokenWidget.spec` → `dist/TokenWidget.exe`，
  2026-08-03；frozen 下 config.json 在 exe 同目录；排除未用 Qt 模块压缩体积；
  `--smoke-webview` 验证 QtWebEngine 初始化）
- [ ] 历史趋势图 —— **暂缓**（2026-08-03 决策：rollups 只覆盖"本机被 CC Switch
  会话扫描到的用量"，非全量，易误导；若后续实现，UI 须标注数据范围脚注）
- [ ] 用量接近上限通知 —— **暂缓**（若后续实现，须默认关闭 + config 开关，
  不弹默认打扰）

### M4（2026-08-04，主题与体验）

- [x] 主题系统：`Theme` 数据类 + 7 主题（深色 `dark` / 浅色 `light` / 纸张 `paper`
  / 墨蓝 `midnight` / 北欧 `nord` / 纯黑 `oled` / 磨砂 `glass`）；托盘"主题"子菜单
  与主窗"设置"对话框（滑杆图标入口）双链路，单选即时预览、持久化
  （config `ui.theme`）、无需重启；分档色随主题（light/paper 加深、oled/glass 加亮、
  nord 低饱和）；作用域约定：主窗与自有对话框随主题，托盘菜单保持系统原生不换肤；
  预览工具 `verify_ui.py`：全主题 × 富场景（余额/分档/stale/错误/仅消耗/展开详情）
  合成 `build/theme_preview.png`（QWidget.render 取图，不走 offscreen 防 CJK 方框）
- [x] 磨砂玻璃：Win11 走 DWM system backdrop（`DWMWA_SYSTEMBACKDROP_TYPE`）+
  圆角偏好（`DWMWA_WINDOW_CORNER_PREFERENCE`），模糊层随窗口裁圆角；
  旧系统退回矩形 accent（四角直角为平台限制）；磨砂不可用时退回高不透明深色
- [x] 尺寸可变：解除固定宽，QSizeGrip 右下角拖拽，最小 300×160，
  `window.w/h` 持久化；副屏断开坐标自动钳回主屏
- [x] 矢量图标：标题栏/设置/状态角标弃用 Emoji，内置描边 SVG glyph
  （menu/refresh/sliders/pin/close/alert），描边色随主题；供应商
  `currentColor` 图标按主题前景色渲染（缓存键含主题）
- [x] 字号规范：主数据（百分比/余额）12px/600，辅助信息（窗口标签/重置/详情）10px
- [x] 文案统一：周窗口一律"每周"（原"7天"），月窗口"每月"（原"30天"）
- [x] OpenCode Go 登录加固：起始页固定首页（防一次性 OAuth code 重放卡死）、
  "回首页 / 清除登录态"自救按钮；cookie 捕获改为根域判定 + `loadAllCookies`
  全量扫描（覆盖持久化会话与回调跳转途中写入两种盲区）
- [x] 审查修复（2026-08-03 晚）：custom_script 密钥改 stdin 传输不落临时文件；
  DB 变更监听覆盖 `-wal`（WAL 模式）；UI 线程 DB 操作改 quick 模式
  （0.3s 短超时不重试）；余额/省略号裁切等显示缺陷修复

---

## 12. 附录

### 12.1 已验证的真实响应样例（OpenCode Go，2026-08-03）

```json
{
  "success": true, "reason": "",
  "data": "滚动 0% (5h) | 周 0% (6d13h) | 月 0% (28d1h)",
  "usage": {
    "rolling": {"percent": 0, "reset_in_sec": 18000, "status": "ok", "reset_in": "5h"},
    "weekly":  {"percent": 0, "reset_in_sec": 568159, "status": "ok", "reset_in": "6d13h"},
    "monthly": {"percent": 0, "reset_in_sec": 2423671, "status": "ok", "reset_in": "28d1h"}
  }
}
```

### 12.2 参考实现与源码指针

| 资产 | 位置 |
|---|---|
| OpenCode Go 采集器参考实现（含测试、fixtures） | `../legacy/opencode-go-bridge/bridge.py`、`test_bridge.py`、`fixtures/` |
| CC Switch 脚本执行协议 | `farion1231/cc-switch: src-tauri/src/usage_script.rs`、`src-tauri/src/services/provider/usage.rs` |
| token_plan 模板端点 | 同仓库 `src-tauri/src/services/coding_plan.rs` |
| 原生余额模板 | 同仓库 `src-tauri/src/services/balance.rs` |
| 供应商图标资产 | 同仓库 `src/icons/extracted/`（vendored 到本仓库 `token_widget/assets/icons/`） |
| 模板路由表（base_url 匹配） | 同仓库 `src/config/codingPlanProviders.ts` |
| 用户手册（用量查询语义） | 同仓库 `docs/user-manual/zh/2-providers/2.5-usage-query.md` |

### 12.3 实测环境快照（2026-08-03，实现者的开发基准）

- CC Switch 库：`C:\Users\曾梓行\.cc-switch\cc-switch.db`，16 张表（§3 所列）
- 已配置供应商 7 个：Claude Official / Claude Desktop Official / Kimi For Coding（codex，token_plan）/
  OpenCode Go（codex，custom 脚本指向桥接）/ OpenAI Official（codex）/ 阿里云百炼（gemini）/ Google Official
- usage_daily_rollups 有 2026-06 起的数据，`_session`/`_codex_session` 为会话导入来源
