# P0 热梗候选离线发现

这条流水线每天从可访问的 P0 来源抓取一小批公开文本，做脱敏标准化和重复表达聚合，最后只生成本地 `pending` 待审文件。它不修改 `resources/releases/`，也不会把任何候选自动接入 Planner 或 Replyer。

## 当前来源边界

| 来源 | 当前能力 | 说明 |
| --- | --- | --- |
| B 站热门视频 | 热门列表、首批公开评论、有限分段弹幕 | 使用公开 Web 端接口，低频、有上限；接口并非稳定 Open API，失效时必须显式报错或停用 |
| 斗鱼 6657 / 9999 / 71415 | 房间页面可达性 | 没有正式弹幕授权时不连接私有协议，不生成聊天证据 |
| 虎牙 10188 | 房间页面可达性 | 没有正式弹幕授权时不连接私有协议，不生成聊天证据 |
| 授权直播导出 | 本地 JSONL inbox | 供平台正式能力、主播授权工具或人工导出的弹幕进入同一流水线 |

公开页面能访问不等于允许无限量抓取和长期保存。默认只保存消息文本、消息 ID、内容/房间 ID、观察时间和圈层；评论者/观众昵称、用户 ID、头像等个人字段不会进入标准化证据，正文中的显式 `@昵称` 会被替换为 `@用户`。运行范围通过配置中的视频数、分段数、评论数和请求间隔限制。`out/` 不应同步到公共仓库，并应由运营方按实际授权设置短期保留和定期清理策略。

## 运行一次

```bash
cp ops/p0_discovery.example.json ops/p0_discovery.local.json
python3 scripts/run_p0_discovery.py --config ops/p0_discovery.local.json
```

输出在被 Git 忽略的 `out/p0-meme-discovery/`：

```text
latest-run.json
evidence-store.jsonl
inbox/
runs/<UTC 时间>/
  evidence.jsonl             # 本次首次看到的增量证据
  source-report.json
  candidates.pending-review.json
  review-queue.html
```

`source-report.json` 会区分真实拿到文本、仅房间元数据和错误，不把降级误报成采集成功。消息 ID 生成幂等键，同一条弹幕不会在后续运行中重复写入；候选从默认 14 天的 `evidence-store.jsonl` 滚动窗口生成，过期证据会从这个本地滚动仓移除。每条证据会写入 `retention_deadline` 和来源政策备注。

每个候选还会生成 `observed_usage_scenarios`：按圈层与弹幕、评论或授权直播来源聚合，统计证据数和独立内容数，并展示代表性来源。弹幕场景会附带默认前后 6 秒内的邻近弹幕，帮助审核人判断它是在什么画面节点、以什么交流动作出现。`draft_card.usage_scenarios` 会同步写入可读场景草稿，但状态仍是 `pending`；程序不会从标题或邻近文本自动断言准确梗义。普通高频话、房间仪式或刷屏噪声仍需人工拒绝。

## 接入授权直播导出

在 `out/p0-meme-discovery/inbox/` 放置一个或多个 UTF-8 JSONL 文件，每行格式如下：

```json
{"platform":"douyu","room_id":"6657","session_id":"douyu:6657:2026-08-26-am","message_id":"msg-001","content":"一条弹幕文本","observed_at":"2026-08-26T02:30:00Z","room_title":"玩机器直播间","circle":"CS2/游戏"}
```

必填字段是 `platform`、`room_id`、`message_id`、`content`。即使上游行中带有 `nickname`、`user_id` 等字段，流水线也不会把它们复制到证据文件。原始导出文件由数据提供方按授权范围和保留期自行管理；程序不会移动或删除它。

## 定时运行

仓库提供通用的 `ops/cron/p0-meme-discovery.crontab.example`，以及 macOS 的 `ops/launchd/com.chiba.meme-discovery.plist.example`。把仓库和 Python 3.11+ 解释器替换为绝对路径后再安装。脚本使用非阻塞文件锁，前一次尚未结束时会跳过新一次运行。示例不会自动安装，避免未经确认修改本机定时任务。

## 人工审核后的下一步

审核人应补充语义、使用场景、反例和风险，并明确选择 `USE`、`UNDERSTAND_ONLY` 或 `REJECT`。通过审核的内容仍需按 [MAINTENANCE.md](MAINTENANCE.md) 新建不可变 Release、离线回放和测试环境验收，不能直接复制待审 JSON 到线上梗包。
