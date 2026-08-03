import json
import re
import subprocess
from urllib.parse import unquote, urlsplit
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "docs" / "site.css").read_text(encoding="utf-8")
JS = (ROOT / "docs" / "site.js").read_text(encoding="utf-8")

WINDOWS_URL = (
    "https://github.com/siddsachar/row-bot/releases/download/v4.5.0/"
    "Row-Bot-4.5.0-Windows-x64.exe"
)
MAC_URL = (
    "https://github.com/siddsachar/row-bot/releases/download/v4.5.0/"
    "Row-Bot-4.5.0-macOS-arm64.dmg"
)
LINUX_COMMAND = (
    "curl -fsSL https://raw.githubusercontent.com/siddsachar/row-bot/main/"
    "installer/install-linux.sh | bash -s -- 4.5.0"
)
MARKETING_PAGES = ("index.html", "features.html", "architecture.html", "contact.html", "404.html")


class LandingPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.sections: list[str] = []
        self.links: list[dict[str, str]] = []
        self.images: list[dict[str, str]] = []
        self.codes: list[dict[str, str]] = []
        self._active_code: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "section" and values.get("id"):
            self.sections.append(values["id"])
        if tag == "a":
            self.links.append(values)
        if tag == "img":
            self.images.append(values)
        if tag == "code":
            self._active_code = values
            self.codes.append(values)

    def handle_endtag(self, tag: str) -> None:
        if tag == "code":
            self._active_code = None

    def handle_data(self, data: str) -> None:
        if self._active_code is not None:
            self._active_code["text"] = self._active_code.get("text", "") + data


def _parse() -> LandingPageParser:
    parser = LandingPageParser()
    parser.feed(HTML)
    return parser


def test_landing_page_is_evergreen_and_current() -> None:
    assert "4.4.0" not in HTML
    assert "4.4.0" not in JS
    assert "What’s new" not in HTML
    assert "What's new" not in HTML
    assert 'id="new"' not in HTML

    json_ld_match = re.search(
        r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
        HTML,
        re.DOTALL,
    )
    assert json_ld_match
    metadata = json.loads(json_ld_match.group(1))
    assert metadata["softwareVersion"] == "4.5.0"
    assert metadata["downloadUrl"].endswith("/releases/tag/v4.5.0")

    parser = _parse()
    assert all(image.get("width") and image.get("height") for image in parser.images)
    assert parser.sections == [
        "top",
        "proof",
        "product",
        "demos",
        "architecture",
        "faq",
        "install",
    ]
    assert "Row-Bot 4.5.0 available" in HTML
    assert "Row-Bot &middot; v4.5.0 &middot; Apache 2.0" in HTML


def test_landing_page_fallbacks_and_links_are_complete() -> None:
    parser = _parse()
    for link in parser.links:
        href = link.get("href", "")
        if href.startswith("#") and len(href) > 1:
            assert href[1:] in parser.ids, href

    os_primary = [link for link in parser.links if "data-os-primary" in link]
    assert {link["href"] for link in os_primary} == {"#demos", "#install"}
    assert all(not link["href"].endswith((".exe", ".dmg")) for link in os_primary)

    hrefs = [link.get("href") for link in parser.links]
    assert WINDOWS_URL in hrefs
    assert MAC_URL in hrefs
    assert "docs/getting-started/installation" in hrefs
    linux_code = next(code for code in parser.codes if "data-linux-command" in code)
    assert linux_code["text"] == LINUX_COMMAND
    assert LINUX_COMMAND in JS


def test_mobile_handoff_and_product_media_contracts() -> None:
    assert "Open <strong>row-bot.ai</strong> on your computer to install it." in HTML
    assert "Share desktop link" in HTML
    assert "Copy row-bot.ai" in HTML
    assert "not a standalone iOS or Android download" in HTML
    assert "navigator.share" in JS
    assert "document.execCommand?.('copy')" in JS

    parser = _parse()
    video_images = [image for image in parser.images if "img.youtube.com" in image.get("src", "")]
    assert len(video_images) == 3
    assert all(image.get("loading") == "lazy" for image in video_images)
    assert all(image.get("width") and image.get("height") for image in video_images)
    assert "background-image:url" not in HTML
    assert "youtube-nocookie.com/embed" in JS


def test_device_states_and_intent_events_remain_distinct() -> None:
    for signal in (
        "userAgentData?.mobile",
        "iphone|ipod",
        "ua.includes('android')",
        "platform === 'macintel' && touchPoints > 1",
        "!isWindows",
        "(pointer: coarse)",
    ):
        assert signal in JS

    for event_name in (
        "desktop_download_click",
        "linux_install_view",
        "linux_command_copy",
        "mobile_desktop_link_share",
        "mobile_desktop_link_copy",
        "product_demo_open",
        "installation_docs_open",
    ):
        assert f"'{event_name}'" in JS

    assert "'download_click'" not in JS
    placements = set(re.findall(r'data-placement="([^"]+)"', HTML))
    assert placements <= {
        "navigation",
        "hero",
        "platform_selector",
        "final_install",
        "mobile_handoff",
        "product_demo",
    }
    assert "html[data-device='mobile'] .desktop-install-options" in CSS
    assert "html:not(.js) .mobile-handoff" in CSS
    assert "min-width: 0" in CSS
    assert "width: 44px;\n    height: 44px" in CSS
    assert ".release-card" not in CSS
    assert "@media (prefers-reduced-motion: reduce)" in CSS


