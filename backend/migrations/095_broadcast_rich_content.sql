-- Personal rich drafts and encrypted, privately owned original image files.
ALTER TABLE creator_broadcast_templates ADD COLUMN body_document JSONB;
ALTER TABLE creator_broadcast_templates DROP CONSTRAINT creator_broadcast_templates_body_check;
ALTER TABLE creator_broadcast_templates ADD CONSTRAINT creator_broadcast_templates_body_check
    CHECK (length(body)<=4000 AND (length(btrim(body))>0 OR body_document IS NOT NULL));
ALTER TABLE creator_broadcast_templates ADD CONSTRAINT creator_broadcast_templates_document_check
    CHECK (body_document IS NULL OR COALESCE(
        jsonb_typeof(body_document)='object' AND body_document->'version'='2'::jsonb
        AND jsonb_typeof(body_document->'ops')='array' AND jsonb_array_length(body_document->'ops')>0, FALSE));

CREATE TABLE creator_broadcast_images (
    id UUID PRIMARY KEY,
    owner_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT,
    owner_open_id TEXT NOT NULL CHECK (length(owner_open_id)>0),
    image_key TEXT NOT NULL CHECK (length(image_key) BETWEEN 1 AND 256),
    filename TEXT NOT NULL CHECK (length(filename) BETWEEN 1 AND 255),
    mime_type TEXT NOT NULL CHECK (mime_type IN ('image/jpeg','image/png','image/webp','image/gif')),
    size_bytes INTEGER NOT NULL CHECK (size_bytes BETWEEN 1 AND 10485760),
    width INTEGER NOT NULL CHECK (width>0),
    height INTEGER NOT NULL CHECK (height>0),
    animated BOOLEAN NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[a-f0-9]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (width<=CASE WHEN mime_type='image/gif' THEN 2000 ELSE 12000 END
        AND height<=CASE WHEN mime_type='image/gif' THEN 2000 ELSE 12000 END)
);
CREATE INDEX creator_broadcast_images_owner ON creator_broadcast_images(owner_authorization_id,created_at,id);
CREATE INDEX creator_broadcast_images_cleanup ON creator_broadcast_images(created_at,id);
