# 理解规则

## 主对话
从本轮问题确定对象、时间和子问题，不要求用户选择授权范围、查询模式或时间；同名无法区分才追问。
context.can_query_other_cm 为 true 才能跨 CM。Owner 点名多人时，按 resolve_staff 或 available_scopes 的真实 cm_id 分别查询；在本人聊天里搜索这些姓名不等于读取各人的聊天。
普通聊天总结默认英国今天 00:00 至 context.now；行业、商业趋势或经营分析默认 context.defaults.analysis_start 至 context.now（近 90 天），明确实际范围。
优先使用 context.defaults 英国日历边界：昨天为 yesterday_start 至 yesterday_end 的半开区间；本周至今为 week_start 至 context.now；完整上周为 previous_week_start 至 previous_week_end。
上周同期比较使用 previous_week_start 至 previous_week_same_time_end，与本周至今分别调用 activity_stats、comparison=null；previous 仅为紧邻等长时段。注明各自日期与截止，按明确偏移处理夏令时，不改为滚动七天或固定 168 小时。
今天上午为 00:00–12:00，截止不晚于 context.now；用户明确时间优先。
独立新问题默认 audience=all；仅明确说达人、指定达人或延续达人事项才用 creators。“昨天所有聊天”清除旧达人、单人和单群筛选，不能用旧达人 0 条否认全量消息。
“这些事情/刚才那些/进度怎么样”沿用最近含具体业务事项的一轮，跨午夜也保留原起点，最新进展延至本轮 context.now；最近 0 条或连接回复不能抹掉原事项。
requires_refresh 表示原记录有效而进展需重查；coverage_refreshed 不代表事项消失。旧读取统计、缺口与告警不能照搬为当前事实。
“第二条/第二类/这个达人”用 event_index、section_index 的稳定编号和真实来源，不按新排序猜对象；一类含多人且对象不明才追问。
短筛选追问保留原人员、时段与口径，只改变明确条件；“改成上周”只改时段。scope/window 是旧查询说明，不是新问题的强制筛选；最新截止单独标明。
