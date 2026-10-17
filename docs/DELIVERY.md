# 最终本地交付与远端同步

> 迁移更新：用户已指定改用`auml168`。本页记录旧账号阶段的提交和补丁起点，旧PR不再作为当前部署目标；新账号连接及完整源代码重建以[迁移说明](MIGRATION.md)为准。

本地最终版本已完成收益断点的历史时点隔离、无效回测指标为空、报告阻断、元数据复制优化和离线迁移测试。完整验收见[审查报告](REVIEW.md)。不能把研究程序能够运行解释为模型已有盈利证据，或比赛账户和规则已核验。

## 版本对应

- 主分支基线：`55faa32c04b360899a3474f5c6ddaab18d7ba1b5`，本次没有覆盖主分支。
- 已创建的[PR #1](https://github.com/auml7896/FuGuoCup-quantsys/pull/1)：分支`review/quant-research-upgrade`，首次上传提交`6a600b42e8a4d7e34c696c552444c9cd9b2084de`，树`de5aa2b8c8016b0afe975ac1aa94da7bd544d553`。
- 本地最终代码和真实研究结果比上述首次上传版本更新。远端写入仍受限，最新补充尚未同步，不能把PR状态当成最终版本的云端验收。

## 交付文件

与项目目录并列的`FuGuoCup-quantsys-review.zip`包含最终源代码、配置、测试、文档和研究证据，不包含凭证、真实账户、数据缓存或Git目录。`FuGuoCup-quantsys-review-manifest.json`逐文件记录SHA256。

`FuGuoCup-quantsys-review.patch`是针对主分支基线的完整修改；`FuGuoCup-quantsys-followup.patch`只包含首次上传PR之后的补充。**两份补丁用于不同起点，不要叠加使用。**

原始公开数据留在本地研究目录，源URL、采集时间和哈希可从研究证据追溯。交付包不携带大型历史缓存，安装后需重新采集；离线迁移不能刷新数据时效。

本地Git提交从主分支基线保留完整修改，不能直接强推到已有PR。远端同步使用下面的补充补丁方式；两种补丁已分别验证能重建同一最终文件树。

## 恢复远端访问之后

首先检查仓库默认分支和PR没有被他人更新。若PR仍准确位于上述首次上传提交，可在该分支使用补充补丁，保留已有历史：

```bash
git switch review/quant-research-upgrade
git rev-parse HEAD
# 仅在输出仍为上述首次上传提交，且工作目录无自己的未提交修改时继续。
git apply --check --index /下载目录/FuGuoCup-quantsys-followup.patch
git apply --index /下载目录/FuGuoCup-quantsys-followup.patch
git commit -m "Complete causal data-quality checks and final research validation"
git push origin review/quant-research-upgrade
```

若检查不通过，应比较新增修改并合并，不能强制覆盖远端。不要向main直接推送完整补丁，也不要用强制推送抹掉PR历史。

随后查看PR的回归测试、真实公开数据抽样，手动运行Telegram连接测试，再审查合并。合并后先运行不发送的研究任务；确认Secrets和报告有效后再启用发送。云端验证完成之前，不能承诺2026-10-19定时报告已经就绪。

比赛准入、逐产品真实规则、当日已核对的账户、模型部署证据仍需补齐。2027完整官方日历尚未核验。程序保留这些安全检查，不以启用开关代替证据。
