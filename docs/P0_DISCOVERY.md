# P0 热梗候选离线发现

这条流水线每天从可访问的 P0 来源抓取一小批公开文本，做脱敏标准化和重复表达聚合，最后只生成本地 `pending` 待审文件。它不修改 `resources/releases/`，也不会把任何候选自动接入 Planner 或 Replyer。

## 当前来源边界

| 来源 | 当前能力 | 说明 |
| --- | --- | --- |
| B 站热门、推荐与重点 UP 新投稿 | 热门榜、匿名首页推荐流、重点账号轮换、首批公开评论、有限分段弹幕 | 覆盖游戏、LOL、二游、泛二次元、数码娱乐、科技资讯和虚拟主播；重点账号严格校验 mid，避免同名搜索结果混入 |
| B 站直播 | CS2、LOL、DOTA2 官方赛事，以及泛式、逍遥散人、极客湾和虚拟主播房间短时弹幕抽样 | 按常见时段和领域轮换；离线房间只归档状态，不建立长连接 |
| 斗鱼 6657 / 9999 / 71415 | 公开直播页短时弹幕抽样 | 使用全新匿名 Chrome 会话加载官方公开直播页；按本地历史高消息时段和领域轮换，每房间默认最多 45 秒 / 200 条 |
| 虎牙 10188 | 官方弹幕接口状态监测 | 官方接口需要 appId 与签名密钥；缺少 `HUYA_OPEN_APP_ID` / `HUYA_OPEN_SECRET` 时显式归档 `missing_credentials`，不绕过鉴权 |
| 授权直播导出 | 本地 JSONL inbox | 供平台正式能力、主播授权工具或人工导出的弹幕进入同一流水线 |

公开页面能访问不等于允许无限量抓取和长期保存。默认只保存消息文本、消息 ID、内容/房间 ID、观察时间和圈层；评论者/观众昵称、用户 ID、头像、粉丝牌等个人字段不会进入标准化证据，正文中的显式 `@昵称` 会被替换为 `@用户`。直播抽样不登录、不发送消息、不保存原始 WebSocket 帧或页面档案。运行范围通过配置中的视频数、分段数、评论数、直播间数、采样时长和消息上限限制。`out/` 不应同步到公共仓库，并应由运营方按实际授权设置短期保留和定期清理策略。

## 直播间短时抽样

```bash
python3 scripts/sample_live_rooms.py --config ops/live_sampling.example.json
```

每次最多并行抽样六个房间。轮换器先选择当前常见开播时段内的房间，再优先覆盖不同领域；没有固定周表的账号仍以平台动态和实际开播状态为准。完整账号、房间和时段置信度见 [meme-discovery-governance.md](meme-discovery-governance.md) 与 `ops/meme_source_watchlist.json`。离线的 B 站房间会在页面采样前直接记录 `offline`，因此不会为了覆盖房间而持续监听。每次运行都会生成：

```text
latest-live-sampling.json
live-archive/<日期>/<UTC 时间>/
  messages.jsonl          # 匿名化后的本轮消息
  sampling-report.json    # 每个房间的成功、无消息、缺凭据或错误状态
inbox/live-<UTC 时间>.jsonl
```

`inbox/` 副本会在下一次发现任务中被标准化为 `public_live_sample` 证据，并与人工或平台授权导出的 `authorized_live_export` 明确区分。抽样报告中的 `chat_collected=true` 只在实际保存了消息时出现；房间离线、页面协议变化、缺凭据和轮换跳过均不会被记成成功。

## 运行一次

```bash
cp ops/p0_discovery.example.json ops/p0_discovery.local.json
export MEME_DISCOVERY_LLM_BASE_URL='https://your-openai-compatible-endpoint/v1'
export MEME_DISCOVERY_LLM_MODEL='your-semantic-model'
export MEME_DISCOVERY_LLM_API_KEY='从本地密钥管理器注入，不写进配置文件'
python3 scripts/run_p0_discovery.py --config ops/p0_discovery.local.json
```

如果采集机没有合法注入模型密钥，可显式使用 `--collection-only`。它仍会抓取、去重、滚动保留并生成表层重复信号，但所有候选都标记为语义未运行，不能拿给千叶做使用决策；后续必须在持有受控模型凭据的环境用 `scripts/enrich_pending_candidates.py` 补齐交流意图。

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

`source-report.json` 会区分真实拿到文本、仅房间元数据和错误，不把降级误报成采集成功。消息 ID 生成幂等键，同一条弹幕不会在后续运行中重复写入；候选从默认 14 天的 `evidence-store.jsonl` 滚动窗口生成，过期证据会从这个本地滚动仓移除。每条证据会写入 `retention_deadline` 和来源政策备注。进入候选挖掘前还会经过可解释黑名单清洗；清洗只过滤审核输入并输出原因统计，原始归档不会被静默删除。

每个候选先生成 `occurrence_contexts`：按圈层与弹幕、评论或授权直播来源聚合，统计证据数和独立内容数，并展示代表性来源。弹幕证据附带默认前后 6 秒内的邻近弹幕。这个字段只回答“它出现在哪里、前后发生了什么”，**不能**作为千叶的使用场景。

