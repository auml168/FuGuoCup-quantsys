# 富国杯基金量化研究与决策支持

面向第六届富国杯：**2026-10-19开始，2027-01-15半程截止，2027-04-30结束，初始虚拟资金200000元**。交易由本人在比赛平台手动执行；系统没有比赛账户登录或下单接口。

## 状态与边界

本次量化重构保存在独立分支 `review/quant-research-upgrade`；迁移准备分支为 `migration/auml168`，目标仓库为 `auml168/FuGuoCup-quantsys`。迁移分支不直接覆盖已有主分支。完整审查、实测和限制见 [审查报告](docs/REVIEW.md)。

旧账号不可用，当前按用户要求迁至 `auml168`；新账号与仓库写入权限已核验，完整工程通过迁移分支和PR交付。定时任务、Secrets及Telegram仍须在新仓库单独验收。迁移步骤见[迁移说明](docs/MIGRATION.md)，旧账号阶段补丁见[交付说明](docs/DELIVERY.md)。本地通过不等于GitHub定时推送已经验收。

- 自动发现公司页关联目录，逐产品核验详情中的管理人；生产候选池不再由固定8只名单控制。
- 保留不支持、净值不足、采集异常的记录。销售目录、销售平台风险描述和申购状态**不等于比赛白名单、官方适当性等级或比赛专用规则**。
- 保留六个基准，新增多周期双动量、波动风险预算与相关性过滤、固定权重多策略组合。研究方案、源代码哈希、配置及时间窗写入manifest，不搜索参数。
- 执行连续持仓的滚动历史验证，比较收益、年化、回撤、夏普、双边换手、申赎费用和市场状态表现。已查看的2026年历史测试不再宣称“未见样本”。
- **新增候选尚未取得足够晋级证据，未替换原等权基准，也未批准该基准用于实盘。** 没有数据/账户/规则/模型证据时不给买卖金额。
- 只有14:20决策时段才可能产生可执行建议；08:30盘前和20:30盘后只作观察。全部建议仍须人工核对。
- 已核验2026年官方日历。系统可在官方发布完整2027年安排后自动解析扩展；目前未取得完整2027年官方公告，保留未知日期拦截，不填入猜测日期。

## 本地运行

