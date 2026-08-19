# 变更记录与问题追踪

> 本文件记录项目开发过程中的关键问题修复、新增功能和已知限制。
> 按时间线组织，每个条目包含：问题 → 根因 → 方案 → 涉及文件 → 验证 → 已知限制。

---

## 已修复问题

### 修复 #1：澄清追问死循环（追问 3-4 次不进入研究）

- **日期**：2026-08-14
- **问题**：clarify 节点由 LLM 自行判断"是否再追问"，导致循环 3-4 次仍不进入研究阶段
- **根因**：无硬限制，LLM 倾向于"再问一个"；追问缺乏聚焦，每轮都在模糊维度上重新发散
- **方案**：核心理念 **LLM 只打分，代码做决策**
  - 第一轮预搜索一次（LLM 生成 query → 程序化调用搜索工具 → 压缩上下文 ≤12000 字符）
  - LLM 五维打分：subject / scope / audience / timeframe + search_anchored
  - 确定性规则判定：`subject 模糊 → 必问`；`其余维度 vague≥2 且搜索锚定不住 → 问`；单维度模糊 → 写成假设推进
  - `max_clarification_rounds=3` 硬上限兜底，循环必然终止
  - 追问带 rationale，推进时列明假设供用户纠偏
  - 预搜索上下文复用给研究简报；报告完成后重置计数，同会话新一轮可重新澄清
- **涉及文件**：`state.py`、`prompts.py`、`deep_researcher.py`、`configuration.py`
- **验证**：追问次数 ≤3 且通常更少；足够精确的问题直接进研究不追问；简报因有预搜索上下文而更具体
- **已知限制**：ANTHROPIC/OPENAI 原生搜索下预搜索跳过（搜索工具是 dict 不可程序化调用），判定退化为无上下文打分；TAVILY/DUCKDUCKGO 不受影响

### 修复 #2：Agent 无记忆（追问"覆盖"回答，循环不退出）

- **日期**：2026-08-14
- **问题**：每轮对话 Agent 都"不记得"之前说过什么，导致反复追问
- **根因**：`deep_researcher_builder.compile()` 未传 checkpointer → `thread_id` 无效，每轮消息只剩最新一条、`clarify_count` / `pre_search_context` 全重置。上游在 LangGraph Platform 部署时平台自动注入 checkpointer 所以没暴露，`app.py` 本地直调时缺失
- **方案**：`compile(checkpointer=MemorySaver())`（进程内内存，本地够用；平台部署时注入自己的，无副作用）。附带修复：`pre_search_context` 现在跨轮复用不再重搜
- **涉及文件**：`deep_researcher.py`
- **验证**：第 1 轮 messages=[human,ai] → 第 2 轮=[human,ai,human,ai,ai] 累积生效；`clarify_count` 持久化=1；浏览器端整体流程跑通
- **陷阱**：改完核心图逻辑**必须重启服务进程**才生效——Python 模块只在进程启动时 import 一次，Streamlit 重跑脚本 ≠ 重载模块

### 修复 #3：预搜索失败静默（DDG 限流时无感知）

- **日期**：2026-08-14
- **问题**：`_run_pre_search` 用 try/except 把所有异常静默吞掉返回 None，DDG 限流时用户无感知，澄清打分退化但无任何日志
- **根因**：DuckDuckGo 无官方 API，`duckduckgo_search` 库是爬虫实现（伪装浏览器抓取 lite/html 端点），被检测后 IP 级返回 HTTP 202 "Ratelimit"
- **方案**：按环节分层处理——
  - **预搜索**（必要输入）：失败会劣化澄清打分 → 重试 3 次、退避 2s/4s，仍失败才降级
  - **研究搜索**（增强输入）：失败只损新鲜度 → 单查询级容错，失败记 warning 继续，全部失败才抛 ToolException
  - `DDGS(timeout=5)` 防止 DDG 黑洞把流程挂死
  - 最坏降级 = 无锚定五维打分，流程必然走完
