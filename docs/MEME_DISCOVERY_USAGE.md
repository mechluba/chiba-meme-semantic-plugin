# 热梗发现与人工梗名接入使用手册

这份手册只说明如何运行候选发现、人工输入梗名和查看结果。来源边界、数据保留和语义规则见 [P0_DISCOVERY.md](P0_DISCOVERY.md)，审核后发布与回滚见 [MAINTENANCE.md](MAINTENANCE.md)。

## 入口选择

| 需求 | 入口 | 实际流程 |
| --- | --- | --- |
| 从热门视频、评论和抽样直播弹幕发现新梗 | `scripts/sample_live_rooms.py`、`scripts/run_p0_discovery.py` | 采集 → 黑名单清洗 → 重复表达候选 → 联网检索 → LLM 梗卡 |
| 已经知道梗名，直接补一张候选卡 | `scripts/build_meme_card_from_name.py` | 读取梗名 → 联网检索 → LLM 梗卡 |
| 采集机暂时没有模型凭据 | `scripts/run_p0_discovery.py --collection-only` | 执行到联网检索并等待补跑，不生成可用语义卡 |
| 给最近一次采集结果补跑模型 | `scripts/run_p0_discovery.py --resume-latest` | 读取最近一次搜索结果 → LLM 梗卡，不重复采集和搜索 |
| 每天汇总前一天结果并通知审核人 | `scripts/build_daily_meme_review.py` | 汇总自动/人工语义结果 → 最终审核页 → 飞书机器人通知 |

所有入口只生成本地待审核文件，不会修改 `resources/releases/`，也不会自动接入 Planner 或 Replyer。

## 运行准备

要求 Python 3.11+。先复制一份本地配置，按需调整来源、批量上限和输出目录：

```bash
cp ops/p0_discovery.example.json ops/p0_discovery.local.json
```

本地配置和 `out/` 都不应提交。模型凭据必须从受控环境注入，不能写进配置或输出文件。二选一配置模型：

```bash
# 方式一：复用同机 Chiba 的模型任务和 Provider 配置
export CHIBA_MODEL_CONFIG_PATH='/path/to/chiba/config/model_config.toml'
```

```bash
# 方式二：直接配置 OpenAI 兼容接口
export MEME_DISCOVERY_LLM_BASE_URL='https://your-endpoint.example/v1'
export MEME_DISCOVERY_LLM_MODEL='your-semantic-model'
export MEME_DISCOVERY_LLM_API_KEY='从本地密钥管理器注入'
```

缺少必需模型配置时，完整流程会在采集或搜索前明确失败，不会用通用模板伪造使用场景。

## 人工输入梗名

单个梗名：

```bash
python3 scripts/build_meme_card_from_name.py \
  --config ops/p0_discovery.local.json \
  --name '无量空处'
```

一次输入多个梗名时重复传入 `--name`：

```bash
python3 scripts/build_meme_card_from_name.py \
  --config ops/p0_discovery.local.json \
  --name '第一个梗' \
  --name '第二个梗'
```

人工入口不经过直播/视频采集、黑名单和重复表达挖掘。每个 `--name` 都建立独立候选，不查询库存、不去重、不合并别名；同名输入两次也会获得不同的 `candidate_id`。输入数量超过 `web_research` 或 `semantic_enrichment` 的单批上限时，脚本会要求分批运行，不会静默遗漏。

搜索问句默认使用“在中文互联网、评论区或弹幕中看到‘XX’是什么意思，是什么梗”，并复用发现配置中的梗百科、B 站和通用搜索源。搜索完成后才会把材料交给 LLM 生成 `draft_card`。

默认输出：

```text
out/p0-meme-discovery/
  latest-manual-run.json
  manual-runs/<UTC 时间>/
    research.pending-semantic.json
    meme-cards.pending-review.json
    review-queue.html
```

- `research.pending-semantic.json`：模型调用前落盘的搜索结果；模型请求失败时可用它检查搜索效果。
- `meme-cards.pending-review.json`：结构校验通过的语义草稿和搜索依据。
- `review-queue.html`：本批次的本地审核页面。
- `latest-manual-run.json`：最近一次成功完成的人工接入结果位置。

需要把输出归档到其他本地目录时传入 `--output-root /absolute/path`。

## 每日终审汇总与飞书通知

每天 14:00 汇总 Asia/Shanghai 的昨天结果。脚本同时读取：

- `runs/*/candidates.pending-review.json` 中的自动发现结果；
- `manual-runs/*/meme-cards.pending-review.json` 中的人工梗名结果。

只有已经完成语义提炼、分类为 `meme_candidate` 且至少有一条使用路线的候选会生成终审卡；普通表达、证据不足和模型失败项会计入汇总中的跳过原因，不会伪造成可入库卡片。

手动验证某一天：

