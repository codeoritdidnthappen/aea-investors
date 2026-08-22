"""Drift guard for the duplicated OAuth2 iframe-breakout script (TICK-047).

TICK-045 added an iframe-breakout `<script>` to two OpenEMR vendor template
overrides, `oauth2-login.html.twig` and `scope-authorize.html.twig`. Both extend
`oauth2/oauth2-base.html.twig`, whose `scripts` block could in principle host the
snippet once -- but hoisting it there was rejected (see the `{# ... #}` rationale
in either template): it would require bind-mounting a pinned copy of the vendor
`<head>`/`setupHeader`/`<body>` skeleton shared by *every* OAuth2 page, and would
silently extend breakout to `patient-select.html.twig`, the only other template
extending the base, which this deployment's portal flow never reaches.

Keeping the duplicate is only safe if it cannot drift silently -- the exact risk
TICK-047 was filed against, where a partial edit (e.g. TICK-046's fallback) lands
in one file but not the other and the two pages diverge in behavior with nothing
to flag it. These are static text checks rather than live browser assertions,
matching test_portal_module.py: no browser-automation tool is available in this
repository's execution environment.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OAUTH2_TEMPLATE_DIR = _REPO_ROOT / "openemr_overrides" / "templates" / "oauth2"
_LOGIN_TEMPLATE = _OAUTH2_TEMPLATE_DIR / "oauth2-login.html.twig"
_SCOPE_AUTHORIZE_TEMPLATE = _OAUTH2_TEMPLATE_DIR / "scope-authorize.html.twig"

_BLOCK_START = "{% block scripts %}"
_BLOCK_END = "{% endblock %}"


def _scripts_block(template: Path) -> str:
    """Return the template's `scripts` block, delimiters included."""
    text = template.read_text(encoding="utf-8")
    start = text.index(_BLOCK_START)
    end = text.index(_BLOCK_END, start) + len(_BLOCK_END)
    return text[start:end]


def test_breakout_scripts_block_is_byte_identical_in_both_oauth2_overrides() -> None:
    login_block = _scripts_block(_LOGIN_TEMPLATE)
    scope_block = _scripts_block(_SCOPE_AUTHORIZE_TEMPLATE)

    assert login_block == scope_block, (
        "The OAuth2 iframe-breakout `scripts` block has drifted between "
        f"{_LOGIN_TEMPLATE.name} and {_SCOPE_AUTHORIZE_TEMPLATE.name}. It is duplicated "
        "deliberately (TICK-047) and the two copies must stay byte identical: edit one, "
        "edit the other. Otherwise the login page and the consent page silently differ "
        "in breakout behavior."
    )


def test_breakout_scripts_block_still_breaks_out_of_an_embedding_iframe() -> None:
    block = _scripts_block(_LOGIN_TEMPLATE)

    assert "window.top !== window.self" in block
    assert "window.top.location.href = window.location.href;" in block


def test_breakout_scripts_block_points_at_its_duplicate_in_the_sibling_template() -> None:
    block = _scripts_block(_LOGIN_TEMPLATE)

    assert _LOGIN_TEMPLATE.name in block
    assert _SCOPE_AUTHORIZE_TEMPLATE.name in block