需要Python3.11—3.13，依赖仅numpy和pandas。根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
python -m unittest discover -s tests -v
fq doctor --limit 12
fq collect
fq research
fq report --research
```

`doctor`在 `data/smoke/` 中做有界真实接口验收，不覆盖完整池。`collect`默认尝试全部受支持的目录产品，最多4个并发请求；`--limit`仅供抽样，结果明确标为不完整，不能开启正式模式。生产没有固定8只上限。

`research_funds`现在仅保留原8只的基准回归信息；人工修订产品分组和行业可使用 `product_overrides`。底层产品分组默认由名称末尾A/C等份额类别推导，并明确标为启发式识别，不能证明持仓穿透后无重合。

```bash
fq discover
fq --data data/smoke research
fq --data data/smoke report --research
fq --data data/expanded backtest --baseline-eight --out reports/eight-benchmark
fq calendar-check --year 2027
```

最后一个命令若尚无完整官方公告，显示待核验，保留当前覆盖期限。禁止把“待核验”解释成2027年已验收。

## 数据与研究产物

|文件|内容|
|---|---|
|`data/catalog.json`|目录关联记录及支持/不支持原因|
|`data/universe.json`|类别、份额组、风险描述来源、净值长度、采集结果|
|`data/nav/代码.csv`|真实单位净值、供应商日收益率、复权总收益指数和事件|
|`data/snapshot.json` / `data/raw/`|本次数据范围、时间、原始来源、SHA256及错误|
|`reports/research/manifest.json`|冻结研究参数、代码哈希和时间窗|
|`reports/research/comparison.json` / `.md`|滚动窗口、最终历史测试、费用、换手、候选晋级结论|
|`reports/research/*-test-equity.csv`|逐日资产、现金、应收款、当时可观测市场状态|
|`reports/latest.json` / `.txt`|当前账户报告；公开工作流不上传此账户报告|

读取CSV会检查快照哈希。无法解释的收益区间标为`quality_break`，不回溯删除断点前的历史；历史选基只能使用当时已经出现的断点，重新积累足够连续观测后才恢复资格。持仓跨过断点、净值长期缺失时，该段回测标记无法验证，指标保留为空，不能用零收益或拼接收益代替。

旧版研究快照可用`python scripts/rebuild_snapshot.py --data data/expanded`从已归档且哈希匹配的原始响应离线重建。此命令不刷新采集时间、不延长旧快照的数据末日，也不能替代正式决策前的在线采集。

现有目录仍有幸存者偏差，历史修订净值也缺少完整逐时发布时间。T-2信号只缓解发布延迟，不能消除这些数据局限。历史交易日用国内权益参考净值日历代理，当前/未来操作则必须通过官方日历。详见 [方法说明](docs/METHODOLOGY.md)。

## Telegram与GitHub Actions

新仓库创建并授权后，在 [目标仓库Secrets](https://github.com/auml168/FuGuoCup-quantsys/settings/secrets/actions) 重新保存：

- `TELEGRAM_BOT_TOKEN`：BotFather生成的有效Token；**不要发到聊天、代码或日志**。
- `TELEGRAM_CHAT_ID`：先给机器人发送 `/start`，再配置自己的聊天/群组ID。
- `ACCOUNT_JSON`：完整实际模拟账户，避免公开提交持仓。

如Token曾公开，应先在BotFather撤销重建。程序只从环境变量读取Token，不记录带Token的请求URL。可用 `Telegram connection test` 手动发送连接测试；此项仅确认通信，不证明比赛规则或实际持仓已核验。

新建仓库不会自动取得旧仓库的Secrets、Actions历史或通知预约状态。不要用旧账号的连接结果冒充新账号授权，也不要从旧日志提取凭证。

GitHub Actions请求在北京时间 **08:30 / 14:20 / 20:30** 启动，非交易日跳过。本地电脑无需开机。Actions排队可能延迟，不保证精确到秒；过时交易报告会抑制。完整步骤和故障处理见 [部署说明](docs/DEPLOYMENT.md)。

云端先恢复可信分支的持久化发送状态，生成预约，**上传预约artifact成功后才调用Telegram**，最后保存发送结果。超时或崩溃后的预约不盲目重发；恢复失败时停止发送。工作流日志和公开artifact不包含账户报告、outbox或凭证。通知状态只保存哈希和ID，保留上一份可执行计划的核对状态。

## 实际账户与操作资格

以比赛平台为账本。建议不是成交，系统不自动扣现金或写回份额。格式示例只是字段说明：

```json
{
  "as_of": "2026-10-19",
  "reconciled_at": "2026-10-19T14:05:00+08:00",
  "cash": 200000,
  "lots": [],
  "pending": [],
  "acknowledged_plan_id": null
}
```

`lots`逐批填 `code`、真实已确认 `units`、`confirmed_on`。`cash`仅是平台可用现金；在途资金已从cash/lots移出，用 `pending` 的 `type: buy/sell`、`amount`、`settle_on`单独记录，避免重复计入。存在在途项目时暂停金额，待平台确认后重新核对。

正式决策要求当日 `as_of`、4小时内的带时区 `reconciled_at`。已发出的可执行计划必须核对；实际成交后更新账户，未执行则在核对后用 `acknowledged_plan_id`明确确认上一份计划，不能只改时间戳来重复生成下单建议。

```bash
fq account validate --file config/account.local.json
fq account import --source config/account.local.json
fq report --research --account config/account.local.json
```

默认账户是用户提供的开赛前虚拟空仓，不代表已连接平台验证。不要将真实账户文件或备份提交公开仓库。

## 正式模式不能靠开关放行

需要准确的比赛 `allowed_codes` 和来源、风险限制、截止口径、逐只实际费率、申购确认、到账天数、最短持有期、申购限额等。`fund_rules`缺少实际交易字段时，不能继承研究默认费率后仅设置 `verified=true`。

```bash
fq validate
fq collect --competition
```

这两条通过也不等于可实盘：还须数据时效、持仓同步、持久化状态、模型部署证据和14:20执行窗口全部通过。研究模式和基准模型只显示目标仓位，不提供可直接执行的金额。系统不保证收益或比赛排名。

## AI评估

本次不强制接入LLM。解释报告可以用确定性数据完成，尚无带历史发布时间的新闻/政策样本证明LLM能带来净收益改善。未来若接入，只处理带来源的辅助材料，不参与最终仓位和金额计算，见 [AI评估](docs/AI_ASSESSMENT.md)。

## 项目结构与验收

`src/fullgoal_quant/`保存数据、模型、账务、报告和通知逻辑；`tests/`覆盖时序、费用、缺失数据、重复份额、账户和发送状态；`.github/workflows/`负责回归测试、真实接口抽样和生产报告。`sample_results/`保留原系统历史记录，`sample_results/review/`是本次审查的可复现证据，不能把旧报告当成当前指令。

[完整审查与验收](docs/REVIEW.md) · [研究方法](docs/METHODOLOGY.md) · [部署说明](docs/DEPLOYMENT.md)