- **涉及文件**：`utils.py`、`deep_researcher.py`
- **验证**：实测 DDG 正处限流（全查询 202 Ratelimit）→ 预搜索重试 3 次（退避 2s/4s）→ 降级为无锚定打分 → 澄清照常完成，日志全程可见 warning

### 修复 #4：UI 误标（clarify 节点消息全部标为"🤔 Agent:"）

- **日期**：2026-08-14
- **问题**：`clarify_with_user` 节点既输出"追问"也输出"确认后推进"的 verification 消息，app.py 把推进消息也标成"🤔 Agent:"并写入 `result["clarification"]`，前端误以为还在等用户澄清
- **根因**：app.py 只判断 `msgs[-1]` 有 content 就标为追问，未区分追问消息和推进消息
- **方案**：追问消息在 `deep_researcher.py` 侧加 `additional_kwargs={"is_clarification_question": True}` 标记；app.py 判定 `getattr(msgs[-1], "additional_kwargs", {}).get("is_clarification_question") is True` ——真追问才设 clarification + "🤔 Agent:"，推进消息只显示"➡️ 澄清完成，直接开始研究"，不写 clarification
- **涉及文件**：`deep_researcher.py`、`app.py`
- **验证**：追问消息 marker=True；推进消息无标记走"澄清完成"分支

### 修复 #5：第二轮静默挂起（用户回答澄清后"既不追问也不给研究内容"）

- **日期**：2026-08-17
- **问题**：用户回答澄清后，图重新进入 `clarify_with_user`，先要调一次评估模型判断"回答够不够清楚"。该调用全链路无超时，且挂了 `.with_retry(3)` 把等待窗口放大到最多 3 次重试。模型 API 瞬时无响应时 `await` 无限阻塞 → `astream` 在产出第一个事件之前卡住 → 前端零反馈
- **根因**：评估调用无超时保护 + `.with_retry(3)` 放大了等待窗口
- **方案**：
  1. `configuration.py` 新增 `clarification_assessment_timeout`（默认 60s，范围 10-600）
  2. `deep_researcher.py` 评估调用包 `asyncio.wait_for`，超时 → warning 日志 + 构造"按假设推进"的降级评估（所有维度设为 clear、question 为空、verification 注明"评估超时"）。`_assess_need_clarification` 见到空 question 自然走推进分支——不追问、不挂死、不死循环
  3. `app.py` run 一开始立即 `st.status("🧠 分析中...")` + 写一行"正在分析你的问题"，任何等待都不再静默
- **涉及文件**：`configuration.py`、`deep_researcher.py`、`app.py`
- **验证**：定向测试（HangingModel 假模型 + 2s 超时）→ 2.0s 返回、goto=write_research_brief、verification 为超时降级消息；真实两轮端到端回归（deepseek）正常出报告
- **已知限制**：研究阶段 supervisor/researcher 的 LLM 调用仍无超时——但那里卡住时前端已显示"🔍 研究中..."，有反馈、可感知，与本次"静默"不同

### 修复 #6：已答还反复追问 + DDG 频繁限流

- **日期**：2026-08-17
- **问题**：用户回答"入门"后仍被连问 2 次；DDG 整个会话持续 202 Ratelimit
- **根因**：
  1. **追问反复**：DDG 挂时 `search_anchored=False`，规则"次维度模糊≥2 且无锚定 → 问"每一轮都触发——即使评估模型已承认"主题/受众清楚"，scope/timeframe 一模糊就又问
  2. **DDG 限流**：研究阶段主管并行 `max_concurrent_research_units=5` 个单元 × 每个单元工具调用 `asyncio.gather` 全并行，峰值 30+ 次同时打 DDG → IP 被封；公司 NAT 共享公网 IP 加剧