def test_device_detection_runtime_matrix() -> None:
    runtime_test = ROOT / "tests" / "docs" / "landing_page_runtime_test.cjs"
    result = subprocess.run(
        ["node", str(runtime_test)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def _page_parser(name: str) -> LandingPageParser:
    parser = LandingPageParser()
    parser.feed((ROOT / "docs" / name).read_text(encoding="utf-8"))
    return parser


def _block_links(content: str, class_name: str) -> list[str]:
    match = re.search(
        rf'<(?:div|footer)[^>]*class="[^"]*\b{class_name}\b[^"]*"[^>]*>(.*?)</(?:div|footer)>',
        content,
        re.DOTALL,
    )
    assert match, class_name
    return re.findall(r'<a\b[^>]*href="([^"]+)"', match.group(1))


def test_marketing_navigation_and_footers_share_one_contract() -> None:
    navigation_prefix = [
        "features.html",
        "architecture.html",
        "docs/",
        "contact.html",
        "https://github.com/siddsachar/row-bot",
    ]
    footer_links = [
        "index.html",
        "features.html",
        "architecture.html",
        "docs/",
        "index.html#privacy",
        "index.html#comparison",
        "contact.html",
        "https://sydsachar.com/category/row-bot/",
        "https://github.com/siddsachar/row-bot",
    ]

    for name in MARKETING_PAGES:
        content = (ROOT / "docs" / name).read_text(encoding="utf-8")
        nav_links = _block_links(content, "nav-menu")
        assert nav_links[:5] == navigation_prefix, name
        assert nav_links[5] in {"#install", "index.html#install"}, name
        assert _block_links(content, "footer-links") == footer_links, name
        assert 'href="index.html#new"' not in content
        assert "What’s new" not in content


def test_all_marketing_internal_links_and_images_resolve() -> None:
    parsed_pages = {name: _page_parser(name) for name in MARKETING_PAGES}

    for name, parser in parsed_pages.items():
        for link in parser.links:
            href = link.get("href", "")
            parts = urlsplit(href)
            if parts.scheme or parts.netloc or href.startswith(("mailto:", "tel:")):
                continue
            target_name = unquote(parts.path) or name
            target = ROOT / "docs" / target_name
            if target.is_dir() or (not target.suffix and (target / "index.html").is_file()):
                target = target / "index.html"
            assert target.is_file(), f"{name}: {href}"
            if parts.fragment:
                target_parser = parsed_pages.get(target.name)
                if target_parser is None:
                    target_parser = LandingPageParser()
                    target_parser.feed(target.read_text(encoding="utf-8"))
                assert parts.fragment in target_parser.ids, f"{name}: {href}"

        for image in parser.images:
            source = image.get("src", "")
            if urlsplit(source).scheme:
                continue
            assert (ROOT / "docs" / unquote(source)).is_file(), f"{name}: {source}"
            assert image.get("width") and image.get("height"), f"{name}: {source}"


def test_cross_page_cta_fallbacks_are_mobile_safe() -> None:
    for name in MARKETING_PAGES:
        content = (ROOT / "docs" / name).read_text(encoding="utf-8")
        parser = _page_parser(name)
        os_primary = [link for link in parser.links if "data-os-primary" in link]
        assert os_primary, name
        assert all(not link["href"].endswith((".exe", ".dmg")) for link in os_primary), name
        assert "data-download=" not in content

    assert "sectionTarget('install', locationInfo)" in JS
    assert "navMenu.inert = !shouldOpen" in JS
    assert "visibility: hidden" in CSS
    assert "pointer-events: none" in CSS


def test_features_inventory_is_evergreen_and_documented() -> None:
    features = (ROOT / "docs" / "features.html").read_text(encoding="utf-8")
    for historical in ("New in 4.", "Improved in 4.", "Restored in 4.", "Beta in 4.", "Version 4."):
        assert historical not in features
    assert "tag--new" not in features
    assert features.count('class="feature-chapter"') == 12
    assert features.count('class="chapter-links"') == 12
    assert "consent-gated third-party telemetry" in features
    assert "No first-party telemetry" in features


def test_architecture_contact_and_not_found_progressive_contracts() -> None:
    architecture = (ROOT / "docs" / "architecture.html").read_text(encoding="utf-8")
    contact = (ROOT / "docs" / "contact.html").read_text(encoding="utf-8")
    not_found = (ROOT / "docs" / "404.html").read_text(encoding="utf-8")

    assert architecture.count("data-lightbox-open") == 10
    assert architecture.count("data-lightbox role=\"dialog\"") == 10
    assert architecture.count("data-lightbox-close") == 10
    assert architecture.count('loading="lazy" decoding="async"') == 20
    assert "event.key === 'Escape'" in JS
    assert "lightboxBackground.forEach" in JS
    assert "github.com/siddsachar/row-bot/blob/main/docs/ARCHITECTURE.md" in architecture

    assert 'action="https://formspree.io/f/mwvagdzv"' in contact
    assert "Submitting sends these form fields to Formspree" in contact
    assert "<option>Security</option>" not in contact
    assert "use one of the project channels" in contact
    assert 'method="POST"' in contact

    assert '<meta name="robots" content="noindex, follow">' in not_found
    assert '<main class="not-found-main shell" id="main">' in not_found
    assert 'href="#main">Skip to content</a>' in not_found
