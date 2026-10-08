# 达人重要度排行调研与技术方案

日期：2026 年 10 月 8 日。状态：技术建议，尚未开发或上线。

建议采用**三档重要度＋可比较的商业指标排序**，复用 Python、PostgreSQL 和现有 Worker，不新增评分模型或独立服务。重要度回答“谁值得长期投入”，消息总结回答“当前先处理什么”，两者不能混成一个分数。

## 推荐方案

| 功能 | 规则 | 放在哪里 |
| --- | --- | --- |
| 达人重要度 | 重点、常规、待评估；显示依据和数据日期 | 我的达人，增加筛选和排序 |
| 商业表现排序 | 在同一数据口径内比较账号 30 天 GMV | 同一名单的可选排序，不叫公司收益榜 |
| 处理顺序 | 当前所选时段内，未处理的 P0 优先于 P1、P2；同级才参考重要度 | 现有消息总结，不另建待办系统 |

例如：常规达人发生付款纠纷，应先于重点达人的普通样品咨询处理。排行榜不改变身份、负责人、正文访问或发送权限。

沿用 PRD v1.5 的 F04、A21–A23：首版三档，不输出精确总分，缺数据不补零。同档商业排序属于本次提出的增补；旧文档中的 40／25／20／15 权重仍只用于后续试验。

## 当前数据和限制

以下为 10 月 8 日生产库与归档的只读核验结果。

| 数据 | 当前情况 | 对方案的影响 |
| --- | --- | --- |
| 正式达人 | 1,463 人；807 人有正式 TikTok 账号，共 877 条人账号关系 | 按人展示，账号指标单独保存 |
| 负责人 | 583 人有未结束的负责关系，共 714 条人 CM 关系 | 一人可有多个 CM；关系未结束不等于 CM 当前授权有效 |
| Curva 底表 | 8 个 CSV，63,017 行、61,600 个去重 handle；877 条正式账号关系中 740 条命中、137 条未命中 | 未收录的人工或其他来源账号继续保留，不当作零业绩 |
| 商业指标 | 原文件有 GMV、粉丝、类目等，但当前系统只加载 handle；没有经营指标或重要度业务表 | 先规范导入，不能把原文件已有字段说成系统已经可计算 |
| 消息总结 | 按达人、CM、日期和时段生成，加密保存 P 级与截至时段的处理进展 | 可辅助处理顺序，不是跨日期的长期事件账本 |

**商业排行目前还缺可验证的数据口径。** 现有 CSV 没有币种、指标截止日或观测日期；文件名和上传日期不能代替这些字段。另有两个重复 handle 的 GMV 不一致，不能取最大值或猜测哪个更新。

先向导出来源确认市场、币种、统计窗口、指标定义和有效期，保存到批次元数据。无法确认的旧值只作为历史参考，不进入当前商业排行。平台 GMV 是销售额，不是达人报酬，也不是本公司收入。

## 第一版规则

### 重要度分层

| 条件 | 结果 |
| --- | --- |
| 有人工设置 | 保留人工的重点、常规或待评估；依据到期标需复核，自动计算不覆盖 |
| 没有人工设置，但商业资料满足可比条件 | 常规，标注“外部数据初评” |
| 没有足够资料 | 待评估；仍可由 Owner 根据明确的合作事实设为重点 |

第一版不强行制造固定比例的重点达人，也不凭聊天数量、群数量、催促或身份识别置信度升档。重点依据可以是明确商业价值、类目合作机会或重要项目，由 Owner 记录具体依据及复核日期。

后续需要自动升重点时，增加一个 Owner 核准的商业门槛即可，例如同口径 30 天 GMV 达到指定金额。结果标“外部数据初评”，不覆盖人工等级。金额须由业务样本校准，不把全库前 10% 或某个厂商分数当行业标准；需结合关系、潜力和当前项目的综合评估，仍要补齐相应事实。

### 排序与多账号

