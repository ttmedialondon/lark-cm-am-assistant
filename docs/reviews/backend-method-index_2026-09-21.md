# 后端方法与直接调用索引

日期：2026-09-21；源码基线：f0dee27。

范围：backend/app 顶层 33 个 Python 文件，共 613 个函数/方法定义，包含类方法、路由工厂内函数和其他嵌套函数；不含测试、迁移、脚本、模板、JavaScript。下列调用由 Python AST 静态提取，包含库调用；不是运行时追踪，也不保证动态分派、函数别名、回调参数和隐式调用的完整解析。尤其 run_in_threadpool/executor.submit 的业务函数是参数，需结合主报告查看实际链路。没有调用不代表可以删除。


## backend/app/admin_permissions.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `_access` | [35](../../backend/app/admin_permissions.py#L35) | `bool`、`frozenset`、`len`、`next`、`row.get` |
| `users_access_data` | [56](../../backend/app/admin_permissions.py#L56) | `_access`、`conn.execute`、`conn.execute(…).fetchall`、`connection`、`results.update`、`set`、`sorted` |
| `access_for` | [84](../../backend/app/admin_permissions.py#L84) | `users_access_data` |
| `permission_keys_for` | [90](../../backend/app/admin_permissions.py#L90) | `access_for` |
| `set_permissions` | [94](../../backend/app/admin_permissions.py#L94) | `LookupError`、`PermissionError`、`ValueError`、`accounts.get`、`accounts.get(…).get`、`actor_role.get`、`any`、`conn.execute`、`conn.execute(…).fetchall`、`conn.transaction`、`connection`、`isinstance`、`json.dumps`、`len`、`list`、`roles.get`、`roles.get(…).get`、`set`、`sorted`、`str`、`uuid4` |

## backend/app/admin_reporting.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `admin_role_for` | [154](../../backend/app/admin_reporting.py#L154) | `conn.execute`、`conn.execute(…).fetchone`、`connection` |
| `overview` | [169](../../backend/app/admin_reporting.py#L169) | `_decorate_job`、`_decorate_lark_admission`、`_decorate_user`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`connection`、`int`、`runtime_settings` |
| `runtime_settings` | [340](../../backend/app/admin_reporting.py#L340) | `_seconds_label`、`get_settings` |
| `normalize_user_permission_filter` | [360](../../backend/app/admin_reporting.py#L360) | — |
| `message_users` | [364](../../backend/app/admin_reporting.py#L364) | `_decorate_user`、`_page_result`、`_page_window`、`conn.execute`、`conn.execute(…).fetchall`、`connection`、`len` |
| `message_user` | [377](../../backend/app/admin_reporting.py#L377) | `message_users` |
| `users` | [382](../../backend/app/admin_reporting.py#L382) | `_decorate_user`、`_page_result`、`_page_window`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`connection`、`int`、`normalize_message_conversation_query`、`normalize_user_permission_filter`、`sorted` |
| `user_detail` | [483](../../backend/app/admin_reporting.py#L483) | `_decorate_conversation_membership`、`_decorate_job`、`_decorate_user`、`_page_result`、`_page_window`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`connection`、`int` |
| `chats` | [664](../../backend/app/admin_reporting.py#L664) | `_decorate_chat`、`_page_result`、`_page_window`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`connection`、`int` |
| `jobs` | [746](../../backend/app/admin_reporting.py#L746) | `_decorate_job`、`_page_result`、`_page_window`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`connection`、`int` |
| `contacts` | [863](../../backend/app/admin_reporting.py#L863) | `_decorate_contact`、`_identity_summary`、`_page_result`、`_page_window`、`activity_by_id.get`、`conn.execute`、`conn.execute(…).fetchall`、`connection`、`int`、`normalize_contact_category`、`normalize_organization_query`、`normalize_organization_status` |
| `_identity_summary` | [952](../../backend/app/admin_reporting.py#L952) | `any`、`dict.fromkeys`、`int`、`row.get` |
| `organization_sync_status` | [979](../../backend/app/admin_reporting.py#L979) | `_format_time`、`conn.execute`、`conn.execute(…).fetchone`、`connection`、`dict`、`item.get`、`labels.get` |
| `contact_detail` | [999](../../backend/app/admin_reporting.py#L999) | `_decorate_contact`、`_decorate_contact_conversation`、`_decorate_contact_organization`、`_decorate_lark_identity`、`_page_result`、`_page_window`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`connection`、`int` |
| `normalize_message_conversation_query` | [1149](../../backend/app/admin_reporting.py#L1149) | `<BoolOp>.strip` |
| `normalize_message_conversation_type` | [1155](../../backend/app/admin_reporting.py#L1155) | — |
| `normalize_contact_category` | [1161](../../backend/app/admin_reporting.py#L1161) | — |
| `normalize_organization_status` | [1167](../../backend/app/admin_reporting.py#L1167) | — |
| `normalize_organization_query` | [1173](../../backend/app/admin_reporting.py#L1173) | `<BoolOp>.split`、`<Constant>.join` |
| `message_conversations` | [1179](../../backend/app/admin_reporting.py#L1179) | `_decorate_message_conversation`、`_page_result`、`_page_window`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`connection`、`int`、`normalize_message_conversation_query`、`normalize_message_conversation_type` |
| `message_conversation` | [1226](../../backend/app/admin_reporting.py#L1226) | `_decorate_message_conversation`、`conn.execute`、`conn.execute(…).fetchone`、`connection` |
| `message_page` | [1243](../../backend/app/admin_reporting.py#L1243) | `_decorate_message`、`_page_result`、`_page_window`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`connection`、`get_settings`、`int`、`mention_names`、`message_body_cipher`、`reversed` |
| `record_message_view` | [1322](../../backend/app/admin_reporting.py#L1322) | `conn.execute`、`conn.transaction`、`connection`、`json.dumps`、`str`、`uuid4` |
| `_page_window` | [1338](../../backend/app/admin_reporting.py#L1338) | `_total_pages`、`max`、`min` |
| `_page_result` | [1345](../../backend/app/admin_reporting.py#L1345) | `_total_pages`、`int`、`len`、`max` |
| `_total_pages` | [1366](../../backend/app/admin_reporting.py#L1366) | `max` |
| `_decorate_user` | [1370](../../backend/app/admin_reporting.py#L1370) | `_authorization_status`、`_format_time`、`_job_status`、`_message_count_label`、`_mode_progress_label`、`_p2p_status`、`_sync_progress_label`、`dict`、`item.get`、`sync_summary` |
| `_decorate_conversation_membership` | [1403](../../backend/app/admin_reporting.py#L1403) | `_chat_mode_label`、`_conversation_name`、`_format_time`、`_member_count`、`_member_snapshot_activity`、`_message_count_label`、`_message_page_activity`、`_sync_status`、`dict`、`item.get` |
| `_decorate_chat` | [1419](../../backend/app/admin_reporting.py#L1419) | `_chat_mode_label`、`_format_time`、`_member_count`、`_message_count_label`、`dict`、`item.get` |
| `_decorate_message_conversation` | [1430](../../backend/app/admin_reporting.py#L1430) | `_chat_mode_label`、`_conversation_name`、`_member_count`、`_message_count_label`、`dict`、`item.get` |
| `_decorate_message` | [1443](../../backend/app/admin_reporting.py#L1443) | `_chat_mode_label`、`_conversation_name`、`_message_time`、`_message_type_label`、`_readable_body`、`cipher.decrypt`、`created_at.astimezone`、`created_at.astimezone(…).strftime`、`dict`、`item.get` |
| `_message_type_label` | [1466](../../backend/app/admin_reporting.py#L1466) | `<Dict>.get` |
| `_readable_body` | [1473](../../backend/app/admin_reporting.py#L1473) | `<Constant>.join`、`_body_text_values`、`json.loads`、`resolve_mentions` |
| `_conversation_name` | [1487](../../backend/app/admin_reporting.py#L1487) | `isinstance`、`str` |
| `_body_text_values` | [1495](../../backend/app/admin_reporting.py#L1495) | `collect` |
| `_body_text_values.collect` | [1498](../../backend/app/admin_reporting.py#L1498) | `collect`、`current.get`、`current.values`、`isinstance`、`rich_mention_text`、`texts.append` |
| `_decorate_job` | [1522](../../backend/app/admin_reporting.py#L1522) | `<Dict>.get`、`_format_time`、`_job_status`、`dict`、`item.get` |
| `_decorate_contact` | [1543](../../backend/app/admin_reporting.py#L1543) | `<Dict>.get`、`_format_time`、`_organization_status`、`_tenant_classification`、`dict`、`float`、`int`、`isinstance`、`item.get`、`private_chat_url`、`role_label`、`safe_avatar_url` |
| `_decorate_lark_identity` | [1609](../../backend/app/admin_reporting.py#L1609) | `_format_time`、`_tenant_classification`、`dict`、`item.get` |
| `_decorate_contact_organization` | [1620](../../backend/app/admin_reporting.py#L1620) | `<Dict>.get`、`ORGANIZATION_KIND_LABELS.get`、`_format_time`、`_organization_status`、`dict`、`item.get`、`relationships.get` |
| `_decorate_contact_conversation` | [1644](../../backend/app/admin_reporting.py#L1644) | `_chat_mode_label`、`_conversation_name`、`_format_time`、`_member_count`、`dict`、`int`、`item.get` |
| `_format_time` | [1658](../../backend/app/admin_reporting.py#L1658) | `value.astimezone`、`value.astimezone(…).strftime` |
| `_message_time` | [1664](../../backend/app/admin_reporting.py#L1664) | `value.astimezone`、`value.astimezone(…).strftime` |
| `sync_summary` | [1670](../../backend/app/admin_reporting.py#L1670) | `<Constant>.join`、`_format_time`、`_is_lark_rate_limited`、`datetime.now`、`facts.get`、`int`、`isinstance`、`parts.append` |
| `_sync_progress_label` | [1740](../../backend/app/admin_reporting.py#L1740) | `facts.get`、`int` |
| `_mode_progress_label` | [1746](../../backend/app/admin_reporting.py#L1746) | `facts.get`、`int` |
| `_seconds_label` | [1752](../../backend/app/admin_reporting.py#L1752) | — |
| `_authorization_status` | [1760](../../backend/app/admin_reporting.py#L1760) | `mapping.get` |
| `_job_status` | [1769](../../backend/app/admin_reporting.py#L1769) | `_is_lark_rate_limited`、`datetime.now`、`mapping.get` |
| `_is_lark_rate_limited` | [1791](../../backend/app/admin_reporting.py#L1791) | — |
| `_sync_status` | [1795](../../backend/app/admin_reporting.py#L1795) | `mapping.get` |
| `_p2p_status` | [1806](../../backend/app/admin_reporting.py#L1806) | `mapping.get` |
| `_chat_mode_label` | [1816](../../backend/app/admin_reporting.py#L1816) | — |
| `_tenant_classification` | [1824](../../backend/app/admin_reporting.py#L1824) | `mapping.get` |
| `_organization_status` | [1833](../../backend/app/admin_reporting.py#L1833) | `mapping.get` |
| `_member_count` | [1842](../../backend/app/admin_reporting.py#L1842) | `str` |
| `_message_page_activity` | [1850](../../backend/app/admin_reporting.py#L1850) | `_format_time`、`item.get` |
| `_member_snapshot_activity` | [1864](../../backend/app/admin_reporting.py#L1864) | `_format_time`、`item.get` |
| `_decorate_lark_admission` | [1881](../../backend/app/admin_reporting.py#L1881) | `_format_time`、`datetime.now`、`dict`、`int`、`isinstance`、`item.get` |
| `_message_count_label` | [1900](../../backend/app/admin_reporting.py#L1900) | `int` |

## backend/app/admin.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `TemplateRenderer.__call__` | [41](../../backend/app/admin.py#L41) | — |
| `create_admin_router` | [66](../../backend/app/admin.py#L66) | `APIRouter` |
| `create_admin_router.render_template` | [77](../../backend/app/admin.py#L77) | `original_render` |
| `create_admin_router.access` | [83](../../backend/app/admin.py#L83) | `<Dict>.get`、`RedirectResponse`、`acl.get`、`admin_permissions.access_for`、`current_authorization`、`render_template` |
| `create_admin_router.page_context` | [124](../../backend/app/admin.py#L124) | `_role_label`、`csrf_token` |
| `create_admin_router.pagination` | [133](../../backend/app/admin.py#L133) | `bool`、`int`、`isinstance`、`list`、`max`、`min`、`page_data.get`、`pages.append`、`query.items`、`range`、`url` |
| `create_admin_router.pagination.url` | [140](../../backend/app/admin.py#L140) | `urlencode` |
| `create_admin_router.contact_query` | [187](../../backend/app/admin.py#L187) | `urlencode` |
| `create_admin_router.owner_access` | [206](../../backend/app/admin.py#L206) | `access`、`render_template` |
| `create_admin_router.render_ai_config` | [220](../../backend/app/admin.py#L220) | `ai_dispatch.status`、`ai_feature_config.display_config`、`ai_feature_config.get_feature_config`、`connection`、`feature.replace`、`offhours_summary.get_settings_view`、`page_context`、`render_template`、`request.query_params.get` |
| `create_admin_router.render_contact_assessment` | [258](../../backend/app/admin.py#L258) | `any`、`contact_assessments.assessment_detail`、`contact_assessments.assessment_matches_contact`、`contact_assessments.record_assessment_view`、`contact_query`、`detail.get`、`item.get`、`page_context`、`render_template` |
| `create_admin_router.admin_overview` | [310](../../backend/app/admin.py#L310) | `access`、`admin_reporting.overview`、`page_context`、`render_template` |
| `create_admin_router.admin_users` | [322](../../backend/app/admin.py#L322) | `access`、`admin_permissions.users_access_data`、`admin_reporting.normalize_message_conversation_query`、`admin_reporting.normalize_user_permission_filter`、`admin_reporting.users`、`page_context`、`pagination`、`render_template` |
| `create_admin_router.admin_set_page_permissions` | [345](../../backend/app/admin.py#L345) | `RedirectResponse`、`ValueError`、`_csrf_matches`、`admin_permissions.set_permissions`、`admin_reporting.normalize_message_conversation_query`、`admin_reporting.normalize_user_permission_filter`、`any`、`body.decode`、`csrf_token`、`form.get`、`int`、`len`、`max`、`min`、`owner_access`、`parse_qs`、`parsed.get`、`parsed.items`、`render_template`、`request.body`、`request.headers.get`、`request.headers.get(…).split`、`set`、`str`、`urlencode` |
| `create_admin_router.admin_user_detail` | [380](../../backend/app/admin.py#L380) | `access`、`admin_permissions.access_for`、`admin_reporting.normalize_message_conversation_query`、`admin_reporting.normalize_user_permission_filter`、`admin_reporting.user_detail`、`bool`、`page_context`、`pagination`、`render_template` |
| `create_admin_router.admin_chats` | [440](../../backend/app/admin.py#L440) | `access`、`admin_reporting.chats`、`page_context`、`pagination`、`render_template` |
| `create_admin_router.export_messages` | [457](../../backend/app/admin.py#L457) | `Response`、`StreamingResponse`、`access`、`admin_reporting.message_user`、`chunks`、`message_export.safe_name`、`quote`、`user.get` |
| `create_admin_router.export_messages.check_access` | [471](../../backend/app/admin.py#L471) | `PermissionError`、`access`、`admin_reporting.message_user`、`latest[…].get` |
| `create_admin_router.export_messages.chunks` | [483](../../backend/app/admin.py#L483) | `anyio.CancelScope`、`message_export.stream_export`、`run_in_threadpool` |
| `create_admin_router.admin_messages` | [499](../../backend/app/admin.py#L499) | `access`、`admin_reporting._page_result`、`admin_reporting.message_conversation`、`admin_reporting.message_conversations`、`admin_reporting.message_page`、`admin_reporting.message_user`、`admin_reporting.message_users`、`admin_reporting.normalize_message_conversation_query`、`admin_reporting.normalize_message_conversation_type`、`admin_reporting.record_message_view`、`any`、`bool`、`len`、`next`、`page_context`、`pagination`、`render_template`、`selected_user.get`、`sorted`、`str`、`urlencode`、`user.get` |
| `create_admin_router.admin_ai_config` | [653](../../backend/app/admin.py#L653) | `access`、`render_ai_config` |
| `create_admin_router.ownership_json` | [660](../../backend/app/admin.py#L660) | `JSONResponse`、`jsonable_encoder` |
| `create_admin_router.ownership_error` | [665](../../backend/app/admin.py#L665) | `isinstance`、`ownership_json`、`str` |
| `create_admin_router.admin_creator_ownership` | [671](../../backend/app/admin.py#L671) | `Response`、`access`、`creator_ownership.page_data`、`hashlib.sha256`、`hashlib.sha256(…).hexdigest`、`jsonable_encoder`、`max`、`ownership_error`、`ownership_json`、`page_context`、`render_template`、`request.headers.get`、`request.url.path.endswith`、`response.headers.items` |
| `create_admin_router.admin_creator_ownership_preview` | [700](../../backend/app/admin.py#L700) | `UUID`、`_csrf_matches`、`_form_fields`、`csrf_token`、`form.get`、`owner_access`、`ownership_error`、`ownership_json`、`run_in_threadpool` |
| `create_admin_router.admin_creator_ownership_action` | [722](../../backend/app/admin.py#L722) | `UUID`、`ValueError`、`_csrf_matches`、`_form_fields`、`csrf_token`、`form.get`、`form.get(…).split`、`len`、`owner_access`、`ownership_error`、`ownership_json`、`request.url.path.rsplit`、`run_in_threadpool`、`set` |
| `create_admin_router.admin_creator_ownership_evidence` | [763](../../backend/app/admin.py#L763) | `creator_ownership.task_evidence`、`owner_access`、`ownership_error`、`ownership_json` |
| `create_admin_router.admin_contacts` | [775](../../backend/app/admin.py#L775) | `access`、`admin_reporting.contacts`、`admin_reporting.normalize_contact_category`、`admin_reporting.normalize_organization_query`、`contact_page.get`、`identity_summary.get`、`identity_summary.get(…).get`、`page_context`、`pagination`、`render_template`、`urlencode` |
| `create_admin_router.admin_contact_detail` | [819](../../backend/app/admin.py#L819) | `access`、`admin_reporting.contact_detail`、`admin_reporting.normalize_contact_category`、`admin_reporting.normalize_organization_query`、`admin_reporting.normalize_organization_status`、`bool`、`contact_assessments.assessment_available`、`contact_assessments.latest_contact_assessment`、`contact_query`、`detail.get`、`page_context`、`pagination`、`render_template` |
| `create_admin_router.admin_continue_contact_assessment` | [893](../../backend/app/admin.py#L893) | `RedirectResponse`、`_csrf_matches`、`_form_fields`、`conn.execute`、`conn.execute(…).fetchone`、`connection`、`contact_assessments.requeue_contact_assessment`、`csrf_token`、`owner_access`、`render_template`、`str` |
| `create_admin_router.admin_confirm_contact_organization` | [925](../../backend/app/admin.py#L925) | `RedirectResponse`、`_csrf_matches`、`_form_fields`、`admin_reporting.normalize_contact_category`、`admin_reporting.normalize_organization_query`、`admin_reporting.normalize_organization_status`、`conn.execute`、`conn.execute(…).fetchone`、`connection`、`contact_query`、`csrf_token`、`form.get`、`organization_sync.manually_confirm_contact_organization`、`owner_access`、`render_template`、`str` |
| `create_admin_router.admin_contact_assessment` | [996](../../backend/app/admin.py#L996) | `admin_reporting.normalize_contact_category`、`admin_reporting.normalize_organization_query`、`admin_reporting.normalize_organization_status`、`owner_access`、`render_contact_assessment` |
| `create_admin_router.admin_confirm_assessment_role` | [1024](../../backend/app/admin.py#L1024) | `RedirectResponse`、`_csrf_matches`、`_form_fields`、`admin_reporting.normalize_contact_category`、`admin_reporting.normalize_organization_query`、`admin_reporting.normalize_organization_status`、`contact_assessments.assessment_matches_contact`、`contact_assessments.confirm_assessment_role`、`contact_query`、`csrf_token`、`form.get`、`owner_access`、`render_contact_assessment`、`render_template`、`str` |
| `create_admin_router.admin_dismiss_contact_assessment` | [1093](../../backend/app/admin.py#L1093) | `RedirectResponse`、`_csrf_matches`、`_form_fields`、`admin_reporting.normalize_contact_category`、`admin_reporting.normalize_organization_query`、`admin_reporting.normalize_organization_status`、`contact_assessments.assessment_matches_contact`、`contact_assessments.dismiss_assessment`、`contact_query`、`csrf_token`、`owner_access`、`render_contact_assessment`、`render_template`、`str` |
| `create_admin_router.admin_save_ai_config` | [1158](../../backend/app/admin.py#L1158) | `RedirectResponse`、`_csrf_matches`、`_form_fields`、`ai_feature_config.FeatureConfigInputError`、`ai_feature_config.api_key_cipher`、`ai_feature_config.save_feature_config`、`bool`、`conn.execute`、`conn.transaction`、`connection`、`csrf_token`、`float`、`form.get`、`form.get(…).strip`、`get_settings`、`json.dumps`、`offhours_summary.save_settings_and_enqueue`、`owner_access`、`render_ai_config`、`render_template`、`str`、`threshold.strip`、`uuid4` |
| `create_admin_router.admin_resume_ai_queue` | [1241](../../backend/app/admin.py#L1241) | `RedirectResponse`、`_csrf_matches`、`_form_fields`、`ai_dispatch.resume`、`conn.execute`、`conn.transaction`、`connection`、`csrf_token`、`json.dumps`、`owner_access`、`render_template`、`uuid4` |
| `create_admin_router.admin_jobs` | [1267](../../backend/app/admin.py#L1267) | `access`、`admin_reporting.jobs`、`page_context`、`pagination`、`render_template` |
| `_form_fields` | [1286](../../backend/app/admin.py#L1286) | `body.decode`、`len`、`parse_qs`、`parsed.items`、`request.body`、`request.headers.get`、`request.headers.get(…).split` |
| `_csrf_matches` | [1297](../../backend/app/admin.py#L1297) | `bool`、`expected.encode`、`form.get`、`hmac.compare_digest`、`supplied.encode` |
| `_role_label` | [1302](../../backend/app/admin.py#L1302) | `<Dict>.get` |

## backend/app/ai_dispatch.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `can_claim` | [22](../../backend/app/ai_dispatch.py#L22) | `bool`、`conn.execute`、`conn.execute(…).fetchone` |
| `throttle` | [63](../../backend/app/ai_dispatch.py#L63) | `conn.execute`、`conn.transaction`、`connection`、`float`、`math.isfinite`、`max`、`min` |
| `pause_for_quota` | [87](../../backend/app/ai_dispatch.py#L87) | `conn.execute`、`conn.transaction`、`connection` |
| `resume` | [103](../../backend/app/ai_dispatch.py#L103) | `conn.execute` |
| `status` | [116](../../backend/app/ai_dispatch.py#L116) | `conn.execute`、`conn.execute(…).fetchone` |

## backend/app/ai_feature_config.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `api_key_cipher` | [31](../../backend/app/ai_feature_config.py#L31) | `TokenCipher`、`hmac.digest` |
| `get_feature_config` | [38](../../backend/app/ai_feature_config.py#L38) | `_required_text`、`_safe_config`、`conn.execute`、`conn.execute(…).fetchone` |
| `save_feature_config` | [48](../../backend/app/ai_feature_config.py#L48) | `FeatureConfigInputError`、`_normalize_api_key`、`_normalize_auto_confirm_threshold`、`_required_text`、`_required_text(…).casefold`、`_safe_config`、`bool`、`cipher.encrypt`、`conn.execute`、`conn.execute(…).fetchone` |
| `display_config` | [131](../../backend/app/ai_feature_config.py#L131) | `<Dict>.get`、`_time_label`、`bool`、`float`、`values.get` |
| `_normalize_api_key` | [164](../../backend/app/ai_feature_config.py#L164) | `FeatureConfigInputError`、`any`、`character.isspace`、`key.isascii`、`len`、`value.strip` |
| `_normalize_auto_confirm_threshold` | [172](../../backend/app/ai_feature_config.py#L172) | `Decimal`、`FeatureConfigInputError`、`str`、`threshold.as_tuple`、`threshold.is_finite`、`threshold.quantize` |
| `_required_text` | [186](../../backend/app/ai_feature_config.py#L186) | `FeatureConfigInputError`、`len`、`value.strip` |
| `_safe_config` | [195](../../backend/app/ai_feature_config.py#L195) | `_SAFE_COLUMNS.split`、`column.strip`、`row.get` |
| `_time_label` | [199](../../backend/app/ai_feature_config.py#L199) | `value.astimezone`、`value.astimezone(…).strftime` |

## backend/app/config.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `_required` | [11](../../backend/app/config.py#L11) | `RuntimeError`、`os.environ.get`、`os.environ.get(…).strip` |
| `_positive_int` | [18](../../backend/app/config.py#L18) | `RuntimeError`、`int`、`os.environ.get`、`os.environ.get(…).strip`、`str` |
| `_nonnegative_int` | [29](../../backend/app/config.py#L29) | `RuntimeError`、`int`、`os.environ.get`、`os.environ.get(…).strip`、`str` |
| `_decode_key` | [40](../../backend/app/config.py#L40) | `RuntimeError`、`base64.urlsafe_b64decode`、`len`、`padded.encode` |
| `Settings.app_secret_configured` | [76](../../backend/app/config.py#L76) | `bool` |
| `get_settings` | [81](../../backend/app/config.py#L81) | `Settings`、`_decode_key`、`_nonnegative_int`、`_positive_int`、`_required`、`os.environ.get`、`os.environ.get(…).rstrip`、`os.environ.get(…).split`、`os.environ.get(…).strip`、`tuple` |

## backend/app/contact_assessments.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `OpenAIRequestError.__init__` | [106](../../backend/app/contact_assessments.py#L106) | `dict`、`super`、`super(…).__init__` |
| `assessment_available` | [126](../../backend/app/contact_assessments.py#L126) | `bool`、`conn.execute`、`conn.execute(…).fetchone`、`connection`、`row[…].casefold` |
| `_feature_enabled_in` | [143](../../backend/app/contact_assessments.py#L143) | `bool`、`conn.execute`、`conn.execute(…).fetchone` |
| `queue_eligible_contact_assessments` | [151](../../backend/app/contact_assessments.py#L151) | `conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`len` |
| `requeue_contact_assessment` | [198](../../backend/app/contact_assessments.py#L198) | `ContactAssessmentInputError`、`_as_uuid`、`_feature_enabled_in`、`_lock_contact`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`str`、`uuid4` |
| `latest_contact_assessment` | [228](../../backend/app/contact_assessments.py#L228) | `_as_uuid`、`_summary`、`conn.execute`、`conn.execute(…).fetchone`、`connection` |
| `assessment_detail` | [247](../../backend/app/contact_assessments.py#L247) | `_as_uuid`、`_decrypt_body`、`_detail_role`、`_is_latest_in`、`_json_list`、`_json_object`、`_refs_are_still_authorized_in`、`_summary`、`_time_label`、`bool`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`connection`、`display_body`、`get_settings`、`int`、`item.get`、`len`、`mention_names`、`message_body_cipher`、`result.get`、`row.get`、`sorted`、`str` |
| `record_assessment_view` | [366](../../backend/app/contact_assessments.py#L366) | `ContactAssessmentInputError`、`_as_uuid`、`_audit`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`str` |
| `dismiss_assessment` | [377](../../backend/app/contact_assessments.py#L377) | `ContactAssessmentInputError`、`_as_uuid`、`_audit`、`assessment.get`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`str` |
| `assessment_matches_contact` | [414](../../backend/app/contact_assessments.py#L414) | `_as_uuid`、`bool`、`conn.execute`、`conn.execute(…).fetchone`、`connection` |
| `confirm_assessment_role` | [431](../../backend/app/contact_assessments.py#L431) | `ContactAssessmentInputError`、`_as_uuid`、`_audit`、`_feature_enabled_in`、`_json_list`、`_json_object`、`_json_object(…).get`、`_lock_contact`、`_protected_classification`、`_refs_are_still_authorized_in`、`_successful_assessment`、`conn.execute`、`conn.transaction`、`connection`、`float`、`isinstance`、`item.get`、`role.strip`、`str`、`suggestion.get`、`suggestions.get` |
| `claim_next_contact_assessment` | [490](../../backend/app/contact_assessments.py#L490) | `_claim_next`、`_expire_leases`、`assessment_available`、`queue_eligible_contact_assessments` |
| `run_claimed_contact_assessment` | [500](../../backend/app/contact_assessments.py#L500) | `_run` |
| `_expire_leases` | [506](../../backend/app/contact_assessments.py#L506) | `conn.execute`、`conn.transaction`、`connection` |
| `_claim_next` | [517](../../backend/app/contact_assessments.py#L517) | `_feature_enabled_in`、`ai_dispatch.can_claim`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`dict` |
| `_current_job_in` | [539](../../backend/app/contact_assessments.py#L539) | `bool`、`conn.execute`、`conn.execute(…).fetchone` |
| `_protected_classification` | [549](../../backend/app/contact_assessments.py#L549) | `conn.execute`、`conn.execute(…).fetchone` |
| `_finish_without_model` | [578](../../backend/app/contact_assessments.py#L578) | `_current_job_in`、`_feature_enabled_in`、`_finish_in`、`_lock_contact`、`_protected_classification`、`conn.execute`、`conn.transaction`、`connection` |
| `_run` | [604](../../backend/app/contact_assessments.py#L604) | `LOGGER.warning`、`_build_evidence`、`_call_openai`、`_config_matches_in`、`_configured_openai`、`_current_job_in`、`_feature_enabled_in`、`_finish_with_decision`、`_finish_without_model`、`_lock_contact`、`_mark_feature_test`、`_protected_classification`、`_refs_are_still_authorized_in`、`_reschedule`、`_retry_delay`、`_save_refs`、`_validate_model_result`、`ai_dispatch.pause_for_quota`、`ai_dispatch.throttle`、`conn.transaction`、`connection`、`finish`、`range`、`response.get` |
| `_run.finish` | [608](../../backend/app/contact_assessments.py#L608) | `_finish` |
| `_retry_delay` | [702](../../backend/app/contact_assessments.py#L702) | `float`、`int`、`math.isfinite`、`max`、`min`、`random.uniform` |
| `_reschedule` | [719](../../backend/app/contact_assessments.py#L719) | `_feature_enabled_in`、`conn.execute`、`conn.transaction`、`connection`、`telemetry.get` |
| `_configured_openai` | [747](../../backend/app/contact_assessments.py#L747) | `ContactAssessmentInputError`、`api_key_cipher`、`api_key_cipher(…).decrypt`、`config[…].casefold`、`conn.execute`、`conn.execute(…).fetchone`、`connection`、`dict` |
| `_config_matches_in` | [770](../../backend/app/contact_assessments.py#L770) | `all`、`bool`、`config.get`、`conn.execute`、`conn.execute(…).fetchone` |
| `_identity_text` | [813](../../backend/app/contact_assessments.py#L813) | `<Constant>.join`、`_ACCOUNT.finditer`、`_ADDRESS.sub`、`_EMAIL.sub`、`_IDENTITY_CONTEXT_SIGNAL.search`、`_PHONE.sub`、`_readable_body`、`account.end`、`account.group`、`account.start`、`fragments`、`json.loads`、`known_names.items`、`len`、`max`、`mention_targets`、`pieces.append`、`re.escape`、`re.sub`、`resolve_mentions`、`row.get`、`signal.start`、`value.strip` |
| `_identity_text.fragments` | [820](../../backend/app/contact_assessments.py#L820) | `fragments`、`isinstance`、`rich_mention_text`、`row.get`、`value.get`、`value.items` |
| `_build_evidence` | [863](../../backend/app/contact_assessments.py#L863) | `<Constant>.join`、`_IMPORTANT.findall`、`_ROUTINE.fullmatch`、`_clean_body`、`_decrypt_body`、`_identity_text`、`_readable_body`、`body.casefold`、`body.casefold(…).split`、`candidates.append`、`candidates.sort`、`ch.isalpha`、`conn.execute`、`conn.execute(…).fetchall`、`connection`、`dict`、`enumerate`、`fragments.append`、`hashlib.sha256`、`hashlib.sha256(…).hexdigest`、`len`、`message_body_cipher`、`messages.append`、`range`、`raw.encode`、`re.sub`、`refs.append`、`round`、`seen_text.add`、`selected.extend`、`selected.sort`、`set`、`sorted`、`sum` |
| `_save_refs` | [960](../../backend/app/contact_assessments.py#L960) | `_current_job_in`、`_feature_enabled_in`、`conn.execute`、`conn.transaction`、`connection`、`len` |
| `_refs_are_still_authorized` | [988](../../backend/app/contact_assessments.py#L988) | `_refs_are_still_authorized_in`、`connection` |
| `_refs_are_still_authorized_in` | [993](../../backend/app/contact_assessments.py#L993) | `_decrypt_body`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`get_settings`、`hashlib.sha256`、`hashlib.sha256(…).hexdigest`、`len`、`message_body_cipher`、`raw.encode` |
| `call_openai` | [1034](../../backend/app/contact_assessments.py#L1034) | `ContactAssessmentInputError`、`OpenAIRequestError`、`_ModelOutputError`、`_bounded_text`、`_is_quota_response`、`_nonnegative_int`、`_openai_telemetry`、`_response_text`、`_retry_after_header`、`config.get`、`data.get`、`hashlib.sha256`、`hashlib.sha256(…).hexdigest`、`httpx.post`、`isinstance`、`json.dumps`、`response.json`、`str`、`str(…).encode`、`time.perf_counter`、`usage.get` |
| `_openai_telemetry` | [1097](../../backend/app/contact_assessments.py#L1097) | `_header_nonnegative_int`、`getattr`、`header`、`max`、`round`、`time.perf_counter` |
| `_openai_telemetry.header` | [1102](../../backend/app/contact_assessments.py#L1102) | `_bounded_text`、`headers.get`、`name.lower` |
| `_retry_after_header` | [1116](../../backend/app/contact_assessments.py#L1116) | `float`、`getattr`、`headers.get`、`math.isfinite`、`max`、`min`、`parsedate_to_datetime`、`parsedate_to_datetime(…).timestamp`、`str`、`time.time` |
| `_is_quota_response` | [1131](../../backend/app/contact_assessments.py#L1131) | `<Constant>.join`、`any`、`error.get`、`isinstance`、`payload.get`、`response.json`、`str`、`str(…).casefold` |
| `_call_openai` | [1155](../../backend/app/contact_assessments.py#L1155) | `_structured_output_format`、`call_openai` |
| `_structured_output_format` | [1168](../../backend/app/contact_assessments.py#L1168) | `sorted` |
| `_response_text` | [1232](../../backend/app/contact_assessments.py#L1232) | `KeyError`、`_json_list`、`data.get`、`isinstance`、`item.get`、`output.get` |
| `_validate_model_result` | [1242](../../backend/app/contact_assessments.py#L1242) | `_ModelOutputError`、`_confidence`、`_evidence_basis`、`_evidence_refs`、`_json_list`、`isinstance`、`json.loads`、`len`、`payload.get`、`raw_role.get`、`roles.append`、`seen_roles.add`、`set` |
| `_evidence_basis` | [1274](../../backend/app/contact_assessments.py#L1274) | `_ModelOutputError`、`str` |
| `_finish_with_decision` | [1280](../../backend/app/contact_assessments.py#L1280) | `_apply_automatic_decision`、`_audit`、`_auto_confirm_threshold`、`_config_matches_in`、`_current_job_in`、`_feature_enabled_in`、`_finish_in`、`_lock_contact`、`_protected_classification`、`_refs_are_still_authorized_in`、`conn.transaction`、`connection`、`str` |
| `_apply_automatic_decision` | [1349](../../backend/app/contact_assessments.py#L1349) | `_auto_confirm_role`、`_has_auto_confirmation_evidence`、`_json_list`、`conn.execute`、`float`、`len`、`max`、`result.get`、`suggestion.get` |
| `_auto_confirm_role` | [1373](../../backend/app/contact_assessments.py#L1373) | `conn.execute`、`conn.execute(…).fetchone` |
| `_has_auto_confirmation_evidence` | [1389](../../backend/app/contact_assessments.py#L1389) | `_json_list`、`all`、`bool`、`evidence_strength.get`、`evidence_strength.get(…).get`、`suggestion.get` |
| `_auto_confirm_threshold` | [1398](../../backend/app/contact_assessments.py#L1398) | `ContactAssessmentInputError`、`config.get`、`float`、`max` |
| `_lock_contact` | [1408](../../backend/app/contact_assessments.py#L1408) | `ContactAssessmentInputError`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone` |
| `_finish` | [1417](../../backend/app/contact_assessments.py#L1417) | `_finish_in`、`conn.transaction`、`connection` |
| `_finish_in` | [1448](../../backend/app/contact_assessments.py#L1448) | `_bounded_text`、`conn.execute`、`json.dumps`、`telemetry.get`、`usage.get` |
| `_mark_feature_test` | [1503](../../backend/app/contact_assessments.py#L1503) | `conn.execute`、`conn.transaction`、`connection` |
| `_successful_assessment` | [1515](../../backend/app/contact_assessments.py#L1515) | `ContactAssessmentInputError`、`_is_latest_in`、`conn.execute`、`conn.execute(…).fetchone` |
| `_is_latest_in` | [1528](../../backend/app/contact_assessments.py#L1528) | `bool`、`conn.execute`、`conn.execute(…).fetchone`、`row.get` |
| `_summary` | [1539](../../backend/app/contact_assessments.py#L1539) | `OUTCOME_BADGES.get`、`OUTCOME_LABELS.get`、`REASON_LABELS.get`、`REVIEW_STATUS_BADGES.get`、`REVIEW_STATUS_LABELS.get`、`STATUS_BADGES.get`、`STATUS_LABELS.get`、`_json_list`、`_json_object`、`_time_label`、`len`、`result.get`、`row.get`、`str` |
| `_detail_role` | [1569](../../backend/app/contact_assessments.py#L1569) | `EVIDENCE_BASIS_LABELS.get`、`ROLE_LABELS.get`、`_confidence_label`、`_json_list`、`float`、`isinstance`、`value.get` |
| `_audit` | [1583](../../backend/app/contact_assessments.py#L1583) | `conn.execute`、`json.dumps`、`uuid4` |
| `_as_uuid` | [1590](../../backend/app/contact_assessments.py#L1590) | `ContactAssessmentInputError`、`UUID`、`isinstance`、`str` |
| `_decrypt_body` | [1597](../../backend/app/contact_assessments.py#L1597) | `cipher.decrypt` |
| `_readable_body` | [1606](../../backend/app/contact_assessments.py#L1606) | `<Constant>.join`、`collect`、`json.loads` |
| `_readable_body.collect` | [1613](../../backend/app/contact_assessments.py#L1613) | `collect`、`isinstance`、`texts.append`、`value.get`、`value.items` |
| `_clean_body` | [1628](../../backend/app/contact_assessments.py#L1628) | `<BoolOp>.split`、`<Constant>.join` |
| `_bounded_text` | [1632](../../backend/app/contact_assessments.py#L1632) | `str`、`str(…).strip` |
| `_confidence` | [1636](../../backend/app/contact_assessments.py#L1636) | `_ModelOutputError`、`float`、`isinstance` |
| `_confidence_label` | [1648](../../backend/app/contact_assessments.py#L1648) | `float`、`round` |
| `_evidence_refs` | [1655](../../backend/app/contact_assessments.py#L1655) | `_ModelOutputError`、`_json_list`、`any`、`dict.fromkeys`、`isinstance`、`len`、`list` |
| `_json_list` | [1662](../../backend/app/contact_assessments.py#L1662) | `isinstance`、`list` |
| `_json_object` | [1670](../../backend/app/contact_assessments.py#L1670) | `isinstance`、`json.loads` |
| `_nonnegative_int` | [1682](../../backend/app/contact_assessments.py#L1682) | `int`、`isinstance` |
| `_header_nonnegative_int` | [1686](../../backend/app/contact_assessments.py#L1686) | `int` |
| `_time_label` | [1694](../../backend/app/contact_assessments.py#L1694) | `value.astimezone`、`value.astimezone(…).strftime` |

## backend/app/contact_taxonomy.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `role_label` | [59](../../backend/app/contact_taxonomy.py#L59) | `ROLE_LABELS.get` |

## backend/app/creator_handle_evidence.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `normalize_handle` | [80](../../backend/app/creator_handle_evidence.py#L80) | `<BoolOp>.casefold`、`ValueError`、`_HANDLE.fullmatch`、`any`、`c.isalnum`、`isinstance`、`re.fullmatch`、`url.path.removeprefix`、`url.path.removeprefix(…).removesuffix`、`urlsplit`、`value.casefold`、`value.endswith`、`value.removeprefix`、`value.startswith`、`value.strip` |
| `_clues` | [99](../../backend/app/creator_handle_evidence.py#L99) | `any`、`clues.append`、`match.end`、`match.groups`、`match.span`、`match.start`、`match[…].replace`、`next`、`normalize_handle`、`occupied.append`、`pattern.finditer`、`raw_token.rstrip`、`re.fullmatch`、`sorted` |
| `_name_handles` | [130](../../backend/app/creator_handle_evidence.py#L130) | `_clues`、`sorted` |
| `_body` | [134](../../backend/app/creator_handle_evidence.py#L134) | `<Constant>.join`、`_AD_CODE.sub`、`_clues`、`aliases.get`、`dict.fromkeys`、`enumerate`、`hashlib.sha256`、`hashlib.sha256(…).hexdigest`、`json.loads`、`len`、`list`、`mention_aliases.get`、`mention_targets`、`name.strip`、`names.items`、`p.strip`、`people._ADDRESS.sub`、`people._EMAIL.sub`、`people._PHONE.sub`、`people._readable_body`、`pieces`、`raw.encode`、`re.escape`、`re.sub`、`replacements.items`、`resolve_mentions`、`reversed`、`row.get`、`text.replace` |
| `_body.pieces` | [138](../../backend/app/creator_handle_evidence.py#L138) | `<Constant>.join`、`_CHANGE.search`、`_clues`、`any`、`bool`、`isinstance`、`known.update`、`len`、`list`、`locales.items`、`max`、`p.strip`、`pieces`、`re.fullmatch`、`rich_mention_text`、`row.get`、`v[…].startswith`、`value.get`、`value.items` |
| `_body.hide_code` | [197](../../backend/app/creator_handle_evidence.py#L197) | `<BoolOp>.strip`、`<BoolOp>.strip(…).lstrip`、`any`、`bool`、`c.isalpha`、`c.isdigit`、`len`、`token.isupper` |
| `_body.safe_url` | [223](../../backend/app/creator_handle_evidence.py#L223) | `urlsplit`、`urlunsplit` |
| `_self_handles` | [236](../../backend/app/creator_handle_evidence.py#L236) | `_AT.fullmatch`、`_CHANGE.search`、`_DECLARATION.fullmatch`、`_INTRO_BEFORE.search`、`_OWN_AFTER.search`、`_OWN_BEFORE.search`、`_clues`、`found.add`、`max`、`re.match`、`set`、`text[…].casefold`、`text[…].casefold(…).startswith` |
| `_related_items` | [259](../../backend/app/creator_handle_evidence.py#L259) | `_ACCOUNT_BUSINESS.findall`、`_ACCOUNT_BUSINESS.search`、`_OWN_ACTION.search`、`abs`、`bool`、`by_key.get`、`by_key.items`、`chosen.add`、`chosen.update`、`item[…].get`、`other[…].get`、`ownership.RESPONSE.search`、`set`、`timedelta`、`v.casefold` |
| `_members` | [317](../../backend/app/creator_handle_evidence.py#L317) | `conn.execute`、`conn.execute(…).fetchall`、`list`、`ownership._READABLE.replace` |
| `_load` | [338](../../backend/app/creator_handle_evidence.py#L338) | `_members`、`aliases.items`、`conn.execute`、`conn.execute(…).fetchall`、`list`、`ownership._scope` |
| `build_evidence` | [377](../../backend/app/creator_handle_evidence.py#L377) | `_ACCOUNT_BUSINESS.search`、`_ALL_MENTION.search`、`_CHANGE.search`、`_body`、`_clues`、`_load`、`_name_handles`、`_related_items`、`_self_handles`、`abs`、`aliases.get`、`any`、`bool`、`by_chat.values`、`by_chat[…].append`、`connection`、`defaultdict`、`dict`、`enumerate`、`group_rows.get`、`group_rows.get(…).get`、`groups.append`、`hashlib.sha256`、`hashlib.sha256(…).hexdigest`、`i[…].timestamp`、`item[…].isoformat`、`items.append`、`labels.get`、`len`、`list`、`max`、`membership[…].values`、`mention_targets`、`message_body_cipher`、`messages.append`、`ownership.BROADCAST.search`、`ownership._hash`、`ownership._relation_hash`、`people._decrypt_body`、`raw.encode`、`refs.append`、`row.get`、`selected.values`、`sorted`、`source_hashes.append`、`str`、`sum`、`timedelta` |
| `validate_evidence` | [587](../../backend/app/creator_handle_evidence.py#L587) | `Jsonb`、`_members`、`all`、`any`、`build_evidence`、`by_message.get`、`conn.execute`、`conn.execute(…).fetchall`、`connection`、`defaultdict`、`dict`、`hashlib.sha256`、`hashlib.sha256(…).hexdigest`、`isinstance`、`iter`、`len`、`list`、`membership[…].values`、`message_body_cipher`、`next`、`ownership._hash`、`ownership._relation_hash`、`ownership._scope`、`pack.get`、`people._decrypt_body`、`r.get`、`raw.encode`、`str` |
| `output_format` | [687](../../backend/app/creator_handle_evidence.py#L687) | — |
| `_strong` | [724](../../backend/app/creator_handle_evidence.py#L724) | `_ACCOUNT_BUSINESS.search`、`_OWN_ACTION.search`、`abs`、`any`、`cm.get`、`datetime.fromisoformat`、`group.get`、`m.get`、`subject.get`、`timedelta` |
| `validate_result` | [756](../../backend/app/creator_handle_evidence.py#L756) | `ValueError`、`_strong`、`any`、`bool`、`isinstance`、`json.loads`、`len`、`math.isfinite`、`normalize_handle`、`result[…].strip`、`seen.add`、`set` |

## backend/app/creator_handles_routes.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `create_router` | [16](../../backend/app/creator_handles_routes.py#L16) | `APIRouter` |
| `create_router.secure` | [19](../../backend/app/creator_handles_routes.py#L19) | — |
| `create_router.payload` | [25](../../backend/app/creator_handles_routes.py#L25) | `JSONResponse`、`jsonable_encoder`、`secure` |
| `create_router.error` | [28](../../backend/app/creator_handles_routes.py#L28) | `isinstance`、`payload`、`str` |
| `create_router.actor` | [32](../../backend/app/creator_handles_routes.py#L32) | `PermissionError`、`current_authorization` |
| `create_router.form_for` | [38](../../backend/app/creator_handles_routes.py#L38) | `ValueError`、`_csrf_matches`、`_form_fields`、`actor`、`csrf_token` |
| `create_router.page` | [47](../../backend/app/creator_handles_routes.py#L47) | `RedirectResponse`、`actor`、`creator_handles.page_data`、`csrf_token`、`current_authorization`、`error`、`jsonable_encoder`、`max`、`payload`、`render_template`、`request.url.path.endswith`、`secure` |
| `create_router.retired_action` | [70](../../backend/app/creator_handles_routes.py#L70) | `error`、`form_for`、`payload` |
| `create_router.review` | [79](../../backend/app/creator_handles_routes.py#L79) | `ValueError`、`any`、`error`、`form.get`、`form_for`、`handle.strip`、`isinstance`、`json.loads`、`len`、`payload`、`request.url.path.endswith`、`run_in_threadpool` |
| `create_router.evidence` | [99](../../backend/app/creator_handles_routes.py#L99) | `actor`、`creator_handles.task_evidence`、`error`、`payload` |
| `create_router.import_preview` | [107](../../backend/app/creator_handles_routes.py#L107) | `ValueError`、`_csrf_matches`、`actor`、`bytearray`、`bytes`、`content.extend`、`csrf_token`、`error`、`int`、`len`、`payload`、`request.headers.get`、`request.headers.get(…).split`、`request.stream`、`run_in_threadpool`、`unquote` |
| `create_router.import_data` | [134](../../backend/app/creator_handles_routes.py#L134) | `actor`、`creator_imports.page_data`、`error`、`max`、`payload` |
| `create_router.import_users` | [142](../../backend/app/creator_handles_routes.py#L142) | `actor`、`creator_imports.search_users`、`error`、`payload` |
| `create_router.import_action` | [153](../../backend/app/creator_handles_routes.py#L153) | `error`、`form_for`、`payload`、`request.url.path.rsplit`、`run_in_threadpool` |
| `create_router.import_evidence` | [168](../../backend/app/creator_handles_routes.py#L168) | `actor`、`creator_imports.row_evidence`、`error`、`payload` |
| `create_router.import_review` | [177](../../backend/app/creator_handles_routes.py#L177) | `UUID`、`ValueError`、`error`、`form.get`、`form.get(…).strip`、`form_for`、`len`、`payload`、`request.url.path.endswith`、`run_in_threadpool` |

## backend/app/creator_handles.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `_audit` | [52](../../backend/app/creator_handles.py#L52) | `Jsonb`、`conn.execute`、`uuid4` |
| `_owner` | [59](../../backend/app/creator_handles.py#L59) | `ownership._owner` |
| `_can_view_all` | [67](../../backend/app/creator_handles.py#L67) | `_owner`、`bool`、`conn.execute`、`conn.execute(…).fetchone` |
| `_access` | [75](../../backend/app/creator_handles.py#L75) | `PermissionError`、`_can_view_all`、`_owner`、`bool`、`conn.execute`、`conn.execute(…).fetchone`、`ownership._cm`、`ownership._uuid` |
| `_run` | [91](../../backend/app/creator_handles.py#L91) | `LookupError`、`ValueError`、`_access`、`conn.execute`、`conn.execute(…).fetchone`、`ownership._uuid` |
| `_task` | [103](../../backend/app/creator_handles.py#L103) | `LookupError`、`ValueError`、`_run`、`conn.execute`、`conn.execute(…).fetchone`、`ownership._uuid` |
| `_parent` | [115](../../backend/app/creator_handles.py#L115) | `ValueError`、`conn.execute`、`conn.execute(…).fetchone`、`ownership._cm`、`ownership._creator`、`ownership._valid_material` |
| `_decode` | [146](../../backend/app/creator_handles.py#L146) | `json.loads`、`ownership._cipher`、`ownership._cipher(…).decrypt`、`task.get` |
| `_valid_handle_material` | [153](../../backend/app/creator_handles.py#L153) | `_decode`、`bool`、`task.get`、`validate_evidence` |
| `_seed` | [170](../../backend/app/creator_handles.py#L170) | `_settle`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`str`、`uuid4` |
| `create_preview` | [212](../../backend/app/creator_handles.py#L212) | `ValueError`、`_access`、`_audit`、`_seed`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`ownership._uuid`、`str` |
| `_settle` | [246](../../backend/app/creator_handles.py#L246) | `conn.execute` |
| `_conflict` | [261](../../backend/app/creator_handles.py#L261) | `bool`、`conn.execute`、`conn.execute(…).fetchone` |
| `_admit` | [277](../../backend/app/creator_handles.py#L277) | `ValueError`、`_audit`、`_conflict`、`_decode`、`_parent`、`_valid_handle_material`、`all`、`any`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`isinstance`、`len`、`min`、`normalize_handle`、`ownership._encode`、`set`、`sorted`、`str`、`uuid4`、`validate_result` |
| `confirm_task` | [373](../../backend/app/creator_handles.py#L373) | `_admit`、`_task`、`conn.transaction`、`connection`、`get_settings`、`ownership._uuid` |
| `set_creator_type` | [380](../../backend/app/creator_handles.py#L380) | `PermissionError`、`ValueError`、`_access`、`_audit`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`ownership._uuid`、`str` |
| `defer_task` | [405](../../backend/app/creator_handles.py#L405) | `ValueError`、`_audit`、`_task`、`conn.execute`、`conn.transaction`、`connection`、`ownership._uuid`、`str` |
| `recheck_task` | [417](../../backend/app/creator_handles.py#L417) | `ValueError`、`_access`、`_audit`、`_parent`、`_task`、`build_evidence`、`conn.execute`、`conn.transaction`、`connection`、`get_settings`、`ownership._cipher`、`ownership._cipher(…).encrypt`、`ownership._encode`、`ownership._uuid`、`str` |
| `_auto_next` | [462](../../backend/app/creator_handles.py#L462) | `_admit`、`_decode`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`ownership._owner` |
| `prepare_next_preview` | [494](../../backend/app/creator_handles.py#L494) | `Jsonb`、`LOGGER.warning`、`_auto_next`、`_parent`、`_seed`、`_settle`、`bool`、`build_evidence`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`json.loads`、`ownership._cipher`、`ownership._cipher(…).encrypt`、`ownership._cm`、`ownership._config`、`ownership._config_fingerprint`、`ownership._encode`、`ownership._owner` |
| `start_run` | [582](../../backend/app/creator_handles.py#L582) | `ValueError`、`_audit`、`_run`、`_settle`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`ownership._config`、`ownership._config_fingerprint`、`ownership._uuid`、`str` |
| `pause_run` | [624](../../backend/app/creator_handles.py#L624) | `_audit`、`_run`、`conn.execute`、`conn.transaction`、`connection`、`ownership._uuid`、`str` |
| `resume_run` | [632](../../backend/app/creator_handles.py#L632) | `_audit`、`_run`、`_settle`、`conn.execute`、`conn.transaction`、`connection`、`ownership._uuid`、`str` |
| `set_auto_admit` | [641](../../backend/app/creator_handles.py#L641) | `ValueError`、`_audit`、`_run`、`conn.execute`、`conn.transaction`、`connection`、`isinstance`、`ownership._uuid`、`str` |
| `claim_next_handle` | [650](../../backend/app/creator_handles.py#L650) | `_settle`、`ai_dispatch.can_claim`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`conn.transaction`、`connection` |
| `_current_paid` | [682](../../backend/app/creator_handles.py#L682) | `conn.execute`、`conn.execute(…).fetchone` |
| `_finish` | [692](../../backend/app/creator_handles.py#L692) | `<BoolOp>.get`、`ValueError`、`_current_paid`、`_parent`、`_settle`、`_valid_handle_material`、`conn.execute`、`conn.transaction`、`connection`、`json.loads`、`min`、`ownership._cipher`、`ownership._cipher(…).encrypt`、`ownership._config`、`ownership._config_fingerprint`、`ownership._encode`、`ownership._owner`、`response.get`、`telemetry.get`、`usage.get` |
| `run_claimed_handle` | [744](../../backend/app/creator_handles.py#L744) | `LOGGER.warning`、`ValueError`、`_current_paid`、`_decode`、`_finish`、`_parent`、`_valid_handle_material`、`ai_dispatch.pause_for_quota`、`ai_dispatch.throttle`、`api_key_cipher`、`api_key_cipher(…).decrypt`、`conn.execute`、`conn.transaction`、`connection`、`contact_assessments.call_openai`、`exc.telemetry.get`、`max`、`min`、`output_format`、`ownership._config`、`ownership._config_fingerprint`、`ownership._owner`、`validate_result` |
| `page_data` | [802](../../backend/app/creator_handles.py#L802) | `<Constant>.join`、`_access`、`_can_view_all`、`args.extend`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`connection`、`counts.update`、`creator_imports.page_data`、`creator_imports.source_labels`、`dict.fromkeys`、`evidence_rows.setdefault`、`evidence_rows.values`、`handles[…].append`、`identities.get`、`imports_by_person[…].append`、`int`、`items.append`、`list`、`max`、`min`、`ownership._uuid`、`private_chat_url`、`safe_avatar_url`、`str`、`str(…).strip` |
| `task_evidence` | [910](../../backend/app/creator_handles.py#L910) | `ValueError`、`_audit`、`_decode`、`_parent`、`_task`、`_valid_handle_material`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`get_settings`、`group.get`、`message.get`、`messages.append`、`ownership._cm`、`ownership._uuid`、`pack.get`、`result.get`、`str` |

## backend/app/creator_import_evidence.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `_mentions_handle` | [26](../../backend/app/creator_import_evidence.py#L26) | `bool`、`re.escape`、`re.search` |
| `_scope` | [31](../../backend/app/creator_import_evidence.py#L31) | `conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone` |
| `_candidate_groups` | [49](../../backend/app/creator_import_evidence.py#L49) | `SequenceMatcher`、`SequenceMatcher(…).ratio`、`_TOKEN.findall`、`_mentions_handle`、`handles._name_handles`、`len`、`max`、`ranked.append`、`ranked.sort`、`round`、`token.rstrip`、`token.rstrip(…).casefold` |
| `_history_groups` | [62](../../backend/app/creator_import_evidence.py#L62) | `_mentions_handle`、`conn.cursor`、`cursor.execute`、`defaultdict`、`handles._body`、`len`、`message_body_cipher`、`people._decrypt_body`、`samples[…].append`、`sorted`、`uuid4` |
| `_population` | [98](../../backend/app/creator_import_evidence.py#L98) | `aliases.get`、`aliases.update`、`any`、`bool`、`by_contact.items`、`by_contact[…].append`、`cm.get`、`cm[…].casefold`、`conn.execute`、`conn.execute(…).fetchall`、`defaultdict`、`fact.get`、`facts.get`、`facts.get(…).get`、`handles._members`、`identities.append`、`identities.sort`、`iter`、`known_ids.add`、`len`、`name.casefold`、`name.strip`、`name_refs.items`、`name_refs[…].add`、`next`、`ownership._hash`、`presence[…].add`、`refs.append`、`sorted`、`str`、`users.append` |
| `_messages` | [189](../../backend/app/creator_import_evidence.py#L189) | `_mentions_handle`、`addressed[…].append`、`aliases.get`、`any`、`bool`、`bucket.sort`、`by_group[…].append`、`by_user[…].append`、`chosen.append`、`conn.execute`、`conn.execute(…).fetchall`、`defaultdict`、`enumerate`、`handles._CHANGE.search`、`handles._body`、`handles._body(…).strip`、`int`、`items.append`、`len`、`match.start`、`max`、`mention_targets`、`message_body_cipher`、`messages.append`、`ownership.BUSINESS.search`、`ownership._hash`、`ownership._relation_hash`、`people._IMPORTANT.findall`、`people._ROUTINE.fullmatch`、`people._decrypt_body`、`people._readable_body`、`population[…].items`、`range`、`re.escape`、`re.search`、`refs.append`、`row.get`、`seen.add`、`set`、`sorted`、`speakers.get`、`str`、`targets.add`、`targets.update` |
| `_build` | [279](../../backend/app/creator_import_evidence.py#L279) | `_candidate_groups`、`_history_groups`、`_messages`、`_population`、`_scope`、`_stable_pack`、`bool`、`enumerate`、`group.items`、`groups.append`、`handles._body`、`handles.normalize_handle`、`len`、`list`、`ownership._hash`、`refs.append`、`set`、`sorted`、`stats.update`、`str`、`sum` |
| `_stable_pack` | [355](../../backend/app/creator_import_evidence.py#L355) | `pack[…].items`、`u.get` |
| `build_group_evidence` | [363](../../backend/app/creator_import_evidence.py#L363) | `_build` |
| `build_person_evidence` | [367](../../backend/app/creator_import_evidence.py#L367) | `_build` |
| `validate_evidence` | [371](../../backend/app/creator_import_evidence.py#L371) | `_build`、`_stable_pack`、`pack.get`、`r.get` |
| `validate_references` | [384](../../backend/app/creator_import_evidence.py#L384) | `any`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`groups[…].get`、`handles._members`、`len`、`list`、`material.get`、`material.get(…).get`、`message_refs[…].get`、`ownership._hash`、`ownership._relation_hash`、`r.get`、`ref.get`、`set`、`sorted`、`stats.get`、`str` |
| `resolve_groups` | [431](../../backend/app/creator_import_evidence.py#L431) | `all`、`r.get` |
| `resolve_contact` | [436](../../backend/app/creator_import_evidence.py#L436) | `len`、`r.get` |
| `_output_format` | [507](../../backend/app/creator_import_evidence.py#L507) | `list`、`sorted` |
| `group_output_format` | [529](../../backend/app/creator_import_evidence.py#L529) | `_output_format` |
| `person_output_format` | [533](../../backend/app/creator_import_evidence.py#L533) | `_output_format` |
| `_refs` | [537](../../backend/app/creator_import_evidence.py#L537) | `ValueError`、`any`、`isinstance`、`len`、`set` |
| `_text` | [544](../../backend/app/creator_import_evidence.py#L544) | `ValueError`、`isinstance`、`len`、`value.strip` |
| `_parse` | [550](../../backend/app/creator_import_evidence.py#L550) | `ValueError`、`_output_format`、`_refs`、`_text`、`bool`、`isinstance`、`json.loads`、`len`、`pack.get`、`people._confidence`、`set` |
| `group_validate` | [570](../../backend/app/creator_import_evidence.py#L570) | `ValueError`、`_mentions_handle`、`_parse`、`_refs`、`any`、`blockers.append`、`bool`、`groups.keys`、`messages.keys`、`pack[…].get`、`result.update`、`set` |
| `person_validate` | [602](../../backend/app/creator_import_evidence.py#L602) | `ValueError`、`_parse`、`_refs`、`_text`、`all`、`any`、`blockers.append`、`bool`、`groups.keys`、`groups.values`、`isinstance`、`len`、`m.get`、`messages.keys`、`messages.values`、`pack[…].get`、`result.update`、`seen.add`、`set`、`targets.append`、`users.items` |

## backend/app/creator_import_xlsx.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `parse_xlsx` | [19](../../backend/app/creator_import_xlsx.py#L19) | `<Constant>.join`、`BytesIO`、`PurePosixPath`、`ValueError`、`ZipFile`、`all_rows[…][…].casefold`、`any`、`archive.infolist`、`c.isalpha`、`cell.find`、`cell.find(…).itertext`、`cell.findtext`、`cell.get`、`columns.add`、`filename.lower`、`filename.lower(…).endswith`、`int`、`isinstance`、`len`、`n.endswith`、`n.startswith`、`normalize_handle`、`populated.append`、`row.findall`、`row.get`、`rows.append`、`seen.add`、`set`、`sheet_rows.append`、`si.itertext`、`sorted`、`sum`、`value.strip`、`xml`、`xml(…).findall` |
| `parse_xlsx.xml` | [35](../../backend/app/creator_import_xlsx.py#L35) | `ET.fromstring`、`ValueError`、`archive.read`、`raw.upper` |

## backend/app/creator_imports.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `source_labels` | [55](../../backend/app/creator_imports.py#L55) | `<BoolOp>.get`、`row.get` |
| `_audit` | [63](../../backend/app/creator_imports.py#L63) | `Jsonb`、`conn.execute`、`json.loads`、`ownership._encode`、`uuid4` |
| `_batch` | [70](../../backend/app/creator_imports.py#L70) | `LookupError`、`ValueError`、`conn.execute`、`conn.execute(…).fetchone`、`creator_handles._access`、`ownership._uuid` |
| `_row` | [82](../../backend/app/creator_imports.py#L82) | `LookupError`、`_batch`、`conn.execute`、`conn.execute(…).fetchone`、`ownership._uuid` |
| `_accessible` | [117](../../backend/app/creator_imports.py#L117) | `bool`、`conn.execute`、`conn.execute(…).fetchone` |
| `search_users` | [125](../../backend/app/creator_imports.py#L125) | `conn.execute`、`conn.execute(…).fetchall`、`connection`、`creator_handles._access`、`str`、`str(…).strip` |
| `_identity_conflict` | [160](../../backend/app/creator_imports.py#L160) | `bool`、`conn.execute`、`conn.execute(…).fetchone` |
| `_conflict` | [173](../../backend/app/creator_imports.py#L173) | `_identity_conflict`、`bool`、`conn.execute`、`conn.execute(…).fetchone`、`row.get` |
| `_admit` | [204](../../backend/app/creator_imports.py#L204) | `PermissionError`、`ValueError`、`_accessible`、`_audit`、`_conflict`、`_invalidate_chain`、`_record_roster_identity`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`len`、`normalize_handle`、`ownership._uuid`、`row.get`、`str`、`uuid4` |
| `_record_roster_identity` | [300](../../backend/app/creator_imports.py#L300) | `conn.execute`、`conn.execute(…).fetchone` |
| `preview_import` | [323](../../backend/app/creator_imports.py#L323) | `Jsonb`、`PurePath`、`ValueError`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`creator_handles._access`、`hashlib.sha256`、`hashlib.sha256(…).hexdigest`、`ownership._uuid`、`parse_xlsx`、`str`、`uuid4` |
| `start_import` | [359](../../backend/app/creator_imports.py#L359) | `PermissionError`、`ValueError`、`_accessible`、`_admit`、`_audit`、`_batch`、`_conflict`、`_existing`、`_settle`、`batch[…].get`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`entry.get`、`isinstance`、`ownership._uuid`、`sorted`、`str`、`uuid4` |
| `confirm_row` | [432](../../backend/app/creator_imports.py#L432) | `ValueError`、`_admit`、`_row`、`_settle`、`conn.transaction`、`connection`、`ownership._uuid`、`str` |
| `defer_row` | [442](../../backend/app/creator_imports.py#L442) | `ValueError`、`_audit`、`_row`、`_settle`、`conn.execute`、`conn.transaction`、`connection`、`ownership._uuid`、`str` |
| `_existing` | [457](../../backend/app/creator_imports.py#L457) | `ValueError`、`conn.execute`、`conn.execute(…).fetchall`、`len` |
| `batch_summaries` | [474](../../backend/app/creator_imports.py#L474) | `bool`、`conn.execute`、`conn.execute(…).fetchall`、`decorated.append`、`str` |
| `_review_reason` | [523](../../backend/app/creator_imports.py#L523) | `REASONS.get` |
| `_status_label` | [531](../../backend/app/creator_imports.py#L531) | `<Dict>.get` |
| `candidate_items` | [548](../../backend/app/creator_imports.py#L548) | `<Constant>.join`、`PHASE_LABELS.get`、`_decode`、`_review_reason`、`_stage`、`_status_label`、`bool`、`chain.values`、`chains.values`、`chains[…].values`、`conn.execute`、`conn.execute(…).fetchall`、`dict.fromkeys`、`float`、`get_settings`、`list`、`material.get`、`r.get`、`ref.get`、`set`、`source_labels`、`str` |
| `page_data` | [618](../../backend/app/creator_imports.py#L618) | `batch_summaries`、`candidate_items`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`connection`、`creator_handles._access`、`int`、`math.ceil`、`max`、`min`、`str`、`str(…).strip` |
| `_decode` | [661](../../backend/app/creator_imports.py#L661) | `json.loads`、`ownership._cipher`、`ownership._cipher(…).decrypt`、`row.get` |
| `row_evidence` | [665](../../backend/app/creator_imports.py#L665) | `<BoolOp>.get`、`<Constant>.join`、`<Dict>.get`、`PHASE_LABELS.get`、`_decode`、`_review_reason`、`_row`、`_selected_chats`、`_stage`、`_status_label`、`_valid_chain`、`assessment.get`、`assessments.get`、`chain.get`、`classifications.get`、`conn.execute`、`conn.execute(…).fetchone`、`connection`、`decision.get`、`decisions.get`、`decisions.get(…).get`、`exclusions.get`、`explain`、`get_settings`、`groups.setdefault`、`groups.values`、`isinstance`、`item.get`、`item[…].append`、`json.loads`、`len`、`list`、`material[…].get`、`message.get`、`names.get`、`pack.get`、`prefix`、`raw_decision.get`、`refs.items`、`result[…].append`、`result[…].extend`、`source_labels`、`validator` |
| `row_evidence.prefix` | [741](../../backend/app/creator_imports.py#L741) | — |
| `row_evidence.explain` | [744](../../backend/app/creator_imports.py#L744) | `re.sub` |
| `_stage` | [864](../../backend/app/creator_imports.py#L864) | `<BoolOp>.get`、`row.get` |
| `_invalidate_chain` | [868](../../backend/app/creator_imports.py#L868) | `Jsonb`、`_audit`、`conn.execute`、`row.get`、`stats.get`、`str` |
| `_settle` | [891](../../backend/app/creator_imports.py#L891) | `conn.execute` |
| `pause_import` | [901](../../backend/app/creator_imports.py#L901) | `_batch`、`conn.execute`、`conn.transaction`、`connection`、`str` |
| `resume_import` | [911](../../backend/app/creator_imports.py#L911) | `_batch`、`_settle`、`conn.execute`、`conn.transaction`、`connection`、`str` |
| `recheck_batch` | [922](../../backend/app/creator_imports.py#L922) | `ValueError`、`_batch`、`_invalidate_chain`、`conn.execute`、`conn.execute(…).fetchall`、`conn.transaction`、`connection`、`len`、`ownership._uuid`、`str` |
| `_selected_chats` | [948](../../backend/app/creator_imports.py#L948) | `creator_import_evidence.resolve_groups`、`decision.get` |
| `_build_material` | [955](../../backend/app/creator_imports.py#L955) | `_selected_chats`、`creator_import_evidence.build_group_evidence`、`creator_import_evidence.build_person_evidence` |
| `_valid_chain` | [963](../../backend/app/creator_imports.py#L963) | `_build_material`、`chain.items`、`creator_import_evidence.validate_references` |
| `prepare_next_import` | [975](../../backend/app/creator_imports.py#L975) | `<BoolOp>.get`、`Jsonb`、`LOGGER.error`、`ValueError`、`_build_material`、`_decode`、`_selected_chats`、`_settle`、`_stage`、`_valid_chain`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`creator_handles._access`、`current[…].get`、`ownership._cipher`、`ownership._cipher(…).encrypt`、`ownership._config`、`ownership._config_fingerprint`、`ownership._encode`、`row[…].get`、`str` |
| `claim_next_import` | [1088](../../backend/app/creator_imports.py#L1088) | `_settle`、`ai_dispatch.can_claim`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`conn.transaction`、`connection` |
| `_current` | [1124](../../backend/app/creator_imports.py#L1124) | `_stage`、`conn.execute`、`conn.execute(…).fetchone`、`job[…].get`、`row[…].get` |
| `_record_response` | [1147](../../backend/app/creator_imports.py#L1147) | `Jsonb`、`_audit`、`_stage`、`conn.execute`、`conn.execute(…).fetchone`、`job[…].get`、`ownership._cipher`、`ownership._cipher(…).encrypt`、`ownership._encode`、`response.get`、`response.get(…).get`、`row[…].get`、`str`、`usage.get` |
| `_finish` | [1181](../../backend/app/creator_imports.py#L1181) | `<BoolOp>.get`、`Jsonb`、`ValueError`、`_admit`、`_current`、`_decode`、`_record_response`、`_settle`、`_stage`、`_valid_chain`、`any`、`bool`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`creator_handles._access`、`creator_import_evidence.resolve_contact`、`g.get`、`ownership._cipher`、`ownership._cipher(…).encrypt`、`ownership._config`、`ownership._config_fingerprint`、`ownership._encode`、`result.get`、`str` |
| `run_claimed_import` | [1255](../../backend/app/creator_imports.py#L1255) | `LOGGER.error`、`ValueError`、`_current`、`_decode`、`_finish`、`_stage`、`_valid_chain`、`ai_dispatch.pause_for_quota`、`ai_dispatch.throttle`、`api_key_cipher`、`api_key_cipher(…).decrypt`、`conn.execute`、`conn.transaction`、`connection`、`contact_assessments.call_openai`、`creator_handles._access`、`creator_import_evidence.group_output_format`、`creator_import_evidence.person_output_format`、`max`、`min`、`ownership._config`、`ownership._config_fingerprint`、`str`、`validator` |

## backend/app/creator_ownership_evidence.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `_hash` | [72](../../backend/app/creator_ownership_evidence.py#L72) | `hashlib.sha256`、`hashlib.sha256(…).hexdigest`、`json.dumps`、`json.dumps(…).encode` |
| `_scope` | [77](../../backend/app/creator_ownership_evidence.py#L77) | `<BoolOp>.values`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`names.update` |
| `_load_rows` | [98](../../backend/app/creator_ownership_evidence.py#L98) | `aliases.items`、`conn.execute`、`conn.execute(…).fetchall`、`list` |
| `_source_hash` | [132](../../backend/app/creator_ownership_evidence.py#L132) | `_hash`、`_relation_hash`、`people._decrypt_body` |
| `_text` | [141](../../backend/app/creator_ownership_evidence.py#L141) | `<Constant>.join`、`aliases.get`、`fragments`、`json.loads`、`len`、`mention_aliases.get`、`mention_targets`、`name.strip`、`names.items`、`people._ACCOUNT.sub`、`people._ADDRESS.sub`、`people._EMAIL.sub`、`people._PHONE.sub`、`people._readable_body`、`re.escape`、`re.sub`、`re.sub(…).strip`、`resolve_mentions`、`row.get` |
| `_text.fragments` | [146](../../backend/app/creator_ownership_evidence.py#L146) | `fragments`、`isinstance`、`rich_mention_text`、`row.get`、`value.get`、`value.items` |
| `_text.safe_url` | [178](../../backend/app/creator_ownership_evidence.py#L178) | `urlsplit`、`urlunsplit` |
| `_relation_hash` | [188](../../backend/app/creator_ownership_evidence.py#L188) | `_hash`、`row.get` |
| `_substantive` | [195](../../backend/app/creator_ownership_evidence.py#L195) | `bool`、`c.isalpha`、`people._ROUTINE.fullmatch`、`re.sub`、`re.sub(…).strip`、`sum` |
| `_tokens` | [200](../../backend/app/creator_ownership_evidence.py#L200) | `re.findall`、`text.casefold` |
| `_segments` | [204](../../backend/app/creator_ownership_evidence.py#L204) | `<DictComp>.values`、`BUSINESS.fullmatch`、`RESPONSE.search`、`_tokens`、`abs`、`anchor[…].get`、`any`、`bool`、`by_chat.values`、`by_chat[…].append`、`defaultdict`、`enumerate`、`extra[…].get`、`list`、`lookup.get`、`max`、`min`、`row.get`、`segments.append`、`selected.append`、`selected.insert`、`set`、`timedelta` |
| `_select` | [271](../../backend/app/creator_ownership_evidence.py#L271) | `len`、`seen.update`、`selected.append`、`set`、`sorted`、`sum` |
| `_pack` | [302](../../backend/app/creator_ownership_evidence.py#L302) | `_hash`、`_relation_hash`、`_segments`、`_select`、`aliases.get`、`any`、`conversations.setdefault`、`enumerate`、`isinstance`、`item.get`、`item[…].isoformat`、`labels.get`、`len`、`max`、`mention_targets`、`messages.append`、`meta.items`、`meta.update`、`min`、`ref.update`、`refs.append`、`row.get`、`sorted`、`str`、`sum`、`tags.add`、`unique.values`、`v.isoformat` |
| `build_evidence` | [360](../../backend/app/creator_ownership_evidence.py#L360) | `BROADCAST.search`、`BUSINESS.search`、`NEGATIVE.search`、`RESPONSIBILITY.search`、`_hash`、`_load_rows`、`_pack`、`_scope`、`_source_hash`、`_substantive`、`_text`、`aliases.get`、`aliases.items`、`any`、`bool`、`connection`、`datetime.min.replace`、`dict`、`hashlib.sha256`、`hashlib.sha256(…).hexdigest`、`int`、`items.append`、`len`、`mention_targets`、`message_body_cipher`、`people._decrypt_body`、`raw.encode`、`row.get`、`str` |
| `revalidate_refs` | [419](../../backend/app/creator_ownership_evidence.py#L419) | `Jsonb`、`_hash`、`_load_rows`、`_relation_hash`、`_scope`、`_source_hash`、`any`、`conn.execute`、`conn.execute(…).fetchall`、`hashlib.sha256`、`hashlib.sha256(…).hexdigest`、`isinstance`、`len`、`lookup.get`、`message_body_cipher`、`people._decrypt_body`、`range`、`raw.encode`、`ref.items`、`ref.keys`、`str` |

## backend/app/creator_ownership.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `_uuid` | [71](../../backend/app/creator_ownership.py#L71) | `UUID`、`ValueError`、`str` |
| `_cipher` | [78](../../backend/app/creator_ownership.py#L78) | `TokenCipher`、`hmac.digest` |
| `_encode` | [84](../../backend/app/creator_ownership.py#L84) | `json.dumps` |
| `_owner` | [88](../../backend/app/creator_ownership.py#L88) | `PermissionError`、`conn.execute`、`conn.execute(…).fetchone` |
| `_cm` | [97](../../backend/app/creator_ownership.py#L97) | `conn.execute`、`conn.execute(…).fetchone` |
| `_owned` | [105](../../backend/app/creator_ownership.py#L105) | `bool`、`conn.execute`、`conn.execute(…).fetchone` |
| `_creator` | [117](../../backend/app/creator_ownership.py#L117) | `bool`、`conn.execute`、`conn.execute(…).fetchone` |
| `_config` | [127](../../backend/app/creator_ownership.py#L127) | `conn.execute`、`conn.execute(…).fetchone` |
| `_config_fingerprint` | [135](../../backend/app/creator_ownership.py#L135) | `_encode`、`_encode(…).encode`、`config.get`、`hashlib.sha256`、`hashlib.sha256(…).hexdigest` |
| `_audit` | [139](../../backend/app/creator_ownership.py#L139) | `Jsonb`、`conn.execute`、`uuid4` |
| `create_preview` | [146](../../backend/app/creator_ownership.py#L146) | `ValueError`、`_audit`、`_cm`、`_owner`、`_uuid`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`len`、`str`、`sum`、`uuid4` |
| `_refs` | [204](../../backend/app/creator_ownership.py#L204) | `conn.execute`、`conn.execute(…).fetchall`、`len` |
| `_valid_material` | [215](../../backend/app/creator_ownership.py#L215) | `_refs`、`bool`、`revalidate_refs` |
| `_eligible_now` | [224](../../backend/app/creator_ownership.py#L224) | `_cm`、`_creator`、`_owned` |
| `_settle` | [234](../../backend/app/creator_ownership.py#L234) | `conn.execute` |
| `prepare_next_preview` | [249](../../backend/app/creator_ownership.py#L249) | `Jsonb`、`LOGGER.warning`、`ValueError`、`_cipher`、`_cipher(…).encrypt`、`_config`、`_config_fingerprint`、`_eligible_now`、`_encode`、`_owner`、`_settle`、`_valid_material`、`bool`、`build_evidence`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`json.loads`、`len`、`revalidate_refs` |
| `_run_for_owner` | [354](../../backend/app/creator_ownership.py#L354) | `LookupError`、`ValueError`、`_cm`、`_owner`、`conn.execute`、`conn.execute(…).fetchone` |
| `start_run` | [364](../../backend/app/creator_ownership.py#L364) | `LookupError`、`ValueError`、`_audit`、`_config`、`_config_fingerprint`、`_eligible_now`、`_run_for_owner`、`_uuid`、`_valid_material`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`get_settings`、`seen.add`、`selected.append`、`selected.extend`、`set`、`str`、`t[…].get` |
| `pause_run` | [439](../../backend/app/creator_ownership.py#L439) | `_audit`、`_run_for_owner`、`_uuid`、`conn.execute`、`conn.transaction`、`connection`、`str` |
| `resume_run` | [448](../../backend/app/creator_ownership.py#L448) | `_audit`、`_run_for_owner`、`_settle`、`_uuid`、`conn.execute`、`conn.transaction`、`connection`、`str` |
| `claim_next_ownership` | [458](../../backend/app/creator_ownership.py#L458) | `_settle`、`ai_dispatch.can_claim`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`conn.transaction`、`connection` |
| `_output_format` | [525](../../backend/app/creator_ownership.py#L525) | `list` |
| `_citation_support` | [561](../../backend/app/creator_ownership.py#L561) | `<BinOp>.total_seconds`、`BUSINESS.search`、`_substantive`、`abs`、`affirmative.search`、`any`、`datetime.fromisoformat`、`days.add`、`directed`、`first.get`、`len`、`m.get`、`messages[…].get`、`min`、`min(…).date`、`negative.search`、`re.compile`、`second.get`、`set`、`sorted`、`used.update`、`zip` |
| `_citation_support.directed` | [568](../../backend/app/creator_ownership.py#L568) | `any`、`bool`、`m.get`、`message.get`、`messages.get` |
| `_validate_result` | [617](../../backend/app/creator_ownership.py#L617) | `ValueError`、`_citation_support`、`_output_format`、`any`、`isinstance`、`json.loads`、`len`、`math.isfinite`、`result[…].strip`、`set` |
| `_current_paid` | [678](../../backend/app/creator_ownership.py#L678) | `conn.execute`、`conn.execute(…).fetchone` |
| `_finish_job` | [689](../../backend/app/creator_ownership.py#L689) | `_cipher`、`_cipher(…).encrypt`、`_config`、`_config_fingerprint`、`_current_paid`、`_eligible_now`、`_encode`、`_owner`、`_settle`、`_valid_material`、`conn.execute`、`conn.transaction`、`connection`、`response.get`、`telemetry.get`、`usage.get` |
| `run_claimed_ownership` | [730](../../backend/app/creator_ownership.py#L730) | `LOGGER.warning`、`_cipher`、`_cipher(…).decrypt`、`_config`、`_config_fingerprint`、`_current_paid`、`_eligible_now`、`_finish_job`、`_output_format`、`_owner`、`_valid_material`、`_validate_result`、`ai_dispatch.pause_for_quota`、`ai_dispatch.throttle`、`api_key_cipher`、`api_key_cipher(…).decrypt`、`conn.execute`、`conn.transaction`、`connection`、`contact_assessments.call_openai`、`exc.telemetry.get`、`json.loads`、`max`、`min` |
| `_review_tasks` | [800](../../backend/app/creator_ownership.py#L800) | `ValueError`、`_run_for_owner`、`_uuid`、`conn.execute`、`conn.execute(…).fetchall`、`len`、`set` |
| `confirm_tasks` | [814](../../backend/app/creator_ownership.py#L814) | `ValueError` |
| `defer_tasks` | [819](../../backend/app/creator_ownership.py#L819) | `ValueError`、`_audit`、`_review_tasks`、`_uuid`、`any`、`conn.execute`、`conn.transaction`、`connection`、`len`、`str` |
| `page_data` | [838](../../backend/app/creator_ownership.py#L838) | `LookupError`、`REASONS.get`、`RESULT_LABELS.get`、`ValueError`、`_cipher`、`_cipher(…).decrypt`、`_config`、`_uuid`、`_valid_material`、`any`、`bool`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`connection`、`counts.update`、`dict.fromkeys`、`float`、`get_settings`、`int`、`items.append`、`json.loads`、`math.ceil`、`max`、`min`、`params.append`、`params.extend`、`private_chat_url`、`str`、`str(…).strip` |
| `task_evidence` | [1024](../../backend/app/creator_ownership.py#L1024) | `LookupError`、`REASONS.get`、`ValueError`、`_audit`、`_cipher`、`_cipher(…).decrypt`、`_cm`、`_owner`、`_refs`、`_uuid`、`_valid_material`、`admin_reporting._message_time`、`admin_reporting._readable_body`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`get_settings`、`int`、`json.loads`、`message_body_cipher`、`message_body_cipher(…).decrypt`、`messages.append`、`result.get`、`sorted`、`str` |

## backend/app/crypto.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `TokenCipher.__init__` | [18](../../backend/app/crypto.py#L18) | `AESGCM`、`ValueError`、`len` |
| `TokenCipher.encrypt` | [24](../../backend/app/crypto.py#L24) | `base64.urlsafe_b64encode`、`base64.urlsafe_b64encode(…).decode`、`base64.urlsafe_b64encode(…).decode(…).rstrip`、`os.urandom`、`plaintext.encode`、`self._cipher.encrypt` |
| `TokenCipher.decrypt` | [29](../../backend/app/crypto.py#L29) | `ValueError`、`base64.urlsafe_b64decode`、`len`、`padded.encode`、`self._cipher.decrypt`、`self._cipher.decrypt(…).decode` |
| `message_body_cipher` | [37](../../backend/app/crypto.py#L37) | `TokenCipher`、`hmac.digest` |

## backend/app/db.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `connection` | [15](../../backend/app/db.py#L15) | `conn.close`、`get_settings`、`psycopg.connect` |
| `database_is_ready` | [23](../../backend/app/db.py#L23) | `conn.cursor`、`connection`、`cursor.execute`、`cursor.fetchone` |

## backend/app/identity_names.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `_raw` | [6](../../backend/app/identity_names.py#L6) | `isinstance`、`value.strip` |
| `format_display_name` | [10](../../backend/app/identity_names.py#L10) | `<BoolOp>.split`、`<Constant>.join`、`_raw`、`english.casefold`、`original.casefold` |
| `update_identity_names` | [19](../../backend/app/identity_names.py#L19) | `_raw`、`conn.execute`、`conn.execute(…).fetchone`、`format_display_name` |

## backend/app/identity_profiles.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `safe_avatar_url` | [22](../../backend/app/identity_profiles.py#L22) | `<BoolOp>.lower`、`any`、`host.endswith`、`host.startswith`、`isinstance`、`len`、`ord`、`urlsplit`、`value.strip` |
| `store_profile_avatar` | [40](../../backend/app/identity_profiles.py#L40) | `conn.execute`、`safe_avatar_url` |
| `claim_next` | [62](../../backend/app/identity_profiles.py#L62) | `conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`uuid4` |
| `_source` | [94](../../backend/app/identity_profiles.py#L94) | `conn.execute`、`conn.execute(…).fetchone`、`list` |
| `_scope_current` | [117](../../backend/app/identity_profiles.py#L117) | `bool`、`conn.execute`、`conn.execute(…).fetchone` |
| `_provider_gate` | [162](../../backend/app/identity_profiles.py#L162) | `conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection` |
| `_wait_avatar_slot` | [187](../../backend/app/identity_profiles.py#L187) | `conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`float`、`max`、`min`、`time.sleep` |
| `_outcome` | [202](../../backend/app/identity_profiles.py#L202) | `conn.execute` |
| `_local_outcome` | [211](../../backend/app/identity_profiles.py#L211) | `_outcome`、`conn.transaction`、`connection` |
| `_provider_result` | [216](../../backend/app/identity_profiles.py#L216) | `conn.execute` |
| `_skip_denied_directory` | [224](../../backend/app/identity_profiles.py#L224) | `conn.execute`、`conn.transaction`、`connection` |
| `_finish_response` | [274](../../backend/app/identity_profiles.py#L274) | `_outcome`、`_provider_result`、`_scope_current`、`_skip_denied_directory`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`max`、`store_profile_avatar`、`update_identity_names` |
| `run_claimed` | [321](../../backend/app/identity_profiles.py#L321) | `LOGGER.warning`、`TokenCipher`、`TokenCipher(…).decrypt`、`_authorization`、`_finish_response`、`_local_outcome`、`_provider_gate`、`_scope_current`、`_skip_denied_directory`、`_source`、`_usable_access_token`、`_wait_avatar_slot`、`client.contact_user`、`client.user_info`、`conn.execute`、`conn.execute(…).fetchone`、`connection`、`dict`、`excluded.append`、`range` |
| `fill_slots` | [386](../../backend/app/identity_profiles.py#L386) | `LOGGER.warning`、`claim_next`、`executor.submit`、`future.done`、`future.result`、`futures.add`、`futures.remove`、`len`、`tuple` |

## backend/app/lark_api.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `private_chat_url` | [19](../../backend/app/lark_api.py#L19) | `isinstance`、`re.fullmatch`、`urlencode` |
| `LarkApiError.__init__` | [29](../../backend/app/lark_api.py#L29) | `super`、`super(…).__init__` |
| `LarkApiError.retryable` | [36](../../backend/app/lark_api.py#L36) | — |
| `LarkApiError.rate_limited` | [40](../../backend/app/lark_api.py#L40) | — |
| `LarkApiError.likely_auth_problem` | [46](../../backend/app/lark_api.py#L46) | — |
| `LarkProfile.display_name` | [79](../../backend/app/lark_api.py#L79) | `format_display_name` |
| `LarkClient.__init__` | [86](../../backend/app/lark_api.py#L86) | — |
| `LarkClient.authorization_url` | [97](../../backend/app/lark_api.py#L97) | `<Constant>.join`、`urlencode` |
| `LarkClient.exchange_code` | [109](../../backend/app/lark_api.py#L109) | `<Constant>.join`、`self._require_secret`、`self._token_request` |
| `LarkClient.refresh_token` | [122](../../backend/app/lark_api.py#L122) | `self._require_secret`、`self._token_request` |
| `LarkClient.user_info` | [132](../../backend/app/lark_api.py#L132) | `LarkApiError`、`LarkProfile`、`_string`、`data.get`、`self._request` |
| `LarkClient.contact_user` | [147](../../backend/app/lark_api.py#L147) | `<BoolOp>.get`、`LarkApiError`、`LarkProfile`、`_string`、`data.get`、`isinstance`、`next`、`quote`、`self._request`、`user.get` |
| `LarkClient.list_chats` | [176](../../backend/app/lark_api.py#L176) | `self._request` |
| `LarkClient.list_chat_messages` | [196](../../backend/app/lark_api.py#L196) | `end_at.timestamp`、`int`、`self._request`、`start_at.timestamp` |
| `LarkClient.list_chat_members` | [226](../../backend/app/lark_api.py#L226) | `quote`、`self._request` |
| `LarkClient.tenant_access_token` | [245](../../backend/app/lark_api.py#L245) | `LarkApiError`、`_string`、`data.get`、`self._request`、`self._require_secret` |
| `LarkClient.list_collaboration_tenants` | [259](../../backend/app/lark_api.py#L259) | `self._request` |
| `LarkClient._token_request` | [274](../../backend/app/lark_api.py#L274) | `LarkApiError`、`TokenBundle`、`_int`、`_optional_int`、`_string`、`data.get`、`self._request` |
| `LarkClient._request` | [287](../../backend/app/lark_api.py#L287) | `LarkApiError`、`_optional_int`、`_rate_limit_reset`、`_string`、`client.request`、`httpx.Client`、`httpx.Timeout`、`isinstance`、`nullcontext`、`raw.get`、`response.json`、`self._admission.request`、`self._http_client.request`、`self._pause_for_rate_limit` |
| `LarkClient._require_secret` | [336](../../backend/app/lark_api.py#L336) | `RuntimeError` |
| `LarkClient._pause_for_rate_limit` | [340](../../backend/app/lark_api.py#L340) | `self._admission.pause` |
| `_string` | [345](../../backend/app/lark_api.py#L345) | `isinstance`、`value.strip` |
| `_int` | [349](../../backend/app/lark_api.py#L349) | `_optional_int` |
| `_optional_int` | [354](../../backend/app/lark_api.py#L354) | `int` |
| `_rate_limit_reset` | [361](../../backend/app/lark_api.py#L361) | `_optional_int`、`headers.get` |

## backend/app/lark_sync_admission.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `LarkSyncAdmission.__init__` | [20](../../backend/app/lark_sync_admission.py#L20) | `timedelta` |
| `LarkSyncAdmission.request` | [25](../../backend/app/lark_sync_admission.py#L25) | `self._acquire`、`self._release` |
| `LarkSyncAdmission.pause` | [32](../../backend/app/lark_sync_admission.py#L32) | `conn.execute`、`conn.transaction`、`connection`、`max` |
| `LarkSyncAdmission._acquire` | [50](../../backend/app/lark_sync_admission.py#L50) | `<BinOp>.total_seconds`、`RuntimeError`、`_as_datetime`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`int`、`max`、`min`、`self.slot_interval.total_seconds`、`time.sleep`、`uuid.uuid4` |
| `LarkSyncAdmission._release` | [97](../../backend/app/lark_sync_admission.py#L97) | `conn.execute`、`conn.transaction`、`connection` |
| `_as_datetime` | [102](../../backend/app/lark_sync_admission.py#L102) | `RuntimeError`、`isinstance` |

## backend/app/main.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `_template` | [49](../../backend/app/main.py#L49) | `templates.TemplateResponse` |
| `healthz` | [54](../../backend/app/main.py#L54) | `HTTPException`、`database_is_ready`、`get_settings` |
| `index` | [66](../../backend/app/main.py#L66) | `RedirectResponse`、`_current_authorization` |
| `start_lark_authorization` | [74](../../backend/app/main.py#L74) | `LarkClient`、`LarkClient(…).authorization_url`、`RedirectResponse`、`TokenCipher`、`_code_challenge`、`_sha256`、`_template`、`cipher.encrypt`、`conn.execute`、`conn.transaction`、`connection`、`get_settings`、`response.set_cookie`、`secrets.token_urlsafe` |
| `lark_callback` | [114](../../backend/app/main.py#L114) | `LarkClient`、`RedirectResponse`、`TokenCipher`、`_audit`、`_expiry`、`_queue_job`、`_reset_chat_backfill`、`_sha256`、`_template`、`bool`、`cipher.decrypt`、`cipher.encrypt`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`get_settings`、`hmac.compare_digest`、`lark.exchange_code`、`lark.user_info`、`request.cookies.get`、`response.delete_cookie`、`response.set_cookie`、`safe_avatar_url`、`secrets.token_urlsafe`、`set`、`set(…).issubset`、`store_profile_avatar`、`tokens.scope.split`、`update_identity_names`、`uuid.uuid4` |
| `_reset_chat_backfill` | [270](../../backend/app/main.py#L270) | `conn.execute` |
| `dashboard` | [296](../../backend/app/main.py#L296) | `RedirectResponse`、`_admin_csrf_token`、`_current_authorization`、`_dashboard_stats`、`_is_remi_tester`、`_template`、`admin_permissions.access_for`、`authorization.get`、`bool` |
| `_is_remi_tester` | [328](../../backend/app/main.py#L328) | `admin_access.get`、`authorization.get`、`bool`、`str` |
| `dashboard_stats` | [341](../../backend/app/main.py#L341) | `HTTPException`、`_current_authorization`、`_dashboard_stats`、`_template` |
| `_current_authorization` | [348](../../backend/app/main.py#L348) | `_sha256`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`request.cookies.get`、`row.pop` |
| `_dashboard_stats` | [373](../../backend/app/main.py#L373) | `_format_time`、`conn.execute`、`conn.execute(…).fetchone`、`connection`、`dict`、`facts.get`、`sync_summary` |
| `_queue_job` | [464](../../backend/app/main.py#L464) | `conn.execute`、`conn.execute(…).fetchone`、`uuid.uuid4` |
| `_audit` | [494](../../backend/app/main.py#L494) | `conn.execute`、`uuid.uuid4` |
| `_sha256` | [501](../../backend/app/main.py#L501) | `hashlib.sha256`、`hashlib.sha256(…).hexdigest`、`value.encode` |
| `_code_challenge` | [505](../../backend/app/main.py#L505) | `base64.urlsafe_b64encode`、`base64.urlsafe_b64encode(…).decode`、`base64.urlsafe_b64encode(…).decode(…).rstrip`、`hashlib.sha256`、`hashlib.sha256(…).digest`、`verifier.encode` |
| `_expiry` | [510](../../backend/app/main.py#L510) | `datetime.now`、`timedelta` |
| `_format_time` | [514](../../backend/app/main.py#L514) | `value.astimezone`、`value.astimezone(…).strftime` |
| `_admin_csrf_token` | [520](../../backend/app/main.py#L520) | `<JoinedStr>.encode`、`get_settings`、`hmac.new`、`hmac.new(…).hexdigest` |

## backend/app/message_export.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `safe_name` | [24](../../backend/app/message_export.py#L24) | `re.sub`、`re.sub(…).strip` |
| `write_conversation` | [28](../../backend/app/message_export.py#L28) | `Alignment`、`ILLEGAL_CHARACTERS_RE.sub`、`TemporaryFile`、`ValueError`、`Workbook`、`WriteOnlyCell`、`archive.open`、`book.create_sheet`、`book.save`、`cells.append`、`len`、`output.seek`、`range`、`sheet.append`、`shutil.copyfileobj`、`str` |
| `ZipBuffer.__init__` | [63](../../backend/app/message_export.py#L63) | `TemporaryFile` |
| `ZipBuffer.writable` | [67](../../backend/app/message_export.py#L67) | — |
| `ZipBuffer.tell` | [70](../../backend/app/message_export.py#L70) | — |
| `ZipBuffer.write` | [73](../../backend/app/message_export.py#L73) | `len`、`self.buffer.write` |
| `ZipBuffer.drain` | [77](../../backend/app/message_export.py#L77) | `self.buffer.read`、`self.buffer.seek`、`self.buffer.truncate` |
| `ZipBuffer.close` | [84](../../backend/app/message_export.py#L84) | `self.buffer.close`、`super`、`super(…).close` |
| `stream_export` | [89](../../backend/app/message_export.py#L89) | `ZipBuffer`、`ZipFile`、`_write_export`、`conn.transaction`、`connection`、`get_settings`、`message_body_cipher`、`output.drain` |
| `_write_export` | [98](../../backend/app/message_export.py#L98) | `ValueError`、`chain`、`check_access`、`conn.cursor`、`conn.execute`、`cursor.execute`、`first.get`、`groupby`、`json.dumps`、`next`、`output.drain`、`safe_name`、`str`、`text_rows`、`uuid4`、`write_conversation` |
| `_write_export.text_rows` | [124](../../backend/app/message_export.py#L124) | `<Constant>.join`、`cipher.decrypt`、`islice`、`json.loads`、`list`、`mention_names`、`reporting._body_text_values`、`reporting.resolve_mentions`、`row.get`、`text.strip`、`timestamp.astimezone`、`timestamp.astimezone(…).isoformat` |

## backend/app/message_mentions.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `_items` | [22](../../backend/app/message_mentions.py#L22) | `isinstance`、`json.loads` |
| `_name` | [31](../../backend/app/message_mentions.py#L31) | `<Constant>.join`、`_KEY.fullmatch`、`isinstance`、`value.split` |
| `_identity` | [38](../../backend/app/message_mentions.py#L38) | `isinstance`、`item.get`、`value.strip` |
| `mention_targets` | [43](../../backend/app/message_mentions.py#L43) | `<BoolOp>.get`、`_KEY.fullmatch`、`_identity`、`_items`、`_name`、`grouped.items`、`grouped.setdefault`、`grouped.setdefault(…).append`、`i.get`、`isinstance`、`item.get`、`iter`、`len`、`next`、`result.append`、`str` |
| `mention_names` | [69](../../backend/app/message_mentions.py#L69) | `_identity`、`_items`、`_name`、`conn.execute`、`conn.execute(…).fetchall`、`item.get`、`row.get`、`sorted` |
| `resolve_mentions` | [96](../../backend/app/message_mentions.py#L96) | `<Constant>.join`、`_LITERAL.finditer`、`_TOKEN.sub`、`item[…].lstrip`、`literal.end`、`literal.start`、`mention_targets`、`parts.append`、`parts.extend`、`replacements.get` |
| `rich_mention_text` | [113](../../backend/app/message_mentions.py#L113) | `iter`、`len`、`mention_targets`、`next`、`next(…).lstrip` |

## backend/app/offhours_summary_routes.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `create_router` | [15](../../backend/app/offhours_summary_routes.py#L15) | `APIRouter` |
| `create_router.secure` | [18](../../backend/app/offhours_summary_routes.py#L18) | — |
| `create_router.read` | [30](../../backend/app/offhours_summary_routes.py#L30) | `PermissionError`、`conn.transaction`、`connection`、`current_authorization`、`jsonable_encoder`、`offhours_summary.SummaryInputError`、`offhours_summary.get_summary_view`、`request.query_params.get`、`summary_periods.selection` |
| `create_router.denied` | [55](../../backend/app/offhours_summary_routes.py#L55) | `JSONResponse`、`secure`、`str` |
| `create_router.page` | [59](../../backend/app/offhours_summary_routes.py#L59) | `RedirectResponse`、`csrf_token`、`current_authorization`、`denied`、`read`、`render_template`、`secure` |
| `create_router.data` | [79](../../backend/app/offhours_summary_routes.py#L79) | `JSONResponse`、`Response`、`denied`、`hashlib.sha256`、`hashlib.sha256(…).hexdigest`、`read`、`request.headers.get`、`secure` |
| `create_router.reply_actor` | [91](../../backend/app/offhours_summary_routes.py#L91) | `PermissionError`、`_csrf_matches`、`csrf_token`、`current_authorization`、`request.headers.get` |
| `create_router.reply_create` | [101](../../backend/app/offhours_summary_routes.py#L101) | `JSONResponse`、`ValueError`、`body.get`、`bytearray`、`content.extend`、`denied`、`isinstance`、`json.loads`、`len`、`reply_actor`、`request.stream`、`run_in_threadpool`、`secure`、`str` |
| `create_router.reply_create.submit` | [115](../../backend/app/offhours_summary_routes.py#L115) | `body.get`、`conn.transaction`、`connection`、`offhours_summary.get_settings`、`summary_replies.enqueue` |
| `create_router.reply_read` | [143](../../backend/app/offhours_summary_routes.py#L143) | `JSONResponse`、`conn.transaction`、`connection`、`denied`、`jsonable_encoder`、`offhours_summary.get_settings`、`reply_actor`、`secure`、`summary_replies.read` |

## backend/app/offhours_summary.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `_cipher` | [44](../../backend/app/offhours_summary.py#L44) | `TokenCipher`、`hmac.digest` |
| `_hash` | [48](../../backend/app/offhours_summary.py#L48) | `hashlib.sha256`、`hashlib.sha256(…).hexdigest`、`json.dumps`、`json.dumps(…).encode` |
| `_authorize` | [52](../../backend/app/offhours_summary.py#L52) | `PermissionError`、`SummaryInputError`、`UUID`、`bool`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`dict`、`sorted`、`str` |
| `_config` | [91](../../backend/app/offhours_summary.py#L91) | `conn.execute`、`conn.execute(…).fetchone` |
| `_enabled` | [99](../../backend/app/offhours_summary.py#L99) | `_config`、`bool` |
| `_parse_window` | [104](../../backend/app/offhours_summary.py#L104) | `SummaryInputError`、`datetime.fromisoformat`、`timedelta`、`tuple`、`value.astimezone`、`value.astimezone(…).astimezone`、`value.astimezone(…).astimezone(…).replace`、`value.replace` |
| `get_settings_view` | [121](../../backend/app/offhours_summary.py#L121) | `<BoolOp>.get`、`_enabled`、`bool`、`conn.execute`、`conn.execute(…).fetchone`、`row[…].astimezone`、`row[…].astimezone(…).strftime`、`str` |
| `save_settings_and_enqueue` | [139](../../backend/app/offhours_summary.py#L139) | `SummaryInputError`、`_authorize`、`_config`、`_enabled`、`_hash`、`_parse_window`、`conn.execute`、`conn.execute(…).fetchone`、`fields.get`、`schedule_if_due`、`str`、`uuid4` |
| `_roster` | [205](../../backend/app/offhours_summary.py#L205) | `conn.execute`、`conn.execute(…).fetchall` |
| `_related_chats` | [242](../../backend/app/offhours_summary.py#L242) | `SummaryInputError`、`UUID`、`any`、`chat[…].add`、`chats.values`、`chats[…][…].add`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`contacts.get`、`creator_import_evidence._population`、`creator_import_evidence.resolve_contact`、`creator_imports._conflict`、`creator_imports._decode`、`creator_imports._selected_chats`、`creator_imports._valid_chain`、`decisions.get`、`decisions.get(…).get`、`decisions[…].get`、`float`、`get_settings`、`identities.get`、`identities.values`、`iter`、`len`、`list`、`next`、`population[…].get`、`ref.get`、`row.get`、`set`、`sorted`、`stats.get`、`str` |
| `_body` | [369](../../backend/app/offhours_summary.py#L369) | `<Constant>.join`、`cipher.decrypt`、`contact_assessments._readable_body`、`fragments`、`json.loads`、`re.sub`、`resolve_mentions`、`row.get`、`text.strip` |
| `_body.fragments` | [381](../../backend/app/offhours_summary.py#L381) | `fragments`、`isinstance`、`item.get`、`item.items`、`rich_mention_text`、`row.get` |
| `_load_chat` | [402](../../backend/app/offhours_summary.py#L402) | `SummaryInputError`、`UUID`、`_authorize`、`_body`、`_hash`、`_related_chats`、`_roster`、`body.startswith`、`bool`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`datetime.min.replace`、`enumerate`、`get_settings`、`len`、`list`、`max`、`mention_names`、`mention_targets`、`message_body_cipher`、`messages.append`、`next`、`people.values`、`people[…][…].append`、`r.get`、`relevant_opens.add`、`relevant_opens.update`、`reversed`、`row.get`、`row[…].isoformat`、`run.get`、`run[…].isoformat`、`sorted`、`speaker_labels.get`、`str`、`timedelta`、`users.items` |
| `schedule_if_due` | [561](../../backend/app/offhours_summary.py#L561) | `_enabled`、`_schedule_run`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`datetime.now`、`dict`、`seed_due` |
| `_schedule_run` | [593](../../backend/app/offhours_summary.py#L593) | `_authorize`、`_load_chat`、`_related_chats`、`_roster`、`any`、`bool`、`conn.execute`、`conn.execute(…).fetchone`、`cutoffs.append`、`datetime.min.replace`、`dict`、`dict.fromkeys`、`gaps.append`、`json.dumps`、`len`、`list`、`max`、`min`、`min(…).isoformat`、`selected_ids.append`、`state.get`、`sum`、`timedelta`、`uuid4` |
| `claim_next_summary` | [693](../../backend/app/offhours_summary.py#L693) | `_enabled`、`ai_dispatch.can_claim`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`dict` |
| `handling_fields` | [766](../../backend/app/offhours_summary.py#L766) | `list` |
| `validate_handling` | [775](../../backend/app/offhours_summary.py#L775) | `InvalidModelOutput`、`any`、`isinstance`、`item.get`、`len`、`messages[…].get`、`messages[…].get(…).strip`、`reason.strip` |
| `_format` | [793](../../backend/app/offhours_summary.py#L793) | `handling_fields`、`list` |
| `_validate` | [836](../../backend/app/offhours_summary.py#L836) | `<Constant>.join`、`<Constant>.join(…).casefold`、`InvalidModelOutput`、`_format`、`all`、`any`、`bool`、`dict.fromkeys`、`evidence[…].get`、`isinstance`、`item[…].lstrip`、`item[…].lstrip(…).casefold`、`item[…].strip`、`json.loads`、`len`、`list`、`m.get`、`messages[…].get`、`result.append`、`seen.add`、`set`、`sorted`、`t.get`、`tuple`、`type`、`validate_handling` |
| `_current` | [923](../../backend/app/offhours_summary.py#L923) | `SummaryInputError`、`_enabled`、`conn.execute`、`conn.execute(…).fetchone` |
| `_finish` | [942](../../backend/app/offhours_summary.py#L942) | `_enabled`、`conn.execute`、`conn.transaction`、`connection` |
| `run_claimed_summary` | [965](../../backend/app/offhours_summary.py#L965) | `summary_dialogues.run` |
| `prepare_next_summary` | [970](../../backend/app/offhours_summary.py#L970) | `conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`schedule_if_due`、`summary_dialogues.schedule_merges` |
| `get_summary_view` | [980](../../backend/app/offhours_summary.py#L980) | `<Dict>.get`、`UUID`、`_authorize`、`_enabled`、`_parse_window`、`_related_chats`、`_roster`、`ai_dispatch.status`、`all`、`any`、`bool`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`coverage.get`、`datetime.now`、`dict`、`dict.fromkeys`、`end.isoformat`、`gaps.append`、`get_settings`、`json.dumps`、`len`、`list`、`row.get`、`row[…].isoformat`、`start.isoformat`、`str`、`summary_dialogues.view_events`、`uuid4`、`view[…].extend`、`view[…].sort`、`window` |

## backend/app/organization_sync.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `sync_if_due` | [24](../../backend/app/organization_sync.py#L24) | `LOGGER.info`、`_fetch_all`、`_record_state`、`_refresh_known_links`、`_store_snapshot`、`client.tenant_access_token`、`conn.execute`、`conn.execute(…).fetchone`、`connection`、`datetime.now`、`state.get`、`timedelta` |
| `manually_confirm_contact_organization` | [60](../../backend/app/organization_sync.py#L60) | `<Constant>.join`、`OrganizationInputError`、`_link_tenant_identities`、`_upsert_contact_link`、`bool`、`canonical_name.split`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`datetime.now`、`identity.get`、`len` |
| `_fetch_all` | [178](../../backend/app/organization_sync.py#L178) | `LarkApiError`、`_organization_rows`、`_organization_tenant_keys`、`_text`、`client.list_collaboration_tenants`、`payload.get`、`result.extend`、`set`、`visible_tenant_keys.update` |
| `_organization_rows` | [193](../../backend/app/organization_sync.py#L193) | `_organization_name`、`_text`、`isinstance`、`item.get`、`payload.get`、`rows.append` |
| `_organization_tenant_keys` | [208](../../backend/app/organization_sync.py#L208) | `_text`、`isinstance`、`item.get`、`payload.get`、`set` |
| `_organization_name` | [221](../../backend/app/organization_sync.py#L221) | `_text`、`isinstance`、`item.get`、`value.get` |
| `_store_snapshot` | [234](../../backend/app/organization_sync.py#L234) | `_link_known_identities`、`_reconcile_snapshot`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`datetime.now`、`len` |
| `_refresh_known_links` | [274](../../backend/app/organization_sync.py#L274) | `_link_known_identities`、`conn.transaction`、`connection`、`datetime.now` |
| `_reconcile_snapshot` | [281](../../backend/app/organization_sync.py#L281) | `conn.execute`、`list` |
| `_link_known_identities` | [313](../../backend/app/organization_sync.py#L313) | `conn.execute` |
| `_link_tenant_identities` | [343](../../backend/app/organization_sync.py#L343) | `conn.execute` |
| `_upsert_contact_link` | [376](../../backend/app/organization_sync.py#L376) | `conn.execute` |
| `_record_state` | [403](../../backend/app/organization_sync.py#L403) | `conn.execute`、`conn.transaction`、`connection` |
| `_text` | [415](../../backend/app/organization_sync.py#L415) | `isinstance`、`value.strip` |

## backend/app/summary_dialogues.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `encode` | [23](../../backend/app/summary_dialogues.py#L23) | `core._cipher`、`core._cipher(…).encrypt`、`json.dumps` |
| `decode` | [27](../../backend/app/summary_dialogues.py#L27) | `core._cipher`、`core._cipher(…).decrypt`、`json.loads` |
| `parts` | [31](../../backend/app/summary_dialogues.py#L31) | `len`、`max`、`messages.append`、`range` |
| `segments` | [45](../../backend/app/summary_dialogues.py#L45) | `batch.append`、`batches.append`、`by_id.get`、`by_id.setdefault`、`by_id.setdefault(…).append`、`chosen.add`、`chosen.update`、`core._hash`、`enumerate`、`json.dumps`、`len`、`m.get`、`max`、`missing.add`、`parts`、`pending.append`、`pending.pop`、`result.append`、`set` |
| `checked_chat` | [101](../../backend/app/summary_dialogues.py#L101) | `core.SummaryInputError`、`core._current`、`core._load_chat` |
| `schedule_merges` | [109](../../backend/app/summary_dialogues.py#L109) | `conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`core._enabled`、`core._hash`、`decode`、`max`、`people.items`、`people.setdefault`、`people.setdefault(…).append`、`result[…].values`、`selected.append`、`str`、`uuid4` |
| `merge_input` | [167](../../backend/app/summary_dialogues.py#L167) | `UUID`、`any`、`conn.execute`、`conn.execute(…).fetchall`、`core.SummaryInputError`、`core._authorize`、`core._hash`、`core._load_chat`、`core._related_chats`、`core._roster`、`decode`、`events.append`、`iter`、`next`、`saved[…].items`、`source_hashes.append`、`str`、`users.values` |
| `merged_evidence` | [236](../../backend/app/summary_dialogues.py#L236) | `core._hash`、`e.get`、`enumerate`、`sorted`、`unique.values` |
| `merge_format` | [249](../../backend/app/summary_dialogues.py#L249) | `core.handling_fields`、`list` |
| `combine` | [274](../../backend/app/summary_dialogues.py#L274) | `<Constant>.join`、`a.casefold`、`b.casefold`、`core.InvalidModelOutput`、`core._hash`、`core.validate_handling`、`evidence.values`、`isinstance`、`json.loads`、`len`、`merge_format`、`merged_evidence`、`messages.get`、`min`、`set`、`sorted`、`type`、`value[…].strip`、`value[…].strip(…).casefold` |
| `step` | [318](../../backend/app/summary_dialogues.py#L318) | `<BoolOp>.get`、`all`、`checked_chat`、`checkpoint.get`、`checkpoint.pop`、`checkpoint.setdefault`、`checkpoint.update`、`combine`、`completed.get`、`conn.execute`、`conn.transaction`、`core.InvalidModelOutput`、`core._config`、`core._current`、`core._format`、`core._validate`、`core.connection`、`decode`、`encode`、`job.get`、`json.dumps`、`json.loads`、`ledger.append`、`ledger.pop`、`len`、`merge_format`、`merge_input`、`merged_evidence`、`next`、`payload.pop`、`request`、`schema[…][…].append`、`segments`、`sorted`、`sum` |
| `request` | [438](../../backend/app/summary_dialogues.py#L438) | `checked_chat`、`conn.transaction`、`contact_assessments.api_key_cipher`、`contact_assessments.api_key_cipher(…).decrypt`、`contact_assessments.call_openai`、`core._current`、`core.connection`、`merge_input` |
| `run` | [457](../../backend/app/summary_dialogues.py#L457) | `contact_assessments._retry_delay`、`core.LOGGER.info`、`core.LOGGER.warning`、`core._finish`、`core.ai_dispatch.pause_for_quota`、`core.ai_dispatch.throttle`、`isinstance`、`step`、`str`、`type` |
| `view_events` | [492](../../backend/app/summary_dialogues.py#L492) | `<Constant>.join`、`decode`、`dict.fromkeys`、`enumerate`、`merge_input` |

## backend/app/summary_periods.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `window` | [9](../../backend/app/summary_periods.py#L9) | `date.fromisoformat`、`datetime.combine`、`end.astimezone`、`start.astimezone`、`str`、`summaries.SummaryInputError`、`time`、`timedelta` |
| `selection` | [22](../../backend/app/summary_periods.py#L22) | `<BinOp>.isoformat`、`<BoolOp>.astimezone`、`UUID`、`bool`、`conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`datetime.now`、`now.date`、`range`、`str`、`summaries.SummaryInputError`、`summaries._authorize`、`summaries._roster`、`timedelta`、`window` |
| `seed_due` | [48](../../backend/app/summary_periods.py#L48) | `conn.execute`、`conn.execute(…).fetchall`、`conn.execute(…).fetchone`、`max`、`now.astimezone`、`now.astimezone(…).date`、`range`、`summaries._enabled`、`summaries._roster`、`timedelta`、`uuid4`、`window` |

## backend/app/summary_replies.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `schema` | [58](../../backend/app/summary_replies.py#L58) | `list` |
| `ref_list` | [67](../../backend/app/summary_replies.py#L67) | `list` |
| `draft_schema` | [71](../../backend/app/summary_replies.py#L71) | `ref_list`、`schema` |
| `source` | [84](../../backend/app/summary_replies.py#L84) | `UUID`、`conn.execute`、`conn.execute(…).fetchone`、`core.SummaryInputError`、`core._authorize`、`core._enabled`、`core._related_chats`、`core._roster`、`dialogues.decode`、`dialogues.merge_input`、`event_id.split`、`events[…].get`、`int`、`len` |
| `collect` | [108](../../backend/app/summary_replies.py#L108) | `all`、`any`、`chat.get`、`conn.execute`、`conn.execute(…).fetchall`、`core._body`、`core._hash`、`core.mention_targets`、`datetime.now`、`enumerate`、`gaps.append`、`len`、`list`、`max`、`mention_names`、`message_body_cipher`、`messages.append`、`messages.sort`、`min`、`now.isoformat`、`range`、`reversed`、`row.get`、`row[…].isoformat`、`scopes.append`、`set`、`sorted`、`source`、`t.get`、`timedelta`、`watermark.isoformat`、`watermarks.append` |
| `chunks` | [183](../../backend/app/summary_replies.py#L183) | `batch.append`、`batches.append`、`json.dumps`、`len` |
| `enqueue` | [197](../../backend/app/summary_replies.py#L197) | `UUID`、`conn.execute`、`conn.execute(…).fetchone`、`core.SummaryInputError`、`core._hash`、`dialogues.decode`、`dialogues.decode(…).get`、`dialogues.encode`、`event_id.split`、`int`、`isinstance`、`len`、`source`、`str`、`text.encode`、`text.strip`、`uuid4` |
| `claim` | [242](../../backend/app/summary_replies.py#L242) | `conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`core._enabled`、`core.ai_dispatch.can_claim` |
| `fence` | [263](../../backend/app/summary_replies.py#L263) | `conn.execute`、`conn.execute(…).fetchone`、`core.SummaryInputError` |
| `event_id` | [271](../../backend/app/summary_replies.py#L271) | — |
| `step` | [275](../../backend/app/summary_replies.py#L275) | `any`、`api.api_key_cipher`、`api.api_key_cipher(…).decrypt`、`api.call_openai`、`by_id.get`、`by_id[…].append`、`checkpoint.get`、`checkpoint.setdefault`、`chosen.values`、`chunks`、`collect`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`core.InvalidModelOutput`、`core.SummaryInputError`、`core._config`、`current[…].index`、`dialogues.decode`、`dialogues.encode`、`dict.fromkeys`、`draft_schema`、`enumerate`、`event_id`、`fence`、`isinstance`、`json.loads`、`len`、`list`、`m.get`、`max`、`min`、`pack[…].append`、`pending.append`、`pending.pop`、`ref_list`、`result.pop`、`result.update`、`result[…].append`、`result[…].strip`、`schema`、`set`、`sorted` |
| `run` | [414](../../backend/app/summary_replies.py#L414) | `api._retry_delay`、`conn.execute`、`conn.transaction`、`connection`、`core.LOGGER.info`、`core.ai_dispatch.pause_for_quota`、`core.ai_dispatch.throttle`、`isinstance`、`step`、`str`、`type` |
| `read` | [439](../../backend/app/summary_replies.py#L439) | `PermissionError`、`UUID`、`collect`、`conn.execute`、`conn.execute(…).fetchone`、`dialogues.decode`、`event_id`、`source`、`str` |

## backend/app/worker.py

| 方法 | 源码 | 直接调用表达式 |
| --- | --- | --- |
| `run_forever` | [101](../../backend/app/worker.py#L101) | `LOGGER.exception`、`LOGGER.warning`、`LarkClient`、`LarkSyncAdmission`、`ThreadPoolExecutor`、`_fill_ai_slots`、`_fill_lark_slots`、`_finish_completed_jobs`、`_reap_ai_futures`、`_reap_member_futures`、`_reap_page_futures`、`_recover_expired_leases`、`_run_organization_sync`、`_schedule_creator_preview`、`_schedule_member_snapshot`、`_start_pending_jobs`、`ai_executor.shutdown`、`avatar_executor.shutdown`、`get_settings`、`getattr`、`http_client.close`、`httpx.Client`、`httpx.Limits`、`httpx.Timeout`、`identity_profiles.fill_slots`、`lark_executor.shutdown`、`logging.basicConfig`、`logging.getLogger`、`logging.getLogger(…).setLevel`、`min`、`preview_executor.shutdown`、`set`、`time.monotonic`、`time.sleep` |
| `_run_organization_sync` | [187](../../backend/app/worker.py#L187) | `LOGGER.exception`、`organization_sync.sync_if_due` |
| `_dispatch_ai_turn` | [196](../../backend/app/worker.py#L196) | `LOGGER.exception`、`claim`、`executor.submit`、`futures.add`、`len`、`range` |
| `_fill_ai_slots` | [241](../../backend/app/worker.py#L241) | `_dispatch_ai_turn`、`len` |
| `_prepare_creator_preview` | [256](../../backend/app/worker.py#L256) | `LOGGER.exception`、`len`、`queues[…]`、`range` |
| `_schedule_creator_preview` | [271](../../backend/app/worker.py#L271) | `LOGGER.exception`、`executor.submit`、`future.done`、`future.result`、`max`、`time.monotonic` |
| `_reap_ai_futures` | [296](../../backend/app/worker.py#L296) | `LOGGER.exception`、`future.done`、`future.result`、`futures.remove`、`tuple` |
| `_recover_expired_leases` | [309](../../backend/app/worker.py#L309) | `conn.execute`、`conn.execute(…).fetchall`、`conn.transaction`、`connection` |
| `_claim_job` | [373](../../backend/app/worker.py#L373) | `SyncJob`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`int` |
| `_running_jobs` | [406](../../backend/app/worker.py#L406) | `SyncJob`、`conn.execute`、`conn.execute(…).fetchall`、`connection`、`int` |
| `_start_pending_jobs` | [429](../../backend/app/worker.py#L429) | `_claim_job`、`_prepare_job`、`_running_jobs`、`len` |
| `_prepare_job` | [443](../../backend/app/worker.py#L443) | `_authorization`、`_begin_discovery`、`_complete_discovery`、`_discover_chats`、`_discover_p2p`、`_finish_job`、`_handle_job_api_error`、`_mark_attention`、`_refresh_profile_name`、`_requeue`、`_usable_access_token`、`authorization.get`、`min` |
| `_begin_discovery` | [476](../../backend/app/worker.py#L476) | `SyncJob`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`int` |
| `_complete_discovery` | [501](../../backend/app/worker.py#L501) | `conn.execute`、`conn.transaction`、`connection` |
| `_handle_job_api_error` | [516](../../backend/app/worker.py#L516) | `_finish_job`、`_mark_attention`、`_requeue`、`min` |
| `_fill_lark_slots` | [534](../../backend/app/worker.py#L534) | `_dispatch_lark_turn`、`_running_jobs`、`len` |
| `_active_page_claim_counts` | [549](../../backend/app/worker.py#L549) | `Counter`、`conn.execute`、`conn.execute(…).fetchall`、`connection`、`int`、`list` |
| `_dispatch_lark_turn` | [568](../../backend/app/worker.py#L568) | `_active_page_claim_counts`、`dispatch`、`len`、`min` |
| `_dispatch_lark_turn.dispatch` | [584](../../backend/app/worker.py#L584) | `_authorization`、`_claim_next_page`、`_finish_job`、`_handle_job_api_error`、`_mark_attention`、`_requeue`、`_usable_access_token`、`authorization_cache.get`、`executor.submit`、`min`、`sorted` |
| `_fetch_claim` | [643](../../backend/app/worker.py#L643) | `client.list_chat_messages` |
| `_reap_page_futures` | [654](../../backend/app/worker.py#L654) | `LOGGER.exception`、`_add_job_progress`、`_continuation_token`、`_handle_claim_api_error`、`_items`、`_record_claimed_page`、`_release_retryable_page_claim`、`_requeue`、`future.done`、`future.result`、`futures.items`、`message_body_cipher`、`min`、`tuple` |
| `_schedule_member_snapshot` | [693](../../backend/app/worker.py#L693) | `_authorization`、`_claim_member_snapshot`、`_mark_one_member_snapshot_unavailable`、`_release_member_snapshot_claim`、`_usable_access_token`、`executor.submit` |
| `_claim_member_snapshot` | [725](../../backend/app/worker.py#L725) | `MemberSnapshotClaim`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`int`、`uuid.uuid4` |
| `_reap_member_futures` | [778](../../backend/app/worker.py#L778) | `LOGGER.exception`、`_release_member_snapshot_claim`、`_store_claimed_member_snapshot`、`future.done`、`future.result`、`futures.items`、`tuple` |
| `_store_claimed_member_snapshot` | [794](../../backend/app/worker.py#L794) | `_internal_tenant_keys`、`_store_member_snapshot`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`datetime.now` |
| `_release_member_snapshot_claim` | [861](../../backend/app/worker.py#L861) | `conn.execute`、`conn.transaction`、`connection` |
| `_mark_one_member_snapshot_unavailable` | [881](../../backend/app/worker.py#L881) | `conn.execute`、`conn.transaction`、`connection` |
| `_handle_claim_api_error` | [901](../../backend/app/worker.py#L901) | `_mark_attention`、`_mark_chat_not_readable`、`_release_retryable_page_claim`、`min` |
| `_finish_completed_jobs` | [918](../../backend/app/worker.py#L918) | `_finish_and_schedule_incremental`、`_running_jobs`、`futures.values` |
| `_authorization` | [926](../../backend/app/worker.py#L926) | `conn.execute`、`conn.execute(…).fetchone`、`connection` |
| `_usable_access_token` | [939](../../backend/app/worker.py#L939) | `TokenCipher`、`_store_refreshed_tokens`、`authorization.get`、`cipher.decrypt`、`client.refresh_token`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`datetime.now`、`timedelta` |
| `_store_refreshed_tokens` | [972](../../backend/app/worker.py#L972) | `_expiry`、`cipher.encrypt`、`conn.execute` |
| `_refresh_profile_name` | [993](../../backend/app/worker.py#L993) | `LOGGER.info`、`TokenCipher`、`TokenCipher(…).decrypt`、`_assert_discovery_current`、`_authorization`、`_upsert_lark_identity`、`client.user_info`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`identity_profiles.store_profile_avatar` |
| `_discover_chats` | [1042](../../backend/app/worker.py#L1042) | `LarkApiError`、`_assert_discovery_current`、`_bool`、`_ensure_contact_for_identity`、`_ensure_internal_contact_roles`、`_has_more`、`_internal_tenant_keys`、`_items`、`_p2p_counterpart_open_id`、`_renew_lease`、`_store_member_snapshot`、`_text`、`_upsert_lark_identity`、`chat.get`、`client.list_chats`、`conn.execute`、`conn.transaction`、`connection`、`data.get`、`datetime.now`、`rows.append` |
| `_assert_discovery_current` | [1246](../../backend/app/worker.py#L1246) | `conn.execute`、`conn.execute(…).fetchone` |
| `_discover_p2p` | [1261](../../backend/app/worker.py#L1261) | `_discover_chats`、`_set_p2p_discovery` |
| `_set_p2p_discovery` | [1287](../../backend/app/worker.py#L1287) | `conn.execute`、`conn.transaction`、`connection` |
| `_p2p_counterpart_open_id` | [1305](../../backend/app/worker.py#L1305) | — |
| `_read_member_snapshot` | [1311](../../backend/app/worker.py#L1311) | `LOGGER.info`、`MemberSnapshotMeasurement`、`_continuation_token`、`_has_more`、`_member_records`、`_nonnegative_int`、`bool`、`client.list_chat_members`、`data.get`、`datetime.now`、`len`、`members.extend`、`tuple`、`unique_members.values` |
| `_member_records` | [1355](../../backend/app/worker.py#L1355) | `_text`、`data.get`、`format_display_name`、`isinstance`、`member.get`、`records.append` |
| `_store_member_snapshot` | [1380](../../backend/app/worker.py#L1380) | `_ensure_contact_for_identity`、`_tenant_classification`、`_upsert_lark_identity`、`bool`、`conn.execute`、`conn.execute(…).fetchall`、`member.get` |
| `_upsert_lark_identity` | [1446](../../backend/app/worker.py#L1446) | `conn.execute`、`datetime.now`、`update_identity_names` |
| `_internal_tenant_keys` | [1476](../../backend/app/worker.py#L1476) | `conn.execute`、`conn.execute(…).fetchall` |
| `_tenant_classification` | [1498](../../backend/app/worker.py#L1498) | — |
| `_refresh_tenant_classifications` | [1508](../../backend/app/worker.py#L1508) | `_ensure_internal_contact_roles`、`conn.execute`、`list` |
| `_ensure_internal_contact_roles` | [1534](../../backend/app/worker.py#L1534) | `conn.execute`、`conn.execute(…).fetchall` |
| `_ensure_contact_for_identity` | [1590](../../backend/app/worker.py#L1590) | `conn.execute`、`conn.execute(…).fetchone`、`datetime.now`、`uuid.uuid4` |
| `_claim_next_page` | [1650](../../backend/app/worker.py#L1650) | `PageClaim`、`_has_pending_backfill_in_connection`、`_reset_one_requested_backfill`、`_select_backfill_chat_for_claim`、`_select_incremental_chat_for_claim`、`authorization.get`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`datetime.now`、`int`、`timedelta`、`uuid.uuid4` |
| `_reset_one_requested_backfill` | [1777](../../backend/app/worker.py#L1777) | `conn.execute`、`conn.execute(…).fetchone` |
| `_select_backfill_chat_for_claim` | [1813](../../backend/app/worker.py#L1813) | `conn.execute`、`conn.execute(…).fetchone` |
| `_select_incremental_chat_for_claim` | [1835](../../backend/app/worker.py#L1835) | `conn.execute`、`conn.execute(…).fetchone` |
| `_has_pending_backfill_in_connection` | [1862](../../backend/app/worker.py#L1862) | `bool`、`conn.execute`、`conn.execute(…).fetchone` |
| `_record_message_sender_identity` | [1882](../../backend/app/worker.py#L1882) | `_ensure_contact_for_identity`、`_sender_metadata`、`_tenant_classification`、`_upsert_lark_identity`、`internal_tenant_keys.add` |
| `_record_claimed_page` | [1918](../../backend/app/worker.py#L1918) | `PageCommit`、`_body_content`、`_bool`、`_ensure_contact_for_identity`、`_internal_tenant_keys`、`_is_author_message`、`_lark_time`、`_message_relation_metadata`、`_record_message_sender_identity`、`_refresh_tenant_classifications`、`_tenant_classification`、`_text`、`_upsert_lark_identity`、`bool`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`content_cipher.encrypt`、`int`、`json.dumps`、`mention.get`、`message.get` |
| `_mark_chat_not_readable` | [2173](../../backend/app/worker.py#L2173) | `conn.execute`、`conn.transaction`、`connection` |
| `_release_retryable_page_claim` | [2200](../../backend/app/worker.py#L2200) | `_yield_job_to_retry_if_idle`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection` |
| `_yield_job_to_retry_if_idle` | [2260](../../backend/app/worker.py#L2260) | `_has_claimable_page_in_connection`、`conn.execute`、`conn.execute(…).fetchone`、`int` |
| `_has_claimable_page_in_connection` | [2297](../../backend/app/worker.py#L2297) | `_has_pending_backfill_in_connection`、`bool`、`conn.execute`、`conn.execute(…).fetchone` |
| `_locked_current_page_claim` | [2333](../../backend/app/worker.py#L2333) | `bool`、`conn.execute`、`conn.execute(…).fetchone` |
| `_renew_lease` | [2357](../../backend/app/worker.py#L2357) | `conn.execute`、`conn.transaction`、`connection` |
| `_add_job_progress` | [2369](../../backend/app/worker.py#L2369) | `conn.execute`、`conn.transaction`、`connection` |
| `_finish_and_schedule_incremental` | [2386](../../backend/app/worker.py#L2386) | `_has_pending_backfill_in_connection`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection`、`get_settings`、`uuid.uuid4` |
| `_requeue` | [2478](../../backend/app/worker.py#L2478) | `_locked_current_page_claim`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection` |
| `_finish_job` | [2551](../../backend/app/worker.py#L2551) | `conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection` |
| `_mark_attention` | [2580](../../backend/app/worker.py#L2580) | `_locked_current_page_claim`、`conn.execute`、`conn.execute(…).fetchone`、`conn.transaction`、`connection` |
| `_items` | [2631](../../backend/app/worker.py#L2631) | `data.get`、`isinstance` |
| `_has_more` | [2636](../../backend/app/worker.py#L2636) | `data.get` |
| `_continuation_token` | [2641](../../backend/app/worker.py#L2641) | `LarkApiError`、`_has_more`、`_text`、`data.get` |
| `_text` | [2650](../../backend/app/worker.py#L2650) | `isinstance`、`value.strip` |
| `_bool` | [2654](../../backend/app/worker.py#L2654) | — |
| `_body_content` | [2658](../../backend/app/worker.py#L2658) | `_text`、`body.get`、`isinstance`、`message.get` |
| `_message_relation_metadata` | [2663](../../backend/app/worker.py#L2663) | `_nonnegative_int`、`_text`、`isinstance`、`mention.get`、`mentions.append`、`message.get` |
| `_sender_metadata` | [2692](../../backend/app/worker.py#L2692) | `_text`、`isinstance`、`message.get`、`sender.get` |
| `_is_author_message` | [2715](../../backend/app/worker.py#L2715) | `isinstance`、`message.get`、`sender.get` |
| `_lark_time` | [2726](../../backend/app/worker.py#L2726) | `datetime.fromtimestamp`、`int` |
| `_nonnegative_int` | [2734](../../backend/app/worker.py#L2734) | `int` |
| `_expiry` | [2742](../../backend/app/worker.py#L2742) | `datetime.now`、`timedelta` |
