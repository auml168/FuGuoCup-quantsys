# 午后投递调度

## 根因与时间线

已核验的运行时间如下（均为北京时间，2026 年）：

| 原 cron 时点 | 实际运行时间 |
|---|---|
| 08:30 | 10-19 13:06、10-20 13:08、10-21 13:09 |
| 14:20 | 10-19 21:40（两次运行，相隔 3 秒）、10-20 19:51、10-21 20:02 |
| 20:30 | 10-20 02:46、10-21 01:24 |

每次运行创建后，runner 都在 3–4 秒内接单；延迟发生在运行创建之前，即 GitHub schedule 事件生成阶段。每次 14:20 任务均命中“工作流延迟超出有效时段”→ 无 outbox → cloud_send 输出“No new eligible report to send.”→ success，failure alert 从未执行。手动 dispatch 使用 slot=manual（slot_guard 始终为真）及 --force --test-send，因此从未验证生产调度路径。

## 架构

外部定时器在工作日 13:50、14:05（Asia/Shanghai）发起 scheduled dispatch；GitHub 自带 13:50、14:20 cron 作为备用。所有模式保留同一个 concurrency group 串行运行。生产模式忽略 research 输入，由 CLI 按比赛配置决定是否强制研究。

外部 workflow_dispatch 绕过了已观测到的 schedule 事件生成瓶颈，但不保证准时送达：GitHub runner 排队或服务故障仍可能发生，因此保留独立 healthchecks 监控。

在外部触发器配置完成前，每个交易日迟到的 GitHub cron 现在都会以 missed_late 明确失败并发送 Telegram 告警（可能两次），不再静默成功；这是预期行为。跨日遗留 cron 若早于今日允许启动时间，则以 skipped_stale_trigger 跳过，不发告警或心跳。

日历检查 → 当日去重预检 → 启动窗口（13:40–14:55）→ 采集/评估 → 等待 14:20 → 报告 → 预约上传 → Telegram → 完成状态上传。报告生成、预约和发送均受 14:20–14:55 窗口约束；迟到且未发送的任务失败退出。reserved/send_failed 必须人工核对 Telegram，禁止自动重发。

状态 artifact 分页检索最多 10 页，下载最新最多 6 份候选，按 generation、created_at、final 优先级恢复。无 state-restored 标记禁止生产发送。FQ_ALLOW_STATE_BOOTSTRAP=true 只允许研究初始化，不伪造恢复标记；首次运行可先用 test-send 初始化并持久化状态，preview 仅预览。已有系统状态缺失时应恢复可信备份，不能以空状态替代。

## 外部触发（二选一，本仓库只提供文件，未部署）

创建 fine-grained PAT，仅授权本仓库，Repository permissions 的 **Actions: Read and write**。令牌仅存服务密钥设置，不能提交或放入 URL。

- **Cloudflare Workers**：复制 deploy/cloudflare-worker/wrangler.toml.example 为 wrangler.toml，设置 GH_REPO；用 `wrangler secret put GH_DISPATCH_TOKEN` 设置 PAT，再部署 Worker。Cron Triggers 为 UTC `50 5 * * 1-5` 和 `5 6 * * 1-5`。只有 scheduled handler，没有公开 HTTP 触发入口；非 204 会抛错供日志告警。
- **cron-job.org**：建立两个任务，时区 Asia/Shanghai，周一至周五 13:50、14:05，方法 POST，URL `https://api.github.com/repos/OWNER/REPO/actions/workflows/reports.yml/dispatches`。请求体 `{"ref":"main","inputs":{"mode":"scheduled"}}`；请求头 `Authorization: Bearer <PAT>`、`Accept: application/vnd.github+json`、`Content-Type: application/json`、`User-Agent: FullgoalQuant-Scheduler`。预期响应 204，配置服务自身失败通知。

## 独立漏发监控

healthchecks.io 新建 Cron 检查：`50 13 * * 1-5`，时区 Asia/Shanghai，Grace time 65 分钟（截止 14:55），接入 Telegram integration。假日跳过（约 13:52）与正常发送（约 14:21）的 ping 均在 13:50 之后，因此都能满足当天的检查周期。

将 ping URL 存入 GitHub secret HEALTHCHECK_PING_URL。只有 FQ_JOB_STATUS=success 且生产结果为 sent/skipped_non_session/skipped_already_sent 才发成功心跳，其余发 `/fail`；skipped_stale_trigger 完全不发送心跳。请求超时 10 秒，不影响作业结果；未配置或非生产运行时跳过。即使 GitHub 整个任务没有启动，独立检查仍会超时告警。

## 状态

| 状态 | 含义 / 退出结果 |
|---|---|
| scheduled | 生产运行开始 |
| skipped_non_session | 非交易日，成功跳过 |
| skipped_already_sent | 当日已经发送，成功跳过 |
| skipped_stale_trigger | GitHub cron 早于今日允许启动时点，视为跨日遗留事件；退出 0，不发告警或心跳 |
| missed_late | 迟到且未发送，退出 3 |
| failed_too_early | scheduled dispatch 早于允许启动时点，退出 2；检查外部调度配置 |
| blocked_unresolved | 预约或失败状态未核清，退出 2；人工查 Telegram |
| reserved | 已预约，须先持久化再发送 |
| sent / test_sent | 正式 / 测试发送完成；测试不更新 last_signal |
| send_failed | 发送失败，退出 1；只保存 http/network/other 类别 |
| failed | 去重、配置或生成失败；生产运行退出 2 |
| preview | 手动预览，不发送 |

状态写入 runtime/run-status.json、日志 FQ_STATUS 和 Actions step summary，不包含账户金额。发送后其他步骤失败时保留“已送达”告警抑制。

## 下一个交易日验证

1. 确认 main 已包含变更，历史状态可恢复，外部触发器已配置且返回 204。
2. 13:50 查看生产 dispatch：scheduled、阶段耗时、等待日志；报告 created_at 不早于 14:20。
3. 确认 reserved artifact 先上传，再出现 sent，Telegram 仅收到一次；final 状态 generation 增加。
4. 检查后续备用 run 为 skipped_already_sent，无第二次发送；healthchecks 检查成功。
5. 若为 blocked_unresolved/send_failed，先人工核对 Telegram 与预约 owner，保留证据；不要自动清除预约或重发。若工作流未启动，确认独立监控最迟在宽限期结束后报警。
