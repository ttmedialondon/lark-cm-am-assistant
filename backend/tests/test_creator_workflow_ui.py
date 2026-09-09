"""Template security and read-only presentation checks, independent of the database."""

import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

APP = Path(__file__).resolve().parents[1] / "app"


def render(*, readonly=False, owner=False):
    env = Environment(loader=FileSystemLoader(APP / "templates"), autoescape=select_autoescape())
    return env.get_template("creator_workflow.html").render(
        authorization={"display_name": "Reviewer"},
        scope={"responsible_id": "remi-id", "responsible_name": "Remi", "readonly": readonly,
               "can_write": not readonly, "can_import": not readonly or owner, "can_admin": owner},
        csrf_token="csrf-test",
        creator_data={"items": [{"handle": "</script><script>alert(1)</script>"}], "summary": {}, "page": 1},
    )


def test_read_only_scope_has_no_write_or_import_controls():
    html = render(readonly=True)
    for control in ("cw-import-open", "cw-import-dialog", "cw-ai-scan", "cw-confirm-selected"):
        assert f'id="{control}"' not in html
    assert "只读核对" in html
    assert "不能补填、代确认" in html
    assert 'id="cw-refresh"' in html


def test_writer_has_four_business_columns_and_json_bootstrap_is_escaped():
    html = render()
    for title in ("达人 handle", "来源", "对应用户", "状态"):
        assert f'<th scope="col">{title}</th>' in html
    assert 'id="cw-import-open"' in html
    assert "</script><script>alert(1)</script>" not in html
    raw = re.search(r'<script id="creator-workflow-data" type="application/json">(.*?)</script>', html).group(1)
    data = json.loads(raw)
    assert data["data"]["items"][0]["handle"] == "</script><script>alert(1)</script>"
    assert data["csrf_token"] == "csrf-test"


def test_owner_can_submit_sources_but_cannot_confirm_for_another_cm():
    html = render(readonly=True, owner=True)
    for control in ("cw-import-open", "cw-import-dialog", "cw-ai-scan"):
        assert f'id="{control}"' in html
    assert 'id="cw-confirm-selected"' not in html


def test_client_does_not_persist_real_data_or_interpolate_it_as_html():
    script = (APP / "static" / "creator-workflow.js").read_text()
    for forbidden in ("localStorage", "sessionStorage", "innerHTML", "insertAdjacentHTML"):
        assert forbidden not in script
    assert 'cache: "no-store"' in script
    assert 'credentials: "same-origin"' in script
    assert 'redirect: "error"' in script
    assert 'headers["X-CSRF-Token"] = csrfToken' in script