```bash
export FEISHU_MEME_REVIEW_WEBHOOK='从本地任务配置注入，不写入仓库'
python3 scripts/build_daily_meme_review.py \
  --date 2026-09-07 \
  --output-root /absolute/path/to/out/p0-meme-discovery \
  --require-notify
```

定时任务不传 `--date`，脚本会自动选择昨天。默认输出为：

```text
out/p0-meme-discovery/
  latest-daily-review.json
  daily-reviews/<YYYYMMDD>/
    daily-review-summary.json
    daily-review.html
```

飞书自定义机器人只发送日期、候选数和本地归档路径，不会把 webhook 写入结果文件，也不会上传本地 HTML。审核页是单文件离线页面，候选数据已内嵌，复制到其他机器后仍可打开；页面支持：

- 选择“通过·可使用”“通过·仅理解”或“淘汰”；
- 展开并直接编辑每张卡的存储 JSON；
- 导入、导出审核记录 JSON；
- 导出审核后梗库和仅理解配置。

### 从审核 JSON 构建插件 Release

审核完成后下载“审核记录 JSON”，然后在插件仓库执行：

```bash
python3 scripts/build_reviewed_decision_release.py \
  --base-release-id reviewed-semantic-meme-library-20260904-v1 \
  --review-decisions /path/to/meme-semantic-card-review-decisions.json \
  --target-release-id reviewed-semantic-meme-library-YYYYMMDD-v1
```

脚本会读取审核页中编辑后的卡片 JSON，只合并 `approve_use` 和 `approve_understand`，为新库生成多语义原型向量，并写入 `resources/releases/<target-release-id>/`。目标目录必须不存在，避免覆盖已有 Release。生成新 Release 不等于已经切换运行时；还需要更新插件使用的 release ID、复核仅理解列表、完成回放和发布。

如果当前机器不能调用向量模型，可先校验并只生成合并后的 `library.json`：

```bash
python3 scripts/build_reviewed_decision_release.py \
  --base-release-id reviewed-semantic-meme-library-20260904-v1 \
  --review-decisions /path/to/meme-semantic-card-review-decisions.json \
  --target-release-id reviewed-semantic-meme-library-YYYYMMDD-v1 \
  --prepare-only /tmp/reviewed-meme-library.json
```

## 从视频和直播弹幕发现候选

先对配置中的直播间做一轮短时抽样：

```bash
python3 scripts/sample_live_rooms.py --config ops/live_sampling.example.json
```

抽样结果会进入 `out/p0-meme-discovery/inbox/`。随后执行完整发现流程：

```bash
python3 scripts/run_p0_discovery.py --config ops/p0_discovery.local.json
```

如果当前机器没有模型凭据，可以只运行采集、清洗、挖掘和搜索：

```bash
python3 scripts/run_p0_discovery.py \
  --config ops/p0_discovery.local.json \
  --collection-only
```

凭据恢复后补跑最近一批候选：

```bash
python3 scripts/run_p0_discovery.py \
  --config ops/p0_discovery.local.json \
  --resume-latest
```

发现任务默认输出：

```text
out/p0-meme-discovery/
  latest-run.json
  evidence-store.jsonl
  inbox/
  runs/<UTC 时间>/
    evidence.jsonl
    source-report.json
    candidates.pending-review.json
    review-queue.html
```

`pipeline_status=pending_human_review` 仅表示模型输出已经通过结构校验，仍不代表审核通过。`awaiting_semantic_enrichment` 表示尚未生成语义卡，不能用于千叶的使用判断。

## 审核和发布边界

审核人至少需要核对：

1. 联网来源是否真的解释了同一个表达，内容是否仍然有效；
2. `semantic_core` 是否描述真实含义，而不是字面释义；
3. `usage_routes[].communicative_intent` 是否说明用户具体想完成的交流动作；
4. `required_context_signals`、`hard_blocks`、正例和负例能否约束误用；
5. 最终决策是 `USE`、`UNDERSTAND_ONLY` 还是 `REJECT`。

待审 JSON 不能直接复制到线上梗包。审核通过后仍需生成不可变 Release、执行离线回放、在测试环境验收，再按 [MAINTENANCE.md](MAINTENANCE.md) 中的发布和回滚流程操作。

## 常见失败

| 现象 | 处理 |
| --- | --- |
| 提示缺少 `base_url`、模型或密钥 | 检查 `CHIBA_MODEL_CONFIG_PATH`，或完整设置三个 `MEME_DISCOVERY_LLM_*` 环境变量 |
| 人工输入超过单批上限 | 减少本次 `--name` 数量后分批运行，不建议临时取消安全上限 |
| 搜索完成但模型调用失败 | 查看本轮 `research.pending-semantic.json`；修复模型配置后重新运行人工入口 |
| `pipeline_status=awaiting_semantic_enrichment` | 使用相同输出根目录执行 `--resume-latest` |
| 直播间没有弹幕 | 先查看 `sampling-report.json` 中的 `offline`、`no_messages`、`missing_credentials` 或协议错误，不要把空结果记为采集成功 |
