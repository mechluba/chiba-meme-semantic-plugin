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
export MEME_DISCOVERY_LLM_BASE_URL='https://your-openai-compatible-endpoint/v1'
export MEME_DISCOVERY_LLM_MODEL='your-semantic-model'
export MEME_DISCOVERY_LLM_API_KEY='从本地密钥管理器注入，不写进配置文件'
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

每个候选先生成 `occurrence_contexts`：按圈层与弹幕、评论或授权直播来源聚合，统计证据数和独立内容数，并展示代表性来源。弹幕证据附带默认前后 6 秒内的邻近弹幕。这个字段只回答“它出现在哪里、前后发生了什么”，**不能**作为千叶的使用场景。

随后，必需的语义模型阶段将证据提炼到 `draft_card`：

- `semantic_core`：表达在互动中的核心含义；
- `usage_routes[].when`：什么对话事件或用户状态下可能适用；
- `usage_routes[].communicative_intent`：说话者想向对方完成的具体交流动作，例如邀请共同惊讶、用自嘲缓和失败、反讽式质疑或请求解释；
- `usage_routes[].response_function`：千叶接这个梗会对当前互动起什么作用；
- route 级和全局 `required_context_signals`；
- `audience_requirements`、`hard_blocks`、正例和 SKIP 负例。

“即时反应”“形成共鸣”“表达情绪”“玩梗”等空泛描述会被结构校验拒绝。模型也必须区分 `meme_candidate`、`ordinary_expression` 和 `insufficient_evidence`，证据不足时不得硬编 usage route。所有合法输出仍标记为 `pending_human_review`，不能自动获得 `USE` 权限。

示例配置把语义提炼设为 `enabled=true, required=true`。模型地址、名称和 API key 必须由环境变量提供；缺少任一项时任务会在发起采集前失败，不会继续生成看似完整但无法用于决策的通用模板。模型响应按输入证据哈希缓存在 `out/p0-meme-discovery/semantic-cache/`，避免定时任务重复付费。

语义模型会收到候选短语、脱敏后的代表性社区文本、内容标题和邻近弹幕，不会收到评论者/观众身份字段。接入模型前仍需确认所选提供方的数据处理与保留政策允许这类公开社区语料；不满足时应保持任务失败，而不是切回无语义模板。

## 接入授权直播导出

在 `out/p0-meme-discovery/inbox/` 放置一个或多个 UTF-8 JSONL 文件，每行格式如下：

```json
{"platform":"douyu","room_id":"6657","session_id":"douyu:6657:2026-08-26-am","message_id":"msg-001","content":"一条弹幕文本","observed_at":"2026-08-26T02:30:00Z","room_title":"玩机器直播间","circle":"CS2/游戏"}
```

必填字段是 `platform`、`room_id`、`message_id`、`content`。即使上游行中带有 `nickname`、`user_id` 等字段，流水线也不会把它们复制到证据文件。原始导出文件由数据提供方按授权范围和保留期自行管理；程序不会移动或删除它。

## 定时运行

仓库提供通用的 `ops/cron/p0-meme-discovery.crontab.example`，以及 macOS 的 `ops/launchd/com.chiba.meme-discovery.plist.example`。把仓库和 Python 3.11+ 解释器替换为绝对路径后再安装。脚本使用非阻塞文件锁，前一次尚未结束时会跳过新一次运行。示例不会自动安装，避免未经确认修改本机定时任务。

## 人工审核后的下一步

审核人应逐条核对语义核心、交流意图、必需信号、受众条件、反例和风险，并明确选择 `USE`、`UNDERSTAND_ONLY` 或 `REJECT`。通过审核的内容仍需按 [MAINTENANCE.md](MAINTENANCE.md) 新建不可变 Release、离线回放和测试环境验收，不能直接复制待审 JSON 到线上梗包。
