"""The dashboard serves Chart.js and Font Awesome itself, checked by hash.

The page loaded both from public CDNs (cdn.jsdelivr.net, cdnjs), and its
Content-Security-Policy had to allow those servers: a CDN that was hacked
could hand the operator's page a script that sends mail as SUPPLECO, and an
office network that blocks the CDNs left the page without charts or icons.
Now the files are in static/vendor, copies of the published releases checked
against the publishers' hashes, and the policy allows the dashboard itself
only (tests/test_dashboard_requests_are_checked.py). Here: each file is the
one recorded, the README records the same, the page asks for these files and
the app serves them, and the stylesheet finds its fonts.
"""

import hashlib
import re

import pytest

import main
from main import PROJECT_ROOT as ROOT

VENDOR = ROOT / "static" / "vendor"

# The published files' sha256, also in static/vendor/README.md.
VENDORED = {
    "chartjs/chart.umd.js": "74401d738dd3e03ee5dfb3b6841210fe2c4ead8a960c4011ca4ba0b78a9fd8f3",
    "fontawesome/css/all.min.css": "1edb1725a9ea8ca4dcf2f5508cee183218aa1685e47c1b23056717f754f58ebf",
    "fontawesome/webfonts/fa-brands-400.woff2": "748332090c4b8e20f95d0ff59f0be20fa9c889359d3b36d4b886d73376054207",
    "fontawesome/webfonts/fa-regular-400.woff2": "8e7e5ea1b15f62ab14dbd41768e8fbcd21cc859a4ea5da812457ee714299fb35",
    "fontawesome/webfonts/fa-solid-900.woff2": "7152a6933ee3d690ec2af3d09da9d701723d16aa3410a6d80f28ff8866f3b880",
    "fontawesome/webfonts/fa-v4compatibility.woff2": "694a17c3d9d6c05f8aac63c544615552a4b220e9a4de863d87341a6bcfc1bc8d",
}


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_each_file_is_the_published_one(name):
    assert _sha256(VENDOR / name) == VENDORED[name], name


def test_no_other_file_is_served_from_the_vendor_folder():
    present = {
        p.relative_to(VENDOR).as_posix()
        for p in VENDOR.rglob("*")
        if p.is_file() and p.name != "README.md"
    }
    assert present == set(VENDORED)


def test_the_readme_records_the_same_hashes_and_the_licences():
    readme = (VENDOR / "README.md").read_text(encoding="utf-8")
    rows = dict(re.findall(r"^\| `([^`]+)` \| [^|]+ \| [^|]+ \| `([0-9a-f]{64})` \|$", readme, re.MULTILINE))
    assert rows == VENDORED
    assert "Chart.js 4.4.1" in readme and "Font Awesome Free 6.4.0" in readme
    for licence in ("MIT", "CC BY 4.0", "SIL Open Font License 1.1"):
        assert licence in readme, licence


def test_the_page_asks_for_these_files_and_the_app_serves_them(settings_factory):
    client = main.create_dashboard_app(settings_factory()).test_client()
    html = client.get("/classic").get_data(as_text=True)
    for name in ("chartjs/chart.umd.js", "fontawesome/css/all.min.css"):
        assert f'"/static/vendor/{name}"' in html, name
    for name, sha256 in VENDORED.items():
        resp = client.get(f"/static/vendor/{name}")
        assert resp.status_code == 200, name
        assert hashlib.sha256(resp.data).hexdigest() == sha256, name
        resp.close()


def test_the_stylesheet_finds_every_font_it_names_first():
    """Each @font-face names its .woff2 file first, which every current
    browser takes; those files must be beside the stylesheet's folder."""
    css = (VENDOR / "fontawesome" / "css" / "all.min.css").read_text(encoding="utf-8")
    first = set(re.findall(r"src:url\(\.\./webfonts/([^)]+\.woff2)\)", css))
    assert first == {
        "fa-brands-400.woff2",
        "fa-regular-400.woff2",
        "fa-solid-900.woff2",
        "fa-v4compatibility.woff2",
    }
    for font in first:
        assert (VENDOR / "fontawesome" / "webfonts" / font).is_file(), font


def test_the_vendor_files_go_into_the_image():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for rule in ignored:
        rule = rule.strip()
        assert not (rule and not rule.startswith("#") and "vendor" in rule), rule
        assert rule not in ("*.woff2", "*.js", "*.css", "static/"), rule
