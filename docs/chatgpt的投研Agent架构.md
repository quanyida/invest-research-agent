目前 C 端消费级 Agent（尤其是投研 Agent、个人金融 Agent、AI 助理）的主流架构，可以理解为：

**用户交互层 → Agent 编排层 → 专业能力层 → 数据与知识层 → 模型基础设施层 → 安全与评估层。**

但如果是做一款真正面向消费者的投研 Agent，我不建议简单地把它设计成一个「大模型 + RAG + Tools」的架构。

更合理的设计是：**以 Agent Runtime 为核心，以专业研究工作流为主线，以数据和工具为基础设施，以用户记忆和个性化为长期竞争壁垒。**

下面按照产品架构、技术架构和投研业务架构三个视角拆解。

# **一、C 端投研 Agent 的整体架构**

**L1 · 用户交互层**

Chat / Voice / Dashboard / Watchlist / Push / 投研报告

**L2 · Agent 产品与编排层**

Intent Router · Planner · Supervisor · Memory · Context Manager

**L3 · 专业 Agent 与 Skill 层**

宏观研究 Agent

行业研究 Agent

公司研究 Agent

财报分析 Agent

估值分析 Agent

投资组合 Agent

风险分析 Agent

事件追踪 Agent

**L4 · 工具与数据服务层**

Search · Financial API · Market Data · SEC Filings · Python · SQL · Browser

**L5 · 数据与知识层**

行情数据库 · 财务数据库 · 新闻库 · 文档库 · Vector DB · 用户知识库

**L6 · 模型与基础设施层**

LLM · Embedding · Reranker · VLM · ASR · Agent Runtime · Cache · Queue

**横向贯穿：安全与治理体系**

权限控制 · 数据溯源 · 引用校验 · 风险提示 · 成本控制 · 质量评估 · 日志审计

这里的 L1-L6 是一种逻辑分层，不代表每层都需要单独部署成一个微服务。

# **二、每一层具体做什么？**

## **L1：用户交互层（Experience Layer）**

这一层决定用户如何使用 Agent。

主要包括：

| 模块                 | 主要功能         |
| ------------------ | ------------ |
| AI Chat            | 自然语言对话、连续追问  |
| Research Dashboard | 展示研究结论、分析过程  |
| Watchlist          | 自选股票、行业、资产   |
| Portfolio          | 用户持仓、收益、风险分析 |
| Alert Center       | 财报、新闻、异动提醒   |
| Report Center      | 自动生成研究报告     |
| Voice              | 语音交互、语音问答    |

C 端产品需要特别注意：不能只有 Chat 窗口。

用户真正需要的是一个持续工作的个人投研助手，而不是每次都要重新提问的聊天机器人。

## **L2：Agent 产品与编排层（Agent Orchestration）**

核心层

这是整个 Agent 系统的大脑，负责将用户需求转换成可以执行的任务。

主要模块：

**1. Intent Router（意图识别）**

判断用户想做什么：

- 查询：英伟达最新财报怎么样？
- 分析：英伟达目前估值贵不贵？
- 比较：英伟达和 AMD 哪个更有投资价值？
- 监控：如果英伟达跌破某个价格，通知我。
- 研究：帮我分析 AI 芯片行业未来三年的发展。

**2. Planner（任务规划）**

把复杂任务拆分为子任务。

例如用户说：

「帮我分析一下英伟达未来两年的投资价值。」

Planner 可以拆成：

1. 获取最新财报。
2. 分析收入和利润增长。
3. 研究 AI 芯片行业竞争格局。
4. 分析主要竞争对手。
5. 预测未来两年业绩。
6. 计算估值区间。
7. 识别潜在风险。
8. 输出投资研究报告。

**3. Supervisor（多 Agent 调度）**

负责决定：

- 哪些任务交给哪个 Agent。
- 哪些任务可以并行。
- 哪些任务存在依赖关系。
- 是否需要重新规划。
- 什么时候结束任务。

**4. Memory（记忆系统）**

C 端 Agent 非常重要的一层：

