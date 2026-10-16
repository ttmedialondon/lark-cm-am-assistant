-- Message changes are local evidence changes, not whole-community topology changes.
CREATE OR REPLACE FUNCTION creator_importance_chat_signal(chat TEXT, cm UUID DEFAULT NULL,
  reset BOOLEAN DEFAULT FALSE, message_id TEXT DEFAULT NULL)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE pair RECORD; source_match JSONB; parent_match JSONB; configured BOOLEAN; keep_profile BOOLEAN;
BEGIN
  configured := EXISTS(SELECT 1 FROM ai_feature_configs WHERE feature='creator_importance'
    AND enabled AND api_key_ciphertext IS NOT NULL);
  IF NOT configured AND NOT reset THEN RETURN; END IF;
  source_match := jsonb_build_array(CASE WHEN message_id IS NULL THEN jsonb_build_object('chat_id',chat)
    ELSE jsonb_build_object('chat_id',chat,'message_id',message_id) END);
  parent_match := jsonb_build_array(jsonb_build_object('chat_id',chat,'parent_id',message_id));
  IF message_id IS NOT NULL THEN
    FOR pair IN
      WITH affected AS (
        -- Existing evidence must be revoked even after deletion, reassignment or lost access.
        SELECT p.contact_id,p.responsible_authorization_id,TRUE AS invalidate,TRUE AS profile_invalidate
          FROM creator_importance_profiles p WHERE reset
            AND (p.sources @> source_match OR p.sources @> parent_match)
            AND (cm IS NULL OR p.responsible_authorization_id=cm)
        UNION ALL
        SELECT j.contact_id,j.responsible_authorization_id,TRUE,FALSE
          FROM creator_importance_jobs j WHERE reset
            AND (j.refs @> source_match OR j.refs @> parent_match)
            AND (cm IS NULL OR j.responsible_authorization_id=cm)
        UNION ALL
        SELECT x.contact_id,x.responsible_authorization_id,FALSE,FALSE
          FROM chat_messages m
          JOIN authorization_message_observations o USING(chat_id,message_id)
          JOIN authorizations a ON a.id=o.authorization_id
          JOIN authorization_chats ac ON ac.authorization_id=a.id AND ac.chat_id=m.chat_id
          JOIN chats ch ON ch.chat_id=m.chat_id
          JOIN contact_assignments x ON x.responsible_authorization_id=a.id AND x.ended_at IS NULL
          JOIN contact_roles role ON role.contact_id=x.contact_id AND role.role='creator' AND role.status='confirmed'
          JOIN lark_identities target ON target.contact_id=x.contact_id AND target.identity_type='user'
          WHERE configured AND m.chat_id=chat AND m.message_id=creator_importance_chat_signal.message_id
            AND (cm IS NULL OR a.id=cm) AND a.status='active' AND a.body_consent_at IS NOT NULL
            AND a.scopes @> ARRAY['im:chat:read','im:message:readonly']::text[]
            AND o.observed_at>=a.body_consent_at AND ac.is_visible AND ac.sync_enabled
            AND ac.sync_status IS DISTINCT FROM 'not_readable' AND COALESCE(ch.chat_status,'normal')='normal'
            AND NOT m.is_deleted AND m.body_ciphertext IS NOT NULL AND m.sender_type='user'
            AND m.message_type IN ('text','post','share_chat','share_user')
            AND m.message_create_time BETWEEN NOW()-INTERVAL '90 days' AND NOW()
            AND (m.sender_open_id=target.open_id OR (m.sender_open_id=a.open_id AND (
              ac.p2p_counterpart_open_id=target.open_id
              OR m.mentions_json @> jsonb_build_array(jsonb_build_object('id',target.open_id))
              OR EXISTS(SELECT 1 FROM chat_messages parent WHERE parent.chat_id=m.chat_id
                AND parent.message_id=m.parent_id AND parent.sender_open_id=target.open_id AND NOT parent.is_deleted)
              OR EXISTS(SELECT 1 FROM chat_messages child
                JOIN authorization_message_observations seen USING(chat_id,message_id)
                WHERE child.chat_id=m.chat_id AND child.parent_id=m.message_id
                  AND child.sender_open_id=target.open_id AND NOT child.is_deleted
                  AND seen.authorization_id=a.id AND seen.observed_at>=a.body_consent_at)
              OR (ac.member_snapshot_status='succeeded' AND ac.member_count IS NOT NULL
                AND EXISTS(SELECT 1 FROM chat_members member
                  WHERE member.chat_id=m.chat_id AND member.open_id=target.open_id AND member.member_type='user')
                AND (SELECT count(*) FROM chat_members member WHERE member.chat_id=m.chat_id)>=ac.member_count
                AND NOT EXISTS(SELECT 1 FROM chat_members member
                  LEFT JOIN lark_identities identity ON identity.open_id=member.open_id
                  WHERE member.chat_id=m.chat_id AND member.open_id<>a.open_id
                    AND identity.contact_id IS DISTINCT FROM target.contact_id
                    AND identity.tenant_classification IS DISTINCT FROM 'internal')))))
      ) SELECT contact_id,responsible_authorization_id,bool_or(invalidate) AS invalidate,
          bool_or(profile_invalidate) AS profile_invalidate
        FROM affected GROUP BY contact_id,responsible_authorization_id
        ORDER BY contact_id,responsible_authorization_id
    LOOP
      keep_profile := FALSE;
      IF pair.invalidate AND NOT pair.profile_invalidate THEN
        -- Match enqueue's job-before-profile lock order; a newer batch is not the published profile.
        PERFORM 1 FROM creator_importance_jobs WHERE contact_id=pair.contact_id
          AND responsible_authorization_id=pair.responsible_authorization_id FOR UPDATE;
        SELECT auto_valid INTO keep_profile FROM creator_importance_profiles WHERE contact_id=pair.contact_id
          AND responsible_authorization_id=pair.responsible_authorization_id FOR UPDATE;
      END IF;
      PERFORM enqueue_creator_importance(pair.contact_id,pair.responsible_authorization_id,
        pair.invalidate,chat,message_id);
      IF keep_profile THEN
        UPDATE creator_importance_profiles SET auto_valid=TRUE WHERE contact_id=pair.contact_id
          AND responsible_authorization_id=pair.responsible_authorization_id;
      END IF;
    END LOOP;
    RETURN;
  END IF;

  -- Permission, membership and relationship changes retain their existing topology fence.
  FOR pair IN
    WITH related AS (
      SELECT i.contact_id FROM lark_identities i JOIN chat_members m ON m.open_id=i.open_id
        WHERE m.chat_id=chat AND i.identity_type='user' AND m.member_type='user'
      UNION SELECT i.contact_id FROM lark_identities i JOIN chat_messages m ON m.sender_open_id=i.open_id
        WHERE m.chat_id=chat AND i.identity_type='user'
      UNION SELECT i.contact_id FROM lark_identities i JOIN authorization_chats ac
        ON ac.p2p_counterpart_open_id=i.open_id WHERE ac.chat_id=chat AND i.identity_type='user'
    ), active_pairs AS (
      SELECT x.contact_id,x.responsible_authorization_id FROM contact_assignments x
        JOIN related r ON r.contact_id=x.contact_id WHERE x.ended_at IS NULL
    )
    SELECT p.contact_id,p.responsible_authorization_id FROM creator_importance_profiles p
      WHERE reset AND p.sources @> source_match AND (cm IS NULL OR p.responsible_authorization_id=cm)
    UNION SELECT j.contact_id,j.responsible_authorization_id FROM creator_importance_jobs j
      WHERE reset AND j.refs @> source_match AND (cm IS NULL OR j.responsible_authorization_id=cm)
    UNION SELECT p.contact_id,p.responsible_authorization_id FROM active_pairs p
      JOIN authorization_chats ac ON ac.authorization_id=p.responsible_authorization_id AND ac.chat_id=chat
      WHERE configured AND (cm IS NULL OR p.responsible_authorization_id=cm)
    ORDER BY contact_id,responsible_authorization_id
  LOOP
    PERFORM enqueue_creator_importance(pair.contact_id,pair.responsible_authorization_id,reset);
  END LOOP;
END $$;