- **方案**：
  1. **追问收敛**：`clarify_count >= 1`（用户已答过一轮）后，次维度（scope/audience/timeframe）模糊不再触发追问，仅 `subject_clear=="vague"` 才问。subject 锚定整份报告，仍受保护
  2. **降并发**：`max_concurrent_research_units` 默认 5→2；新增 `max_concurrent_tool_calls=2`，researcher_tools 用 `asyncio.Semaphore` 限流。峰值搜索并发从"5 单元 × N 工具全并行"降到"2 单元 × 2 工具"
- **涉及文件**：`configuration.py`、`deep_researcher.py`
- **验证**：规则测试——round1 次维度模糊且无锚定 → 问；clarify_count=1 同评估 → 推进不追问；clarify_count=1 但 subject 仍模糊 → 仍问。Semaphore 测试——4 个假工具各 0.4s，并发=2、总耗时 0.8s（两批）。循环回归证明完整出报告路径无回归
- **已知限制**：DDG 是爬虫，降并发只能减少触发、不能消除；IP 冷却后自行恢复。彻底根治需换搜索引擎

---

## 新增功能

### 向量记忆（运行时自建语义索引）

- **日期**：2026-08-17
- **目标**：给研究 Agent 加"读过什么的语义记忆"——网页被抓到即 embed 进线程级索引，后续轮次可语义回看原文/片段，不再只有一份有损摘要
- **设计**：
  - **依赖**：`fastembed`（ONNX，免 torch）+ `BAAI/bge-small-zh-v1.5`（首次从 HuggingFace 下载 ~100MB/约 1 分钟，之后本地缓存；推理本地化，零 API 费用）
  - **存储**：`vector_memory.py` 新模块，线程级（`thread_id`）进程内索引，numpy 余弦相似度；研究周期结束（`final_report_generation`）`clear_memory` 清空
  - **写入**：搜索工具抓到内容即 `remember(config, text, url, title)`——DDG 存搜索片段、Tavily 存整页 `raw_content`（截断后），每篇一次、增量、不重复 embed
  - **读取**：新工具 `recall_from_read_content(query, top_k)` 绑给 researcher，`config` 由 LangChain 自动注入，不暴露给模型（args schema 只有 `query` / `top_k`）
  - **切块**：单条 >1800 字符按重叠窗口切块，短内容整体保留
  - **降级**：embedding 加载失败 / 缺 thread_id / 内容为空 → 静默 no-op，图行为与之前完全一致（增强层，不是依赖层）
- **涉及文件**：`vector_memory.py`（新）、`utils.py`、`deep_researcher.py`、`uv.lock`
- **验证**：模型语义相似度（遗忘门↔门控 0.63 vs 遗忘门↔苹果 0.28，dim=512）；round-trip（`remember→recall` top-1 排序正确 → 工具格式化 → `clear` 生效）；工具注册 + config 注入；deepseek `bind_tools` 接受 recall 工具；循环回归（DDG 全挂）照常出报告
- **已知限制**：
  1. DDG 只返回搜索片段、不抓整页正文 → DDG 下向量记忆存的是片段记忆（中低价值）；Tavily 的整页 `raw_content` 才能装满（高价值）。机制已就绪，切 Tavily 自动升级
  2. 首次模型下载需联网 HuggingFace（公司网若封 HF，需设 `HF_ENDPOINT=https://hf-mirror.com` 镜像）
  3. 索引是进程内、任务级，重启即失、跨会话不持久（当前定位够用）

---

## 操作备忘

### DDG 限流时切换兜底

**机制**：`Configuration.from_runnable_config` 从环境变量（字段名大写）读取配置，只改 `.env` 即可切换搜索，无需改代码。

| 搜索模式 | 配置 | 说明 |
|----------|------|------|
| Tavily 免费档 | `SEARCH_API=tavily` + `TAVILY_API_KEY=tvly-xxx` | 推荐应急首选，约 1000 次/月 |
| 纯模型知识 | `SEARCH_API=none` | 零搜索零成本，报告全靠模型训练记忆 |
| DuckDuckGo | `SEARCH_API=duckduckgo` | 默认，免费但会限流 |

