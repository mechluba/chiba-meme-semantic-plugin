# Chiba 语义梗库插件

该插件把人工审核后的中文梗卡接入真实 Maisaka Planner → Replyer 链路。它不使用正则、关键词表或字面命中来决定是否玩梗，召回和最后判断都基于语义。

## 在线链路

1. 仅从宿主读取 Planner 当前真实会话中用户可见的最近消息。
2. 使用宿主 `llm.embed` 生成一次语义向量，在内存向量矩阵中召回 Top-K。
3. Planner 可显式选择 `USE`、`UNDERSTAND_ONLY` 或 `SKIP`。
4. Planner 未选择或选择 `SKIP` 时，独立语义裁判每条回复重新读取最新收发消息及当前媒体任务；向量候选可以缓存，但不能复用旧对话作为当前使用许可。用户指出重复、要求停用，或最近助手已连续使用明显梗时，裁判优先跳过。
5. `USE` 只向 Replyer 提供一张经过校验的卡；`UNDERSTAND_ONLY` 只提供语义，不提供可复读原句。
6. `UNDERSTAND_ONLY` 的发送前语义质量门会检查回复是否复读、改写或讲解了禁用梗；违规时最多重生成一次。
7. 发送后异步评估是否真的使用、是否自然、是否生硬、是否误入严肃语境，不阻塞已生成回复。决策及其语境按会话与回复 ID 绑定；质量门和效果评估读取该条回复的快照，不读取后来被其他回合覆盖的候选状态。

插件默认启用，默认只服务 `galpet_app` 私聊。共用同一后端的其他平台、群聊和传统对话产品不会进入梗召回链路。

## 当前梗包

```text
resources/releases/reviewed-semantic-meme-library-20260914-user-ai-v1/
```

- 158 张人工审核卡
- 358 条使用路线
- 1790 个语义原型向量
- 每条路线由使用场景、交流意图、正例和反例共同校准
- 59 张高风险卡固定为 `UNDERSTAND_ONLY`

`release.json` 固定记录库、索引和向量文件的 SHA256。插件启动时逐项校验，不接受绝对路径、目录越界、未审核卡、错位索引或错误向量维度。

## 性能边界

本地余弦查询约为 0.05 ms；耗时主要来自远程模型：

- 首次 Embedding 通常约 0.45–0.84 s，偶发约 1.9 s。
- 同一可见上下文 60 秒内复用召回缓存，命中约 0.1 ms，不再次请求 Embedding。
- 仅当 Planner 没有授权时，独立语义裁判通常额外增加约 1.3–1.6 s。
- 仅 `UNDERSTAND_ONLY` 回复进入发送前语义质量门；若违规并重生成，会再增加一次 Replyer 延迟。

所以“向量库查询”本身是毫秒级，但完整语义决策不是纯数据库查询。

## 本地验证

在 Chiba 主仓库中执行：

```bash
.venv/bin/ruff check plugins/chiba_meme_semantic_plugin
.venv/bin/pytest -q plugins/chiba_meme_semantic_plugin/tests
```

真实 Planner → Replyer 回放：

```bash
.venv/bin/python \
  plugins/chiba_meme_semantic_plugin/scripts/run_real_chain_probe.py \
  --chiba-root /path/to/chiba \
  --spec plugins/chiba_meme_semantic_plugin/tests/real_chain_cases.json \
  --output out/meme-semantic-plugin/real-chain-results.json
```

## 测试到生产

该目录必须作为独立 Git 仓库发布，不能依赖 Chiba 主仓部署脚本。主仓会保护服务器上的运行态插件，并排除本地忽略插件。

测试与生产均从同一个插件 commit 安装或更新。安装后分别生成运行指纹：

```bash
.venv/bin/python scripts/runtime_fingerprint.py \
  --chiba-root /path/to/chiba \
  --plugin-root /path/to/chiba/plugins/chiba_meme_semantic_plugin \
  --environment staging \
  --require-clean \
  --output /tmp/chiba-meme-staging.json
```

再用以下命令比较：

```bash
.venv/bin/python scripts/diff_runtime_fingerprints.py \
  /tmp/chiba-meme-staging.json \
  /tmp/chiba-meme-production.json
```

两边必须使用相同的 Chiba commit、插件 commit/runtime tree、插件配置、梗包哈希、模型任务配置和行为关键配置。

服务器的 `/opt/.../current` 不是 Git checkout；指纹工具会自动读取同级 `shared/deployed-revision`。也可以用 `--chiba-revision <完整 commit>` 显式指定，但不能省略 revision。

长期找新梗、更新梗和让老梗退环境的流程见 [docs/MAINTENANCE.md](docs/MAINTENANCE.md)。

2026-09-14 经作者明确授权加入两条 AI 梗，保留原文与个人记录；原有 156 张卡和 1780 个向量逐项不变。授权与场景见 `resources/contributions/user-ai-memes-20260914.json`。
