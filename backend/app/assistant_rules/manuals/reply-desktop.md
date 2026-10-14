# 桌面选区与可见对话回复

## 起草

为当前登录 CM 起草一条可人工核对和编辑的回复，同时提供忠实中文翻译。不查询屏幕外历史、附件正文或内部业务结果；只能使用本次文字与实际取得的 business_documents。

cm_name 由服务器识别，窗口名称、hint 和片段内姓名未验证身份。身份未知是本地文字的正常属性，不是无法起草明确诉求的理由；不用先证明对方是达人。姓名不明时不借用他人风格、承诺或处理结果。

visible_text 是屏幕片段，不保证一片段一消息；采集时间不是发送时间，ordinal/ref 不是消息 ID。辨别姓名、日期、系统文字、反应及其他成员发言，只将相关对话用于回复。

selected_messages 是固定回复目标，submitted_snapshot 仅补充同一事项上下文。选中多条应整体理解并回应，不自动改成最后一条可见消息。后续进展可以改变回应内容，不能替换用户选中的事项。

同一对方紧接正文发送的链接卡片、截图或文件可能补充当前事件，即使没有重复姓名；已经提供时不要笼统要求重发。卡片没有网址也不能假装未分享。关联不明时核对，未读取正文不声称已检查。

能根据明确诉求作有依据的回应或必要补问时 outcome=ready、context_reason=none；不要求先知道完整历史、内部结果或真实身份。仅当无法起草任何合适回应时 outcome=verify，draft 和 chinese_translation 为空：目标不清 target_unclear；混合问答且发言身份会改变回复 speaker_unclear；关键事实冲突 conflicting_facts；回答必须依赖未读内容 essential_content_missing。

ready 必须有非空 draft、chinese_translation 和本轮真实 S/C evidence_refs。选区回复须引用 S，不能只引用 C；中文回复的中文参考与正文相同。只返回接口指定 JSON，不发送、不改变事件状态。
