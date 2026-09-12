# OpenCode Go 用量桥接服务（方案 A）

让 CC Switch 显示 OpenCode Go 套餐的三档剩余额度（5 小时 / 每周 / 每月）。

OpenCode 官方**没有**额度查询 API，用量只展示在 opencode.ai 控制台网页。
本服务用浏览器会话 cookie 抓取工作区页面（SSR HTML），解析出三档用量百分比，
包装成本地 JSON API，供 CC Switch「自定义用量脚本」调用。

- 零第三方依赖：Python 3.10+ 标准库，Windows 直接运行
- 解析逻辑参考 MIT 项目 [andywang425/opencode-go-usage-api](https://github.com/andywang425/opencode-go-usage-api)，特此致谢
- 默认只监听 `127.0.0.1`，凭据不出本机

## 文件清单

| 文件 | 说明 |
|---|---|
| `bridge.py` | 桥接服务本体（抓取 + 解析 + 缓存 + HTTP API） |
| `config.example.json` | 配置模板，复制为 `config.json` 后填写 |
| `cc-switch-script.js` | CC Switch 自定义用量脚本（复制粘贴用） |
| `test_bridge.py` | 单元测试（16 个用例，`python test_bridge.py`） |
| `fixtures/` | 测试用的页面样本 |

## 第一步：获取 cookie 和 workspace id

1. 浏览器登录 [opencode.ai](https://opencode.ai)，进入你的工作区
2. 注意地址栏：如果你的访问路径是 `/workspace/<名称>/...`，页面通常会
   **自动跳转到 `https://opencode.ai/workspace/wrk_XXXX/...`** ——
   必须取跳转后 URL 里的 **`wrk_XXXX`** 作为 workspace_id，
   **不要**填工作区的显示名称（填名称会被重定向到登录页，报 `cookie_expired`）
3. 按 `F12` 打开开发者工具 →「应用 / Application」→「Cookie」→ `https://opencode.ai`
4. 找到名为 **`auth`** 的 cookie，复制它的**值**（一长串字母数字，不带 `auth=` 前缀）

## 第二步：配置并启动

```bash
# 在 opencode-go-bridge 目录下
cp config.example.json config.json
# 编辑 config.json，填入 auth_cookie 和 workspace_id
python bridge.py
```

启动后验证：

```bash
curl http://127.0.0.1:18443/health
curl http://127.0.0.1:18443/usage
```

成功的返回示例：

```json
{
  "success": true,
  "reason": "",
  "data": "滚动 12% (3h12m) | 周 45% (2d4h) | 月 67% (12d)",
  "usage": {
    "rolling": { "percent": 12, "reset_in_sec": 11520, "reset_in": "3h12m", ... },
    "weekly":  { ... },
    "monthly": { ... }
  },
  "cached": false
}
```

## 第三步：配置 CC Switch

1. CC Switch → 找到 OpenCode Go 供应商卡片 → 悬停 → 点击「用量查询」（📊 图标）
2. 打开「启用用量查询」开关，模板选择「**自定义**」
3. 把 `cc-switch-script.js` 的全部内容粘贴进脚本框
4. 点击「测试脚本」—— 应显示 `OpenCode Go` 套餐和剩余百分比
5. 保存。「自动查询间隔」建议 ≥ 30 分钟（见下方注意事项）

脚本默认把 **5 小时窗口的剩余百分比**作为卡片主数字，三档完整明细放在 `extra` 里。
想让主数字改成周 / 月窗口，把脚本里的 `rolling` 换成 `weekly` / `monthly` 即可。

## cookie 过期了怎么办

返回 `cookie_expired` 时，重新执行「第一步」拿到新 cookie，然后二选一：

```bash
# 方式一：直接改 config.json 后重启服务

# 方式二：不重启，运行时更新
curl -X POST http://127.0.0.1:18443/config \
  -H "Content-Type: application/json" \
  -d "{\"account\": \"main\", \"auth_cookie\": \"新的cookie值\"}"
```

## 返回状态说明

| reason | 含义 | 处理 |
|---|---|---|
| （空，success=true） | 正常 | — |
| `cookie_expired` | 被重定向到登录页 | 先检查 workspace_id 是否为 `wrk_` 前缀的 ID（不是工作区名称），再更新 cookie |
| `no_subscription` | 账号无 Go 订阅 | 确认登录的是已订阅账号 |
| `parse_empty` | 页面结构变化，解析失败 | 页面改版了，需要更新解析逻辑 |
| `fetch_error` | 网络超时 / 上游异常 | 有缓存时自动回退旧数据（`stale: true`） |

## 注意事项（务必读）

- **频率克制**：这是非官方抓取路径，高频请求有触发风控的风险。服务默认缓存
  5 分钟（`cache_ttl`），CC Switch 自动查询间隔建议设 ≥ 30 分钟
- **页面结构可能变**：opencode.ai 改版会导致解析失效（报 `parse_empty`），
  需要同步更新 `bridge.py` 里的解析正则
- **安全**：`config.json` 含有会话凭据，不要提交到 git、不要分享给他人；
  服务如改为监听非本机地址，请务必设置 `server.api_token`
- **多账号**：`accounts` 里可加多个账号，`GET /usage/<账号名>` 分别查询

## 测试

```bash
python test_bridge.py   # 16 个用例：解析 / 格式化 / 缓存 / 鉴权 / 过期处理
```