随后，必需的语义模型阶段将证据提炼到 `draft_card`：

- `semantic_core`：表达在互动中的核心含义；
- `usage_routes[].when`：什么对话事件或用户状态下可能适用；
- `usage_routes[].communicative_intent`：说话者想向对方完成的具体交流动作，例如邀请共同惊讶、用自嘲缓和失败、反讽式质疑或请求解释；
- `usage_routes[].response_function`：千叶接这个梗会对当前互动起什么作用；
- route 级和全局 `required_context_signals`；
- `audience_requirements`、`hard_blocks`、正例和 SKIP 负例。

“即时反应”“形成共鸣”“表达情绪”“玩梗”等空泛描述会被结构校验拒绝。模型也必须区分 `meme_candidate`、`ordinary_expression` 和 `insufficient_evidence`，证据不足时不得硬编 usage route。所有合法输出仍标记为 `pending_human_review`，不能自动获得 `USE` 权限。

模型初稿如果只有 JSON 结构不合格，流水线默认允许一次带具体字段路径的结构修复；修复提示不得改变分类结论或增加新事实，修复结果仍须完整通过同一套 usage route、证据 ID、正反例和禁用条件校验。再次失败的候选保留为 `error`，不进入可用路线。

示例配置把语义提炼设为 `enabled=true, required=true`。可通过 `chiba_model_config_path` 和 `chiba_text_task=utils` 只读复用 Chiba 的任务、模型与 Provider 配置；适合让任务与 Chiba 部署在同一受控环境中运行，密钥不需要复制到候选文件或审核报告。没有共址配置时，也可继续通过 `MEME_DISCOVERY_LLM_*` 环境变量注入 OpenAI 兼容模型。缺少任一必需配置时任务会在发起采集前失败，不会继续生成看似完整但无法用于决策的通用模板。模型响应按输入证据哈希缓存，避免定时任务重复付费。

启用 `embedding_calibration` 后，流水线会用 Chiba 的 `embedding` 任务为每条合法 usage route 生成向量，并和已审核 Release 的多原型向量比较。审核页会展示最接近的旧梗卡、route 和相似度，用来判断“已有卡别名 / 已有卡新路线 / 可能是新梗”。相似度没有自动合并权限，也不会改变 `pending_human_review` 状态；配置的模型名称和向量维度必须与目标 Release 一致。通用示例默认关闭这一可选阶段；与 Chiba 共址运行并填好模型配置路径后再开启。

如果采集和模型调用需要分开运行，可以先生成关闭语义阶段的候选文件，再用 `scripts/enrich_pending_candidates.py` 在持有 Chiba 模型配置的受控环境中另存语义 JSON 与审核页。该脚本不覆盖输入文件，也不发布 Release。

语义模型会收到候选短语、脱敏后的代表性社区文本、内容标题和邻近弹幕，不会收到评论者/观众身份字段。接入模型前仍需确认所选提供方的数据处理与保留政策允许这类公开社区语料；不满足时应保持任务失败，而不是切回无语义模板。

## 接入授权直播导出

在 `out/p0-meme-discovery/inbox/` 放置一个或多个 UTF-8 JSONL 文件，每行格式如下：

```json
{"platform":"douyu","room_id":"6657","session_id":"douyu:6657:2026-08-26-am","message_id":"msg-001","content":"一条弹幕文本","observed_at":"2026-08-26T02:30:00Z","room_title":"玩机器直播间","circle":"CS2/游戏"}
```

必填字段是 `platform`、`room_id`、`message_id`、`content`。即使上游行中带有 `nickname`、`user_id` 等字段，流水线也不会把它们复制到证据文件。原始导出文件由数据提供方按授权范围和保留期自行管理；程序不会移动或删除它。

## 定时运行

仓库提供通用的 `ops/cron/p0-meme-discovery.crontab.example`，以及 macOS 的 `ops/launchd/com.chiba.meme-live-sampling.plist.example`、`ops/launchd/com.chiba.meme-discovery.plist.example`。默认节奏是每两小时 07 分短时抽样、每天 04:20 汇总洗梗。把仓库和 Python 3.11+ 解释器替换为绝对路径后再安装。两个脚本各自使用非阻塞文件锁，前一次尚未结束时会跳过新一次运行。

## 人工审核后的下一步

审核人应逐条核对语义核心、交流意图、必需信号、受众条件、反例和风险，并明确选择 `USE`、`UNDERSTAND_ONLY` 或 `REJECT`。通过审核的内容仍需按 [MAINTENANCE.md](MAINTENANCE.md) 新建不可变 Release、离线回放和测试环境验收，不能直接复制待审 JSON 到线上梗包。

梗、口癖、普通表达和噪音的定义，别称/谐音合并规则，以及库存降权与淘汰项冷却复审机制见 [meme-discovery-governance.md](meme-discovery-governance.md)。生命周期程序只生成待审建议，不会自动修改运行时梗库。
