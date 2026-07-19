# M25 Web Runtime 部署边界

状态：已实施。冻结来源为协调仓库 `M25_WEB_RUNTIME_CONTRACT.md@3e4fdf6`。

## 目标与范围

- 由可信调用方通过 `RuntimePolicy.web()` 选择服务器 Web profile，模型和单次请求不能修改。
- 工具注册、权限默认值和能力快照共用同一 policy，不建立第二套 Runtime composition。
- Web profile 只暴露 `web_search`、受信 Skill、能力自省、用户澄清和受控图表展示。
- 服务器文件、Git、Shell、后台进程、扩展管理、通用 URL 抓取和任意 MCP 工具不注册。
- CLI profile 保留完整工具目录和原有交互审批语义。
- Interaction 增加权威 `expires_at`，pause/cancel 可立即解除有界等待并安全拒绝。
- 不修改 `agent/loop.py`，不修改 API/Web，不增加通用文件导出。

## 安全决策

Web 工具安全依赖注册白名单，而不是提示词或浏览器隐藏。Registry 对未注册名称返回
`unknown_tool`，因此模型无法通过猜测工具名绕过。白名单工具的自动允许由冻结
`RuntimePolicy.auto_allow_tools` 控制；配置中的显式 deny 仍可继续收紧。

`fetch_url` 暂不进入 Web profile。现有 URL 层能拒绝 localhost、非公网地址、metadata 地址、
混合 DNS 结果和每跳私网重定向，但尚未把校验后的 DNS 地址绑定到实际 HTTP 连接，无法证明抵御
DNS rebinding。首版按 fail closed 只自动开放搜索 backend。

文件输出当前仅有 M24 受控 Chart Artifact。未来通用 Export Store 必须生成 opaque ID、安全文件名、
媒体类型、大小和不可变 hash；模型不得提供服务器路径。该边界本轮只冻结，不扩大实现范围。

## 公共契约

- `RuntimePolicy.web()`：固定 Web 部署上限。
- `RuntimeCapabilities.profile`：新增 `cli/service/web/custom` 安全事实。
- `InteractionRequestBase.expires_at`：Blocking port 入队时生成 UTC RFC 3339 截止时间。
- 五类 Interaction 保留各自类型，决策选项统一由 `legal_options` 表达；question 另保留回答候选
  `options`。
- `BlockingInteractionPort.interrupt_pending()`：解除当前等待但不关闭 Runtime，晚到响应拒绝。

`EVENT_CONTRACT_VERSION` 保持 1，Run checkpoint 保持 v4，StepEvent、final/run_terminal 顺序不变。

## 验收

- strict 权限配置下 Web `web_search` 直接执行且不产生 approval。
- Web capabilities 和 Registry 均不存在危险工具及 `fetch_url`。
- CLI 同一网络工具仍产生 approval。
- profile 为 frozen policy，且不出现在工具 schema 参数中。
- timeout/interrupt/close/晚到响应全部 fail closed；pause/cancel 解除等待。
- URL 策略覆盖 localhost、私网、链路本地、metadata、IPv6 和重定向目标复检。
- Ruff、mypy、import-linter、pytest/coverage、scripted/recovery eval 全绿。
