# 达人维护优先级：规则与实现

日期：2026 年 10 月 8 日。v2.0 将“历史合作成熟度”改为“CM/AM 现在应该重点维护谁”。本次先暂停本功能，完成验证后重新提取；其他 AI 功能、业务关系和授权不变。

仅使用已授权聊天，不依赖 Curva、GMV、粉丝数或外部经营数据。AI 提取有原文引用的业务事实，Python 决定档位；不输出综合分或让模型直接排序。

## 判断规则

| 维度 | 作用 |
| --- | --- |
| 近期业务内容 | 主要依据：具体合作机会、报价、直播安排、视频交付及实质推进 |
| 有效互动频率 | 同档辅助：从双方业务证据的原文时间派生不同交流日期，不按消息条数计分 |
| 最近业务时间 | 近期推进优先于旧交流；不使用模型分析时间 |
| 历史合作 | 最后背景参考，旧合作不能永久占据前列 |

聊了很多问候不一定重要；只聊几条，但已在确认具体付费直播，也可以成为重点。投诉、催款、申诉和退出机构进入独立事件紧急度，不凭语气或催促次数抬高维护优先级。

| 档位 | 条件 |
| --- | --- |
| 重点 | 近 30 天有真实、仍有效的具体双边机会或交付安排、实质推进；不要求先证明长期合作，也没有消息数门槛 |
| 常规 | 完成评估且有完整可读的受控正文，但没有上述近期重点事项；包括一般支持、旧合作、已完成交付和只有意向 |
| 待评估 | 尚未完成、没有完整可读正文或来源已失效；不表示低价值 |

人工档位优先。完结、取消或真实冲突只关闭对应业务对象；其他有效对象保留。沉默不证明合作取消，但旧机会超过窗口不再自动列为当前重点。

默认排序：**档位 → 7 天／30 天时效带 → 近 30 天业务交流日期数 → 最近业务时间 → 历史合作 → 姓名和稳定 ID**。分页前在数据库排序，不现场扫描聊天或调用 AI。档位使用滚动 30×24 小时；频率按 Europe/London 最近 30 个自然日期去重。该频率是已提取证据覆盖的业务交流日，不冒充全部聊天的精确频率。

## 事实与取材

- 初次升级重新读取有效授权内近 90 天背景。旧 v1 的自动事实不直接继承，避免保留此前的语义误判。
- 此后按新消息增量更新，合并更新信号、复用相同输入缓存；不每天全库重跑模型。
- 只使用该达人和所选 CM 的授权来源。显式回复补原问题，普通问答最多补四条同会话前文；每批仍最多 80 条、24,000 字符。
- 业务对象必须具体。单方广播、群名、寄样、优惠券和机构管理关系不等于成立合作；其他人或其他公司的成果不能算到此人。
- 具体机会和有效互动须有双方证据。完成还须连接成立合作，以及本人实际发布／执行完成的正向见证。一个历史批次可以包括“双方确认后数日完成”；裸链接、ad code 和未来承诺不是完成。
- 空增量不删除原事实。源消息编辑、删除、撤权或身份改绑后，立即使涉及结果失效，定向重新准备材料。
- 每个“达人 × CM”独立评估，不把多个 CM 的聊天相加，也不增加正文权限。

可执行指令、正反例和协议见[维护优先级事实提取协议 v2.0](../specs/creator-importance-facts-prompt_v1.0.md)。结构化输出只约束格式，不能保证业务判断正确，因此仍保留语义校验、原文核验和人工调整。

## 最小实现和页面

沿用 Python、PostgreSQL、现有 Worker、权限和独立 Key，不新增服务或依赖。继续使用三个已有表：

| 表 | 用途 |
| --- | --- |
| `creator_importance_jobs` | 历史／增量队列、游标、租约、加密检查点、重试 |
| `creator_importance_profiles` | 自动结果、人工覆盖、加密事实、当前来源及排序元数据 |
| `creator_importance_requests` | 请求 ID、错误、token 用量和加密结构化返回；失效结果也保留真实费用 |

