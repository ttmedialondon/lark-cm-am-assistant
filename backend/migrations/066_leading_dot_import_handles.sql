-- Restore only handles rejected by the old first-character validation.
-- Preview batches remain unstarted; existing rows and decisions are untouched.
DO $$
DECLARE b RECORD; e JSONB; entries JSONB; seen TEXT[]; h TEXT; restored INTEGER;
BEGIN
  FOR b IN SELECT * FROM creator_import_batches
    WHERE policy_version='creator-import-v2' AND creator_type='cap'
      AND EXISTS (SELECT 1 FROM jsonb_array_elements(preview_rows) x
                  WHERE x->>'error'='handle 格式不正确' AND x->>'handle' LIKE '.%')
    FOR UPDATE
  LOOP
    entries := '[]'::jsonb;
    restored := 0;
    SELECT COALESCE(array_agg(x->>'handle'), ARRAY[]::text[]) INTO seen
      FROM jsonb_array_elements(b.preview_rows) x WHERE x->>'error'='';
    FOR e IN SELECT value FROM jsonb_array_elements(b.preview_rows)
    LOOP
      h := lower(e->>'handle');
      IF e->>'error'='handle 格式不正确' AND h ~ '^\.[a-z0-9._]{0,63}$'
         AND h ~ '[a-z0-9]' AND right(h,1)<>'.' THEN
        e := e || jsonb_build_object('handle',h,'error',CASE WHEN h=ANY(seen) THEN '重复 handle' ELSE '' END);
        IF NOT h=ANY(seen) THEN
          seen := array_append(seen,h);
          IF b.status<>'preview' THEN
            INSERT INTO creator_import_rows(id,batch_id,cm_authorization_id,row_number,
              original_handle,handle,status,outcome,stats)
            VALUES(gen_random_uuid(),b.id,b.cm_authorization_id,(e->>'row_number')::int,
              COALESCE(e->>'original_handle',e->>'handle'),h,'pending','review','{"analysis_stage":"group"}')
            ON CONFLICT DO NOTHING;
          END IF;
          restored := restored+1;
        END IF;
      END IF;
      entries := entries || jsonb_build_array(e);
    END LOOP;
    UPDATE creator_import_batches SET preview_rows=entries,
      counts=(SELECT jsonb_build_object(
        'valid',count(*) FILTER (WHERE x->>'error'=''),
        'duplicates',count(*) FILTER (WHERE x->>'error'='重复 handle'),
        'invalid',count(*) FILTER (WHERE x->>'error'='handle 格式不正确'))
        FROM jsonb_array_elements(entries) x),
      status=CASE WHEN status='completed' AND restored>0 THEN 'running' ELSE status END,
      updated_at=NOW() WHERE id=b.id;
    INSERT INTO audit_events(id,authorization_id,action,metadata)
      VALUES(gen_random_uuid(),b.uploaded_by,'creator_import_leading_dot_repaired',
        jsonb_build_object('batch_id',b.id,'restored_handles',restored));
  END LOOP;
END $$;
