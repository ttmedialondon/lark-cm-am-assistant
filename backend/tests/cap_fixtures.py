"""CAP fixtures including historical data to protect migration compatibility."""
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from app import admin_reporting
from app.ai_feature_config import api_key_cipher
from app.crypto import message_body_cipher


@pytest.fixture
def pilot(pg, monkeypatch):
    settings = SimpleNamespace(token_encryption_key=b"o" * 32)
    ids = {name: uuid4() for name in ("owner", "cm")}
    for name, identity in ids.items():
        pg.execute(
            "INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext,body_consent_at) "
            "VALUES(%s,%s,%s,'test-token',NOW()-INTERVAL '1 day')", (identity, "ou_" + name, name),
        )
        pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,%s)", (identity, name))
        pg.execute("INSERT INTO lark_identities(open_id,contact_id,display_name) VALUES(%s,%s,%s)",
                   ("ou_" + name, identity, name))
    pg.execute("INSERT INTO admin_roles(authorization_id,role) VALUES(%s,'owner')", (ids["owner"],))
    pg.execute(
        "INSERT INTO ai_feature_configs(feature,provider,project_id,model,enabled,api_key_ciphertext,key_suffix) "
        "VALUES('creator_import','openai','test-project','test-model',FALSE,%s,'only') "
        "ON CONFLICT(feature) DO UPDATE SET model=EXCLUDED.model,project_id=EXCLUDED.project_id,"
        "api_key_ciphertext=EXCLUDED.api_key_ciphertext,key_suffix=EXCLUDED.key_suffix",
        (api_key_cipher(settings.token_encryption_key).encrypt("sk-test-only"),),
    )
    cipher = message_body_cipher(settings.token_encryption_key)

    def person(name, pairs=0):
        contact_id, open_id, chat = uuid4(), "ou_" + name, "chat_" + name
        pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,%s)", (contact_id, name))
        pg.execute("INSERT INTO lark_identities(open_id,contact_id,display_name) VALUES(%s,%s,%s)",
                   (open_id, contact_id, name))
        pg.execute("INSERT INTO contact_roles(contact_id,role,status,source,is_primary) "
                   "VALUES(%s,'creator','confirmed','ai_auto_confirmed',TRUE)", (contact_id,))
        pg.execute(
            "INSERT INTO contact_assessments(id,contact_id,status,policy_version,result) "
            "VALUES(%s,%s,'succeeded','people-identity-v1',%s)",
            (uuid4(), contact_id, Jsonb({"reason_code": "classified", "roles": [{"role": "creator",
                                                                                 "confidence": 0.99}]})),
        )
        if pairs:
            pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES(%s,'Creator private conversation','p2p')",
                       (chat,))
            pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_open_id) "
                       "VALUES(%s,%s,%s)", (ids["cm"], chat, open_id))
        for index in range(pairs):
            question_id = f"{name}_{index}_question"
            for minute, sender, message_id, parent, text in (
                (0, open_id, question_id, None, f"Can you arrange the campaign sample number {index} for me?"),
                (1, "ou_cm", f"{name}_{index}_answer", question_id,
                 f"I am your manager and will arrange your campaign sample number {index}."),
            ):
                pg.execute(
                    "INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,"
                    "body_ciphertext,message_create_time,parent_id) VALUES(%s,%s,%s,'user','text',%s,%s,%s)",
                    (chat, message_id, sender, cipher.encrypt(json.dumps({"text": text})),
                     datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=index * 10, minutes=minute), parent),
                )
                pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
                           "VALUES(%s,%s,%s)", (ids["cm"], chat, message_id))
        return contact_id

    ids["creator"] = person("creator", pairs=8)
    ids["silent"] = person("silent")
    ids["legacy"] = person("legacy")
    account_id, candidate_id = uuid4(), uuid4()
    pg.execute("INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) "
               "VALUES(%s,'tiktok','legacy_account','legacy_account')", (account_id,))
    pg.execute("INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source) "
               "VALUES(%s,%s,'owner','confirmed','cm_confirmation')", (ids["legacy"], account_id))
    pg.execute(
        "INSERT INTO creator_workflow_candidates(id,normalized_handle,responsible_authorization_id,contact_id,"
        "relationship,status,social_account_id) VALUES(%s,'legacy_account',%s,%s,'owner','confirmed',%s)",
        (candidate_id, ids["owner"], ids["legacy"], account_id),
    )
    pg.execute(
        "INSERT INTO creator_account_assignments(id,social_account_id,responsible_authorization_id,contact_id,"
        "relationship,source,candidate_id,created_by_authorization_id) "
        "VALUES(%s,%s,%s,%s,'owner','cm_confirmation',%s,%s)",
        (uuid4(), account_id, ids["owner"], ids["legacy"], candidate_id, ids["owner"]),
    )
    monkeypatch.setattr(admin_reporting, "connection", lambda: nullcontext(pg))
    return SimpleNamespace(pg=pg, ids=ids, settings=settings, person=person, calls=[], after_request=[])

def _snapshot(pg, tables):
    return {table: pg.execute(f"SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]') AS rows "
                             f"FROM {table} t").fetchone()["rows"] for table in tables}
