# 历史案例与识别验收

这些案例用于规则开发和回归，不是独立准确率测试，不随每次生产请求发送全文。消息编号属于对应材料包，不能跨包引用；真实原始消息仍由后台当前权限校验。本手册停用不暂停身份或账号生产任务。

## 应识别的自然证据

| 案例 | 必要上下文 | 预期 |
| --- | --- | --- |
| Carla Packer | 私聊 M13 员工说明 TikTok 找不到其 UID；M16 本人 “It’s shop.with.Carla”；中间连续对话须保留；合作群为 `❤️ shop.with.carla X TT Media` | 支持该账号，不要求问题写成“你的 handle 是什么” |
| Jessica Rice | 群 `❤️ jessyrice94 X TT Media`；M1/M2 本人说账号被异地登录、商店链接丢失；M23 员工就该账号的视频索取广告码；M24 本人真实回复 M23 并交付 | 已知达人可用群名与本人账号、创作业务联合关联；新身份仍独立判断 |
| Robin Cope | 群 `💙robc825(tiktokshopdeals_uk)& TT Media`；M35 “my main…both accounts”；4 秒后 M36 “Robc825”；M37 再给另一账号，另有本人直播与内容履约 | 连读本人消息，分别判断两个账号；不能只读“my main”或凭括号副号继承 |
| Karen Gough | M3 为报名优惠券通知；M4 本人说明已报名但没有券；M5 同一回复链填 `beautywithkaz`；M45 本人谈视频销售 | 自然业务填名可支持，不伪造“员工直接问账号”的问题 |
| Sarah McFadzean | 本人先问样品申请；M2 对方询问 ID；M3 本人明确回复 “SarahDisneyMum”；另有活动用户名和本人直播时间填报 | 支持自然问答，不因 `your id` 句式或无“my account”降级 |
| Sabiha Khan | 本人 M5 “My Handel is sabihaara”，结合账号业务语境 | 容忍账号词错拼，不修改 handle 精确字符 |

上述案例来自当前保存的七份匹配材料包。它们证明这些表达能形成证据链，不证明包外所有历史、当前平台控制权或全库准确率。

## 必须拦截或保留不确定的案例

| 案例 | 关键证据 | 预期 |
| --- | --- | --- |
| 李莉娟与 `lucyhallett_` | Mona：“这个达人可以批准嘛 lucyhallett_”；李：“可以的 辛苦建联” | 商家审批他人，不支持本人账号；历史错误关联不能被自动恢复 |
| Stephanie Rogers 与 `clairesk8sss` | “introduce you to Claire (@clairesk8sss)”；签名 “Creator Manager…On behalf of Claire” | 代表 Claire，不将 Claire 的账号写给 Stephanie；不推断 Stephanie 永远不能兼任达人 |
| Furqan 与品牌账号 | 描述付费 White Label Creators 产视频，并 “on behalf of Seller” 上传品牌 TikTok | 代运营业务，不把“我们上传视频”直接认定本人达人账号 |
| Elise Denny 与 `ourunfilterednest` | “my friend is interested…Please could you add her” | 明确介绍朋友，不关联到介绍者 |
| Eileen O’Grady 的品牌邮件 | 本人说“this is the email I got”，邮件自称 Phinneus from Quince | 邮件角色属于作者；要求创作不等于本人已经完成交付 |
| Iman Hamid 的机构申诉草稿 | 草稿 “on behalf of our agency”；上下文说明可复制修改，本人希望恢复自己的账号 | 分开判断本人诉求与代写草稿，不能给本人强加机构身份 |
| Fatima Amjad 的培训通知 | 发文人是 Fatima，通知正文自称 “I’m Janette…training assistant” | 识别通知、模板或转贴；文中第一人称不自动归给发送者 |
| Roxanne Freeman 的邮箱 | 员工明确问 email，下一条提供邮箱 | 邮箱前缀不是本人 TikTok 账号确认，不篡改真实问答含义 |
| Devin-Jade Eames 的商品 URL | 佣金商品链接包含 `unique_id=devinjadex` | 只能召回，不单独证明账号归属；也不否定既有达人身份 |
| Teresa Basquine 的新旧名称 | 群含 `teresa_serena (teresa_official75)`；M13 旧名称渲染为人物占位符；M24 本人明确 main 为 Teresa_serena | 支持新名称；旧号状态保留缺口，既有正式关系不自动删除，不直接把两名称合并成同一平台账号 |

历史原始出处：`reports/creator-rule-research-2026-09-27/missing_handle_current_raw.json`、`full_population_raw.json`、`full_results_20260927/corpus.json`。目录为本次已有研究资料，不作为可公开下载的生产资源。

## 验收要求

1. 导入与新人对相同完整材料给出一致的身份、账号含义；入口只影响候选发现和负责人业务声明。
2. 新人没有 handle 但有本人创作证据，可以确认身份；没有消息留等待材料，不判非达人。
3. 仅私聊有账号证据能够进入目标流程；不能先要求找到群。
4. 同一人已关联账号不重付费，新账号仍能处理；同号涉及别人不合并人员。
5. 一弱候选或一条无关残缺问答，不拦截其他完整有效候选；关键反证确实缺失则拦截受影响项。
6. 权限撤销、材料编辑或人工拒绝发生后，旧结果不能写库；重复提交不生成重复关系。
7. 身份确认不直接归给发现者；确有负责名单或负责人结论才添加相应 CM。
8. 后端入库后按条件触发七天事件补算；保存手册、账号补配不触发全库重跑或自动发送。

独立验证集另选未参与这些规则修订的联系人，并包含助理、商家、代运营、无消息和改名样本。同一账号、群、转让关系保持在同一数据分组，避免开发集与验证集共享上下文。

分别报告身份确认精确率、账号关联精确率、可判定覆盖、待材料、待核实及错误入库数；自动写入准确性优先，不能把模型自评分或已有数据库标签当测试真值。规则变更先核对固定案例及权限、幂等，再定向验证新样本，不默认全库重跑。
