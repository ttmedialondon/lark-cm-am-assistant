-- Preserve existing source limits while permitting several explicitly named CMs.
ALTER TABLE admin_message_source_limits DROP CONSTRAINT admin_message_source_limits_pkey;
ALTER TABLE admin_message_source_limits ADD PRIMARY KEY (authorization_id,source_authorization_id);

WITH added AS (
  INSERT INTO admin_message_source_limits(authorization_id,source_authorization_id)
  SELECT viewer.id, source.id FROM authorizations viewer CROSS JOIN authorizations source
  WHERE viewer.id='1225b4a0-fbd8-431e-962e-25e66943a8a0'
    AND source.id IN ('9f9c4b2f-1a4f-4e4b-b365-9b01cd908983',
                      'b9d08608-b892-4cd6-965e-af0bbb3321f4',
                      '7d83d4ce-615c-44fb-aa8c-136dd14da83c',
                      'b567927b-1f5b-4fa2-bbca-86ba1e147c46')
  ON CONFLICT DO NOTHING
  RETURNING authorization_id,source_authorization_id
)
INSERT INTO audit_events(id,authorization_id,action,metadata)
SELECT gen_random_uuid(),authorization_id,'admin_message_sources_expanded',
       jsonb_build_object('added_source_ids',jsonb_agg(source_authorization_id))
FROM added GROUP BY authorization_id;