展示为“维护优先级”，底层功能键和接口保持兼容。我的达人默认按优先级排序，只显示**重点／常规／待评估**角标。展开最多两条依据和资料截止；Owner 调整折叠展示。仅有跨 CM 名单权限不授予依据、筛选或排序权限。

## 验证与发布边界

升级前暂停本功能并验证完整备份。只重置自动画像、任务材料与历史游标；人工档位、Key、请求用量、达人／handle／负责人关系及其他功能配置保留，并在事务中对比。

本轮修改前的线上快照：2026-10-08 14:50 UTC，本功能关闭，714 组任务中完成 100、取消 614，111 份画像、0 个人工档位，未完成请求为 0。这是旧版中间状态，不是新规则的全量结果。

独立原文核验覆盖 8 组“达人 × CM”，近月 233 条双方材料、去重 226 条来源；另补读一组旧合作 4 条消息。59 条带附件、正文或提及解析缺口，不代表全部无文字。人工先判断业务情境，再与算法对照，不根据旧档位倒推。重点案例包括具体视频投流问答及未结束的视频安排；负例包括券／申诉支持、已交付的三视频合作和几个月前的旧合作。证据不足不能被拼成新机会。

本地 139 项相关测试及 1 项浏览器工作流通过，桌面和手机布局已检查。v2.0 已发布，Web／Worker 和健康检查正常，发布备份通过完整读取及 SHA 校验。全量功能仍暂停，小样本不发布画像。

独立 Key 的 8 次有界原文回归均通过结构与来源校验，但 3 例有业务偏差：通用平台补贴被误作机会、同批交付的对象改名使完结未连接、索投流码误作整个投流已完成。5 例档位符合人工预判。输入 43,874、输出 1,274 tokens；这些片段不是 90 天全文分析，也不能据此宣称全库准确率。

Prompt v2.0.1 定向修正上述边界，规则代码不变：平台权益支持不升重点；已发布源视频的双边索码／给码是投流机会，不是投流完成；同批同一交付严格复用业务对象名称，完成后的款项／链接核查不重新激活。只重测受影响样本后再重新初始化，费用按请求账本记录。

保留已验证的旧版备份和用量历史，不删除业务数据。v1 曾把机构管理、周期价格支持或完成问答误判为合作，本次协议及回归覆盖这些问题；旧“只有持续／再次合作才重点”和“30 天不衰减”的规则不再沿用。

## 产品对照与来源

以下保留 17 个产品或平台的市场对照，**不是本功能的数据依赖**。聊天版借鉴 CRM 的事实分层、衰减与可解释规则，以及客服产品的重要度与紧急度分离；不采用外部销售额、粉丝画像或经营指标。厂商公式只在明确公开的范围内引用，不将产品宣传当作效果验证。

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

Cruva 的数据更新和 API 不纳入本方案；已有 handle 关系继续作为身份关联，不作为重要度输入，也不因本次选型删除。

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

原有线上能力与业务数量的核验基线为 `ad4fcf1` 和 10 月 8 日快照，不代表以后实时数量。第一版实现于 `c6fc834` 发布。

- [名单与按人去重](../../backend/app/creator_roster.py)：维护优先级默认排序，保留姓名排序、筛选及原人员和 CM 范围。
- [合作重要度队列与读取](../../backend/app/creator_importance.py)、[事实校验与分档](../../backend/app/creator_importance_facts.py)及[数据迁移](../../backend/migrations/087_creator_importance.sql)：历史分批、增量、缓存、来源失效及三张业务表。
- [多个 CM 关系](../../backend/migrations/068_multiple_creator_cms.sql)：合作画像须按 CM 授权范围隔离，不能按负责人数量加分。
- [消息总结读取](../../backend/app/offhours_summary.py)和[合并结果证据复核](../../backend/app/summary_dialogues.py)：当前为加密的时段结果，不是可直接排序的明文业务账本。
- [消息总结规则](../product/message-summary.md)与[权限边界](../product/admin-access.md)：排名不得扩大当前授权。

PRD 依据为工作区的 `TTMedia_Lark_Creator_Management_PRD_v1.5.md` 中 F04 与 A21–A23。旧 `creator-management.md` 的 CAP 阶段说明不作为当前功能状态依据。
