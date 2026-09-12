# Token 桌面挂件（ControlSystem Part 1）

镜像 [CC Switch](https://github.com/farion1231/cc-switch) 已配置套餐额度的只读桌面挂件：
勾选要盯的套餐，一眼看到剩余额度、重置倒计时、账户余额与当日消耗。
完整设计见 [`desktop-widget/DESIGN.md`](desktop-widget/DESIGN.md)。

## 功能

- **套餐额度**：OpenCode Go（原生抓取）、Kimi / 智谱（含团队版）/ MiniMax / ZenMux
  token_plan 模板、官方订阅（Claude / Codex / Gemini 复用官方 CLI 登录凭据）、
  DeepSeek / StepFun / SiliconFlow / OpenRouter / Novita 原生余额、CC Switch 自定义用量脚本
- **当日消耗**：按套餐与模型拆解（来自 cc-switch.db `usage_daily_rollups`，只读）
- **凭据一键重登**：OpenCode Go cookie 过期时内嵌 webview 自动重新登录并捕获凭据
- 托盘、自动刷新（近限升频 / 失败退避 / 单飞）、DB 变更跟随、CC Switch 同款图标、
  7 套主题（深色 / 浅色 / 纸张 / 墨蓝 / 北欧 / 纯黑 OLED / 磨砂玻璃，托盘或设置中切换）

## 开发运行

```bash
uv sync
uv run python main.py            # 启动挂件
uv run python main.py --smoke    # 冒烟：2 秒后自动退出
uv run python main.py --smoke-webview  # 冒烟：验证 QtWebEngine 登录对话框
uv run python verify_ui.py       # 主题预览：全主题富场景合成到 build/theme_preview.png
uv run pytest                    # 运行测试
```

挂件自有配置 `config.json`（含 OpenCode cookie，不入库）默认在项目根（开发期）/
exe 同目录（打包后）；`TOKEN_WIDGET_CONFIG` 环境变量可覆盖路径。

## 打包单 exe

```bash
uv run pyinstaller --clean --noconfirm TokenWidget.spec
```

产物：`dist/TokenWidget.exe`（单文件，约 200MB；含 QtWebEngine 运行时）。

- 应用图标源为 `token_widget/assets/app.svg`（方案 A「微缩挂件」，多尺寸 ICO 已提交）；
  调整设计后运行 `uv run python tools/make_icon.py A` 重新生成 `app.ico`，
  候选方案预览在 `build/icon_preview.png`
- 首次运行会在 exe 同目录自动创建 `config.json`
- 启动后以窗口程序运行，无控制台；`--smoke` / `--smoke-webview` 参数仍可用
- 冒烟验证命令：
  `Start-Process .\dist\TokenWidget.exe -ArgumentList "--smoke" -Wait -PassThru | Select ExitCode`
- 未签名 exe 可能触发 SmartScreen 提示（"仍要运行"）
- 自定义用量脚本依赖系统 Node.js（无 Node 时该类型降级 unsupported）

## 测试

`uv run pytest`（173 个用例）：Store 层、调度器、各采集器契约（mock HTTP）、
余额模板、官方订阅凭据解析、挂件配置、webview 捕获纯函数、UI 组件（offscreen）。

## 许可证

本项目原创代码采用 [MIT License](LICENSE)，Copyright (c) 2026 WindFromKadath。

第三方依赖与资产仍适用其各自的许可证。`token_widget/assets/icons/` 中来自 CC Switch 的图标保留上游 MIT 授权，见 [第三方声明](THIRD_PARTY_NOTICES.md)。相关品牌名称、商标与标志的权利归各自权利人所有。
