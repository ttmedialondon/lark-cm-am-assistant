-- Same eligibility boundary; start from this group's people instead of scanning all creators for every group.
CREATE OR REPLACE FUNCTION creator_group_is_eligible(target TEXT) RETURNS BOOLEAN LANGUAGE SQL STABLE AS $$
  SELECT EXISTS(SELECT 1 FROM chats c WHERE c.chat_id=target
    AND COALESCE(c.chat_status,'normal')='normal' AND COALESCE(c.chat_mode,'group')<>'p2p'
    AND EXISTS(SELECT 1 FROM authorization_chats ac JOIN authorizations a ON a.id=ac.authorization_id
      WHERE ac.chat_id=c.chat_id AND ac.is_visible AND ac.sync_enabled
        AND a.status='active' AND a.body_consent_at IS NOT NULL)
    AND (EXISTS(SELECT 1 FROM chat_members m
      JOIN lark_identities i ON i.open_id=m.open_id JOIN contact_roles r ON r.contact_id=i.contact_id
      WHERE m.chat_id=c.chat_id AND i.identity_type='user'
        AND i.contact_id<>'6abf6bf9-12dc-4a22-9654-2010aa4c7785'::uuid
        AND r.role='creator' AND r.status='confirmed')
      OR EXISTS(SELECT 1 FROM chat_messages m
      JOIN lark_identities i ON i.open_id=m.sender_open_id JOIN contact_roles r ON r.contact_id=i.contact_id
      WHERE m.chat_id=c.chat_id AND NOT m.is_deleted AND i.identity_type='user'
        AND i.contact_id<>'6abf6bf9-12dc-4a22-9654-2010aa4c7785'::uuid
        AND r.role='creator' AND r.status='confirmed')))
$$;