| 记忆类型              | 示例           |
| ----------------- | ------------ |
| Profile Memory    | 用户的投资经验、风险偏好 |
| Preference Memory | 喜欢价值投资还是成长投资 |
| Episodic Memory   | 上次研究过英伟达     |
| Task Memory       | 当前研究任务进度     |
| Long-term Memory  | 长期关注的行业和公司   |

**5. Context Manager（上下文管理）**

负责管理历史对话、研究结果、检索内容和模型上下文，避免上下文无限膨胀。

## **L3：专业 Agent 与 Skill 层（Domain Capability）**

这一层是投研 Agent 的专业能力中心。

我建议将其拆成两类，而不是所有能力都做成独立 Agent。

**A. 专业 Agent：负责复杂的、有独立上下文的研究任务**

| Agent           | 核心职责           | 典型输出   |
| --------------- | -------------- | ------ |
| Macro Agent     | 宏观经济、货币政策      | 宏观分析   |
| Industry Agent  | 行业趋势、产业链       | 行业研究报告 |
| Company Agent   | 公司基本面、竞争格局     | 公司分析   |
| Financial Agent | 财报、财务指标        | 财务质量分析 |
| Valuation Agent | DCF、PE、PB、可比估值 | 估值区间   |
| Portfolio Agent | 资产配置、组合分析      | 组合建议   |
| Risk Agent      | 风险识别、压力测试      | 风险清单   |

**B. Skills：负责可复用的具体任务**

例如：

- `get_stock_price`：获取股票行情
- `parse_10k`：解析年报
- `calculate_dcf`：DCF 估值
- `calculate_pe`：计算 PE
- `compare_companies`：公司对比
- `extract_guidance`：提取管理层业绩指引
- `analyze_sentiment`：舆情分析

这里有个重要的架构原则：

**Agent 是任务执行主体，Skill 是可复用的能力单元，Tool 是连接外部世界的接口。**

例如：

Company Agent 可以调用 `parse_10k` Skill，Skill 再调用 SEC API、文档解析服务和财务数据库。

不是每一个 Skill 都需要配置成一个独立 Agent。

这也符合目前 Agent 工程实践中的一个趋势：优先使用简单的工具调用和工作流，只有在任务复杂度、上下文隔离或并行执行确实有价值时才引入 Subagent。