切换后**必须重启 `streamlit run app.py`**。切换后的预搜索行为：`tavily` → 正常；`none` → 自动跳过，澄清打分退化为无锚定。

### 首轮量化 eval（2026-08-17）

- **方法**：同一查询（LSTM 门控区别），唯一变量 `allow_clarification` = True vs False，`SEARCH_API=tavily`，LLM-as-judge（deepseek）打分
- **数据**（单查询）：

| 指标 | fork_full（有澄清） | baseline（无澄清） | delta |
|------|---------------------|--------------------|-------|
| completeness | **5/5** | 4/5 | **+25%** |
| 成本 | **$0.118** | $0.159 | **-26%** |
| 报告长度 | 8,054 字 | 5,719 字 | +41% |

- **结论**：澄清机制（预搜索 + 打分 + 代码决策）同时提升质量并降低成本——更准方向 = 更完整报告 + 更少浪费 token
- **扩样**：充值 deepseek 后重跑 `eval_fork.py` 可扩大样本；本次因余额不足（402）中断
- **成本监控**：deepseek 余额需主动监控——本次跑 eval 时账户耗尽才发现；建议定期查余额或加调用级预警

---

## 发布准备

> 项目上传 GitHub 前的补充工作，按时间线记录。
> 评估框架详见 [docs/eval-framework.md](docs/eval-framework.md)（五层工业标准模型）。

### 1. 添加 MIT 许可证

- **日期**：2026-08-17
- **文件**：`LICENSE`（新建）
- **内容**：MIT License，copyright 2026 roylau
- **必要性**：开源项目必须有许可证，否则默认受版权保护，他人无法合法使用/修改/分发

### 2. README 架构图

- **日期**：2026-08-17
- **文件**：`README.md`
- **内容**：新增 Mermaid 流程图，展示完整节点链路：clarify → write_research_brief → supervisor → researcher_1/researcher_2 → compress → final_report → clear_memory
- **语言**：英文标签，与 README 原文统一

### 3. 单元测试

- **日期**：2026-08-17
- **文件**：`tests/test_core_logic.py`（新建）
- **覆盖**：39 个测试用例，全部通过
  - 澄清判定规则（11 例）：subject 模糊必问、次维度模糊 + 未锚定才问、partial 不算模糊
  - 追问收敛规则（5 例）：clarify_count≥1 后仅 subject 模糊触发
  - 图路由函数（9 例）：clarification 路由、research iteration 限制
  - State reducer（6 例）：add 模式 vs override 模式
  - 向量记忆切块（8 例）：空值、短文本、重叠验证
- **设计**：纯逻辑测试，不依赖 LLM 调用；核心函数从源码复制，保持测试独立性
- **运行方式**：`.venv/bin/python -m pytest tests/test_core_logic.py -v`（绕过 uv 的 onnxruntime wheel 问题）

### 4. README 中英文统一 + 中文版

- **日期**：2026-08-17
- **文件**：`README.md`
- **问题**：原 README 为英文，新增 Mermaid 图和设计原则文字用了中文，导致中英混杂
- **方案**：所有新增内容改为英文；在英文版后添加完整中文版，用分隔线隔开

---

## 已知限制与待改进项

| 项 | 说明 | 优先级 |
|---|---|---|
| 研究阶段 LLM 无超时 | supervisor/researcher 的模型调用卡住时前端停"🔍 研究中..."（有反馈但无进展） | 中 |
| DDG 限流根治 | 爬虫本质 + 共享公网 IP，降并发只能缓解不能消除 | 低（已有切换方案） |
| DDG 全页记忆 | 向量记忆在 DDG 下只有片段价值，缺抓全文步骤 | 低 |
| 预搜索失败时锚点退化 | 搜索 API 故障或 native search 路径下预搜索返回空，澄清走向保守 | 低（已论证方案） |