- 默认按重点、常规、待评估分组；同档可按可比商业指标排序。待评估不是低价值或商业榜末位，缺指标的人员不显示商业名次，仍可正常查看和人工评估。
- 多账号先展开显示各账号指标。首版使用“最佳账号 30 天 GMV”，即该人已确认账号中的最大值，保留对应 handle；它不是个人总 GMV。全部已确认账号在选定口径下资料齐全时才参与这个排序，否则标“账号资料不全”。
- 金额排序只比较同市场、币种、统计窗口和指标定义的有效数值。空值、区间值和冲突值不伪造精确金额；真实记录的零才是零。同值并列，以稳定人员 ID 保证分页顺序。
- 不把直播 GMV 再加到总 GMV，不把改名或重复导出算成多个账号。以后确认账号互不重复且覆盖完整，再考虑个人汇总。
- 展示本筛选范围的可比较人数和数据日期。公司榜按人去重；一个达人由两个 CM 负责，可以分别出现在两人的名单，但公司榜不计两次。

当前数据库限制一个 TikTok 账号只能有一个正式 owner。代表人关系不能作为本人经营指标参与计算；不能按历史设想擅自改为多人共享 owner。

### 事件优先级

在消息总结中复用当前所选 CM 和时段的合法结果，按未处理 P0、P1、P2 排序，同级再参考重要度。待核实事项单列；已处理或无需处理不挤占未处理事项。

显示“截至该时段的进展”，不称为实时待办。未完成、失效或缺材料的总结不能解释成没有问题；旧事项没出现在新时段，也不能推断已经解决。暂不增加跨日投诉次数、真实等待时长或 SLA 排序。

## 技术落地

### 最小存储

新增两张表，审计继续使用 `audit_events`。

| 表 | 最小内容 |
| --- | --- |
| `creator_metric_snapshots` | 标准化 handle、市场、来源批次、币种、统计窗口、观测日期、有效期、GMV、粉丝、类目、质量状态及原档引用 |
| `creator_importance` | 联系人 ID、自动基础档、人工等级与依据及复核日期、最佳账号快照引用、资料缺口、规则版本、输入指纹、计算时间和待重算标记 |

商业表按来源批次和 handle 去重，金额用 PostgreSQL `NUMERIC`／Python `Decimal`。全量底表不必先创建正式达人或账号关系；仅通过已确认的 owner 关系连接到人员。重复同值合并，冲突字段隔离，原档保留。

公司级分层只采用外部指标和公司明确共享的合作事实。聊天衍生的 P 级、摘要和私人证据不写入全局重要度行，也不在其他 CM 的排名理由中显示。

### 更新和读取

1. 商业数据导入、账号确认或解绑、人工分层变化时，事务内标记受影响的人待重算。
2. 现有 Worker 分批领取并计算，按输入指纹幂等写回；同一输入不重复处理。计算期间输入再次变化时保留待重算标记，避免旧结果覆盖新关系。
3. 每日检查指标有效期；过期值可保留历史展示，但退出当前商业排序。人工等级保留，依据失效时提示复核。
4. “我的达人”在分页前按已存结果排序并批量读取账号；不在打开页面时扫描 CSV、全文聊天或调用模型。账号解绑或身份纠错后，读取时排除不再有效的快照关联，不能等 Worker 才挡住旧指标。

名单接口沿用 `/creators`，仅增加等级筛选和白名单排序参数。人工调整只需一个 Owner 专用 POST 接口，校验登录、CSRF、等级、依据和复核日期，并记录修改前后值；不增加一套新角色系统。

消息总结复用 `get_summary_view` 和既有证据复核，在合法读取整个时段后内存排序。**当前没有分页或联系人批量过滤接口，加密 JSON 不能直接 SQL 排序。** 首版不新增跨 CM、跨日期的混合事件榜；规模实测需要时，再增加非正文的结构化索引，展示前仍复核原授权和材料。

排序计算不新增模型调用，不需要加入 API Key 列表，也不占用现有付费 AI 队列。不改现有 AI 功能开关。

### 页面和权限

在“我的达人”增加一个等级角标和“重要度／30 天 GMV”排序，保留现有负责人筛选。详情中展开最多两条主要依据、账号指标和更新时间，不堆分数雷达图。

普通 CM 只看本人名单；Owner 和既有跨 CM 权限按原范围查看。查看排名不增加聊天权限，普通用户不能修改公司级分层。排名靠前也不自动获得群发、自动回复或服务豁免。

## 验证和升级顺序

先做离线对照，不直接调整业务名单。抽取约 200 位有不同资料覆盖、类目和合作状态的达人，由业务负责人标档并比较优先顺序；阈值校准和验证使用不同样本。看重点名单的人工认可率、漏掉重要合作的比例和资料覆盖率，不把“有分数的人变多”当成功。

