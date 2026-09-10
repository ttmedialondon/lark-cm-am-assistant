"""Template security and read-only presentation checks, independent of the database."""

import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

APP = Path(__file__).resolve().parents[1] / "app"


def render(*, readonly=False, owner=False, test_editor=False, responsible_id="remi-id"):
    env = Environment(loader=FileSystemLoader(APP / "templates"), autoescape=select_autoescape())
    return env.get_template("creator_workflow.html").render(
        authorization={"display_name": "Reviewer"},
        scope={"responsible_id": responsible_id, "responsible_name": "Remi", "readonly": readonly,
               "can_write": not readonly, "can_import": (not readonly and not test_editor) or owner,
               "can_scan": (not readonly and not test_editor) or owner,
               "can_admin": owner, "test_editor": test_editor},
        csrf_token="csrf-test",
        creator_data={"items": [{"handle": "</script><script>alert(1)</script>"}], "summary": {}, "page": 1},
    )


def test_read_only_scope_has_no_write_or_import_controls():
    html = render(readonly=True)
    for control in ("cw-import-open", "cw-import-dialog", "cw-ai-scan", "cw-initial-retry", "cw-confirm-selected"):
        assert f'id="{control}"' not in html
    assert "只读核对" in html
    assert "不能补填、代确认" in html
    assert 'id="cw-refresh"' in html
    assert 'id="cw-initial-refresh"' in html
    owner = render(readonly=True, owner=True, responsible_id=None)
    assert 'id="cw-import-open" disabled' in owner
    assert 'id="cw-confirm-selected"' not in owner
    tester = render(test_editor=True)
    assert 'id="cw-confirm-selected"' in tester
    for control in ("cw-import-open", "cw-ai-scan", "cw-initial-retry"):
        assert f'id="{control}"' not in tester


def test_writer_has_type_and_existing_business_columns_and_json_bootstrap_is_escaped():
    html = render()
    for title in ("达人 handle", "达人类型", "来源", "对应用户", "状态"):
        assert f'<th scope="col">{title}</th>' in html
    assert 'id="cw-import-open"' in html
    assert "</script><script>alert(1)</script>" not in html
    raw = re.search(r'<script id="creator-workflow-data" type="application/json">(.*?)</script>', html).group(1)
    data = json.loads(raw)
    assert data["data"]["items"][0]["handle"] == "</script><script>alert(1)</script>"
    assert data["csrf_token"] == "csrf-test"


def test_client_does_not_persist_real_data_or_interpolate_it_as_html():
    script = (APP / "static" / "creator-workflow.js").read_text()
    for forbidden in ("localStorage", "sessionStorage", "innerHTML", "insertAdjacentHTML"):
        assert forbidden not in script
    assert 'cache: "no-store"' in script
    assert 'credentials: "same-origin"' in script
    assert 'redirect: "error"' in script
    assert 'headers["X-CSRF-Token"] = csrfToken' in script