[image](https://www.google.com/s2/favicons?domain=https://claude.com\&sz=32)

Claude by Anthropic+2



## **L4：工具与数据服务层（Tools & Data Services）**

这一层决定投研 Agent 是否真的有能力进行专业研究。

主要包括：

| 数据/工具            | 具体内容          |
| ---------------- | ------------- |
| Market Data      | 实时行情、历史行情、成交量 |
| Financial Data   | 三大报表、财务指标     |
| News Search      | 新闻、公告、舆情      |
| Web Search       | 网络搜索、信息检索     |
| Filing Search    | SEC、交易所公告     |
| Quant Engine     | Python、金融计算   |
| Chart Engine     | K 线、技术指标      |
| Portfolio Engine | 组合收益、风险计算     |

特别强调：

**金融计算不要全部交给 LLM。**

例如：

- PE、PB、ROE 等指标：通过确定性程序计算。
- DCF：由估值引擎计算。
- 历史收益率：由行情数据库和计算引擎提供。
- 投资逻辑、商业模式分析：交给 LLM。
- 风险解释和研究结论：交给 LLM 综合判断。

这样可以避免模型产生看似合理、实际错误的金融数字。

## **L5：数据与知识层（Data & Knowledge）**

投研 Agent 的数据体系建议分成四类：

1. **结构化数据**：股票行情、财务报表、估值指标、宏观经济数据。
2. **非结构化数据**：研报、新闻、公告、电话会议纪要、财报原文。
3. **知识图谱**：公司、股东、供应商、客户、产品、行业之间的关系。
4. **用户知识库**：用户收藏的研报、历史研究、个人投资笔记。

需要特别注意三个技术模块：

- RAG：负责文档检索。
- Knowledge Graph：负责实体关系和关联分析。
- Temporal Database：负责时间序列和历史数据。

金融数据有一个非常重要的特点：**时间有效性**。

例如，2025 年的财务数据不能被误认为 2026 年最新数据。因此需要保存数据发布时间、报告期、入库时间、修订时间等信息。

## **L6：模型与基础设施层（Model & Infrastructure）**

这里包括：

| 模块            | 作用           |
| ------------- | ------------ |
| LLM Gateway   | 统一管理模型接入     |
| Model Router  | 根据任务选择模型     |
| Embedding     | 向量化          |
| Reranker      | 检索结果重排序      |
| VLM           | 图表、图片、PDF 理解 |
| ASR           | 会议录音转文字      |
| Agent Runtime | 执行 Agent 循环  |
| Queue         | 异步任务调度       |
| Cache         | 缓存和成本优化      |

模型路由可以这样设计：

- 简单查询：小模型。
- 信息抽取：性价比模型。
- 复杂分析：高推理能力模型。
- 财务计算：确定性代码。
- 图表理解：视觉模型。

不应该所有请求都调用最贵的模型。

# **三、真正的核心：投研 Agent 应该有哪些业务工作流？**

如果从产品角度看，以上六层只是技术架构。真正决定用户价值的是业务工作流。

我建议先设计五条主工作流。

**工作流 1：个股深度研究**

1. 用户输入研究目标
2. 识别公司及研究范围
3. 并行收集财报、新闻、行业数据
4. 基本面、竞争格局、财务分析
5. 估值计算与风险分析
6. 交叉验证研究结论
7. 生成结构化投资研究报告

**工作流 2：财报解读**

1. 获取最新财报
2. 提取关键财务指标
3. 对比历史季度和年度表现
4. 分析业绩变化原因
5. 提取管理层指引
6. 识别超预期和低于预期项目

**工作流 3：行业研究**

1. 行业定义与边界识别
2. 产业链拆解
3. 市场规模和增长预测
4. 竞争格局分析
5. 上下游公司映射
6. 行业投资机会和风险

**工作流 4：投资组合分析**

1. 读取用户持仓
2. 资产分布与行业暴露分析
3. 相关性和集中度分析
4. 回撤与风险分析
5. 情景压力测试
6. 生成风险提示

**工作流 5：持续监控**

1. 建立用户关注标的
2. 定时或事件触发数据采集
3. 识别重要变化
4. 判断是否影响原有投资逻辑
5. 推送个性化提醒

这五条工作流中，尤其值得关注的是最后一条：它能够让 Agent 从一次性问答产品，变成持续服务用户的产品。

AWS 有一个投资研究多 Agent 的参考实践，也采用了 Supervisor 调度新闻、量化分析和摘要 Agent 的架构，可以作为业务拆分的参考。

[image](https://www.google.com/s2/favicons?domain=https://aws.amazon.com\&sz=32)

Artificial Intelligence



# **四、还有一个容易被忽略的层：安全与质量治理**

投研 Agent 和普通聊天 Agent 不一样，它的输出可能直接影响用户的投资决策。

建议独立建设一套横向治理能力：

| 模块                    | 具体职责      |
| --------------------- | --------- |
| Fact Check            | 核实关键事实    |
| Source Citation       | 结论关联原始来源  |
| Data Validation       | 财务数据校验    |
| Hallucination Check   | 检查虚构数据和引用 |
| Investment Disclaimer | 投资风险提示    |
| Permission Control    | 用户数据访问控制  |
| Audit Log             | 研究过程和操作留痕 |
| Evaluation            | 研究结果质量评估  |

特别建议引入一个 **Research Evaluator Agent**：

它不负责生成报告，而负责检查报告：

- 关键数据是否有来源？
- 计算结果是否正确？
- 是否存在数据口径不一致？
- 结论是否有证据支撑？
- 是否遗漏重要风险？
- 是否把事实、预测和观点混在一起？

# **五、如果现在从 0 到 1 做产品，我会怎么落地？**

这里我给一个更偏实际工程实施的建议。

不建议第一版就搭建十几个 Agent，更不建议一开始就做复杂的 Agent Swarm。

可以分三个阶段：

**Phase 1：单 Agent + Workflow**

MVP

先验证核心研究价值

- 一个主 Agent
- 一个 Planner
- 5～10 个 Skills
- Search + RAG
- 金融计算工具
- 研究报告生成
- 基础引用与事实校验

**Phase 2：多 Agent 专业化**

提升研究深度和执行效率

- Company Agent
- Financial Agent
- Industry Agent
- Valuation Agent
- Risk Agent
- Supervisor 调度
- 并行研究与交叉验证

**Phase 3：Personal Agent**

从工具升级成长期个人助理

- 用户投资画像
- 长期记忆
- 持仓感知
- 主动监控
- 事件驱动研究
- 个性化推送
- 用户授权下的持续任务

# **六、我认为最值得关注的三个架构设计问题**

**1. Agent 和 Workflow 到底如何划分？**

我的建议是：

- 固定流程：使用 Workflow。
- 动态任务规划：使用 Agent。
- 专业研究任务：使用 Specialist Agent。
- 高频确定性操作：使用普通 API / Function。

不要为了体现 Agent 化而把所有功能都包装成 Agent。

**2. Memory 和 RAG 到底如何划分？**

这是 C 端消费级 Agent 很重要的产品差异：

- RAG 解决「系统知道什么」。
- Memory 解决「系统记得用户什么」。
- Context 解决「当前任务需要什么」。

三者应该是独立的系统模块。

**3. 如何从一次性对话升级成持续工作的 Agent？**

需要引入后台任务系统：

- Scheduler：定时任务。
- Event Listener：事件监听。
- Trigger Engine：触发条件。
- Task Queue：任务队列。
- Notification：消息推送。

这会让产品从 Chatbot 演进为真正的 Personal Agent。

# **七、总结：推荐的最终架构**

如果让我给一个 C 端投研 Agent 画技术架构图，我会采用下面这个组合：

## **Consumer Investment Agent**

推荐的目标架构

**Consumer Experience**

Chat · Research Workspace · Portfolio · Watchlist · Notifications

**Personal Agent Runtime**

Intent Router · Planner · Supervisor · Memory · Context Manager

**Research Agents**

Macro · Industry · Company · Financial · Valuation · Risk

**Action Agents**

Monitoring · Alert · Portfolio · Report · Scheduler

**Capability & Tool Layer**

Skills · MCP · Search · Financial API · Python · SQL · Browser

**Data & Knowledge Infrastructure**

Financial DB · Market DB · News DB · RAG · Knowledge Graph · User Memory

**Cross-cutting Infrastructure**

Model Gateway · Evaluation · Observability · Security · Governance · Cost Control

我的核心判断是：

**C 端 Agent 的竞争壁垒不在于有多少个 Agent，而在于能否建立「用户画像 + 专业数据 + 可验证的研究能力 + 持续主动服务」这套闭环。**

如果是做投研 Agent，尤其值得投入的是金融数据基础设施、研究工作流和用户长期记忆，而不是过早投入复杂的多 Agent 协作框架。

进一步阅读：

- [Anthropic：Building Effective Agents](https://www.anthropic.com/research/building-effective-agents?utm_source=chatgpt.com)
- [AWS：Investment Research Multi-Agent 实践](https://aws.amazon.com/jp/blogs/machine-learning/part-3-building-an-ai-powered-assistant-for-investment-research-with-multi-agent-collaboration-in-amazon-bedrock-and-amazon-bedrock-data-automation/?utm_source=chatgpt.com)
- [Multi-agent Orchestration 技术文档](https://platform.claude.com/docs/en/managed-agents/multiagent-orchestration?utm_source=chatgpt.com)

**还有一个值得深入讨论的问题：** 你如果是从产品和技术架构设计的角度做这个项目，我建议下一步把这套架构进一步拆成「产品模块图 + 后端微服务架构图 + Agent 执行时序图」，这样就可以直接进入技术方案和团队分工阶段。