验收必须包括：缺值不等于零、混币种和跨窗口不比较、重复与多账号不叠加、人工等级不被覆盖、纠错后旧指标失效、两个 CM 的总结不串用、常规 P0 先于重点 P2、缺总结不当作无事。

建议顺序是：**人工三档和依据 → 规范商业指标与排序 → 校准自动重点规则 → 补齐公司真实合作收益和履约结果 → 再决定是否需要综合评分。** 长期事件账本、共享结单和 SLA 属于后续事件流程，不为做排行提前建设。

## 产品对照与来源

以下对照 17 个产品或平台的官方说明，涵盖达人发现、商业绩效、客户价值、客户健康和服务优先级。先比较机制，再核查缺失数据、时效与误排风险，最后对照本系统的数据和权限边界。厂商公式只在明确公开的范围内引用，不将产品宣传当作效果验证。

### 达人平台

| 产品 | 已公开做法 | 借鉴与边界 |
| --- | --- | --- |
| [TikTok Shop UK](https://seller-uk.tiktok.com/university/essay?knowledge_id=5104634859849474) | 销售、增长、内容、产出、合作表现分榜；可选类目和周期，合作榜看回复与样品履约 | 分维度和时间窗口。该说明发表于 2024 年，不代表已实测今日所有账号界面 |
| [Modash](https://www.modash.io/features/influencer-discovery) | 按受众和内容筛选；[销售追踪](https://www.modash.io/features/influencer-tracking)依赖店铺、链接或代码 | 适配与实际归因结果分开；[受众画像](https://help.modash.io/en/articles/13715083-understanding-audience-demographics-and-insights)是估算，不等于公司收益 |
| [CreatorIQ](https://www.creatoriq.com/influencer-marketing-solution/creator-search) | 内容、受众、品牌适配和安全筛选 | 先回答适合哪项合作，不照搬未公开总分或从相似度推断负责人 |
| [HypeAuditor](https://blog.hypeauditor.com/what-is-audience-quality-score-on-hypeauditor/) | Instagram AQS 看互动、真实受众、增长异常与互动真实性；[跨平台报告](https://hypeauditor.com/reports/)另有 TikTok Account QS | 账户质量独立展示，不能把 Instagram 公式称为 TikTok 重要度公式 |
| [Upfluence](https://www.upfluence.com/analytics/) | 按归因收入、点击、转化、成本等排行；[CRM](https://www.upfluence.com/creator-relationship-management/)另管关系和跟进 | 绩效与工作流分开；互动不能单独预测销量，归因收入也不覆盖全部购买 |
| [GRIN](https://launch.grin.co/influencer-marketing-platform) | 官网描述公开表现和品牌交易数据两层评估，保留品牌关系层 | 外部能力与本公司合作结果分开；不复制未公开权重 |
| [Traackr](https://www.traackr.com/influencer-marketing-blog/leaderboard-faq) | VIT 综合可见度、互动和品牌内容，用于内容、活动及品牌比较 | 固定比较对象；传播表现不是净收益或事件紧急度 |
| [Cruva](https://cruva.com/new-features/reputation-metrics) | 六项 reputation 指标覆盖质量、样品、产出和销售；另存自己的合作关系 | 2026 年 10 月 1 日说明为 US-first，不能当作我们的 UK 文件已有字段 |

Cruva 的 [Content Insights](https://cruva.com/help-center/content/content-insights)特别提醒：效率倍数不考虑样本大小，小样本可能出现很高的倍数。因此增长和转化要展示分母，不能只看百分比。[TikTok US 合作评价规则](https://seller-us.tiktok.com/university/essay?knowledge_id=7371703592568618)也区分商家原因；不能把缺货或商家取消计成达人不履约，该规则不直接当作 UK 的已验收机制。

数据更新可先用规范 CSV，不必立即采购 API。Cruva 的[官方 API 文档](https://www.cruva.com/docs/api)提供 Enterprise 档的 UK handle 精确查询，以及指定日期窗口的达人品牌、商品查询；可作为后续更新渠道，但不能据此给旧 CSV 猜币种或截止日。

### CRM 客户价值和服务优先级

| 产品 | 已公开做法 | 借鉴与边界 |
| --- | --- | --- |
| [HubSpot](https://knowledge.hubspot.com/scoring/understand-the-lead-scoring-tool) | 匹配度和活跃度分开；支持分组封顶、扣分、事件衰减和发布前预览 | 防止活跃刷分，旧事实不能永久加分；营销点击不是合作结果 |
| [Salesforce Einstein](https://help.salesforce.com/s/articleView?id=ai.einstein_sales_lead_insights.htm&language=en_US&type=5) | 基于历史转化排序，显示影响因素；自有模型有[数据要求](https://help.salesforce.com/s/articleView?id=ai.einstein_sales_data_requirements.htm&language=en_US&type=5) | 要真实成败标签，不拿聊天数量或达人识别置信度训练重要度 |
| [Dynamics 365](https://learn.microsoft.com/en-us/dynamics365/sales/work-predictive-lead-scoring) | 分数、等级、趋势和原因；趋势允许信息不足，训练有[前提](https://learn.microsoft.com/en-us/dynamics365/sales/configure-predictive-lead-scoring) | 保留等级、日期和未知状态，不把分数冒充成功概率 |
| [Pipedrive](https://support.pipedrive.com/en/article/scores) | 条件加减分，数据变化重算，解释贡献因素 | 最接近轻量确定性规则；其对象是交易，不把每个群当一个达人 |
| [Gainsight](https://support.gainsight.com/gainsight_nxt/05Scorecards/02Admin_Guides/Configure_Scorecards) | 客户或关系健康度，自动或人工汇总，有效期与例外 | 健康不等于价值；其过期指标仍参与计算，不能只抄日期标签 |
| [Shopify](https://help.shopify.com/en/manual/reports-and-analytics/shopify-reports/report-types/default-reports/customers-reports) | RFM 用购买时间、订单频次、消费额分组；未购买者单列 | 借鉴分层，不把消息次数代替订单次数，不把新达人当低价值 |
| [Intercom](https://www.intercom.com/help/en/articles/6989006-inbox-sorting) | 会话按等待、活动、优先级或 SLA 排序，真人等待与机器人活动不同 | 今天先处理谁与长期价值分开；摘要生成时间不能冒充等待开始时间 |
| [Zendesk](https://support.zendesk.com/hc/en-us/articles/6712096584090-Understanding-how-omnichannel-routing-uses-queues-to-route-work-to-agents) | 队列按优先级、时间或 SLA 排序，再考虑员工容量分配 | 排序与人员分配分开；CM 忙不代表达人价值低 |
| [Jira Service Management](https://support.atlassian.com/jira-service-management-cloud/docs/how-impact-and-urgency-are-used-to-calculate-priority/) | 影响与紧急程度共同决定事件优先级 | 高影响但远期的事项不必最急，情绪词不能单独触发最高级 |

Gainsight 对 [NA 权重](https://support.gainsight.com/gainsight_nxt/05Scorecards/02Admin_Guides/Group_Weights_and_Exceptions)有重分配机制，我们不照搬：资料少但仅有一项好指标的人可能因此虚高。缺失保留未知，完整度与业务判断分开。

## 项目依据

当前核验代码为 `ad4fcf1`，业务数据为 10 月 8 日快照，不代表以后实时数量。

- [名单与按人去重](../../backend/app/creator_roster.py)：现按姓名排序，复用人员和 CM 范围。
- [账号 owner 唯一约束](../../backend/migrations/028_creator_account_workflow.sql)和[多个 CM 关系](../../backend/migrations/068_multiple_creator_cms.sql)：账号主人与负责人是不同关系。
- [Curva 加载器](../../backend/app/creator_handle_matching.py)：当前仅加载 handle，没有经营指标计算。
- [消息总结读取](../../backend/app/offhours_summary.py)和[合并结果证据复核](../../backend/app/summary_dialogues.py)：当前为加密的时段结果，不是可直接排序的明文业务账本。
- [消息总结规则](../product/message-summary.md)与[权限边界](../product/admin-access.md)：排名不得扩大当前授权。

PRD 依据为工作区的 `TTMedia_Lark_Creator_Management_PRD_v1.5.md` 中 F04 与 A21–A23。旧 `creator-management.md` 的 CAP 阶段说明不作为当前功能状态依据。
