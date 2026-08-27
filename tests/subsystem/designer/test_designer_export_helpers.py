from __future__ import annotations

import pytest

from row_bot.designer import export
from row_bot.designer.state import DesignerPage, DesignerProject


pytestmark = pytest.mark.subsystem


def _project() -> DesignerProject:
    return DesignerProject(
        name='Quarterly <Plan> & "Notes"',
        pages=[
            DesignerPage(title="Overview <Q1>", html="<main>one & two</main>"),
            DesignerPage(title="Details & Risks", html="<section>three</section>"),
            DesignerPage(title="Appendix", html="<footer>four</footer>"),
        ],
    )


def test_page_ranges_support_mixes_whitespace_duplicates_and_invalid_values() -> None:
    assert export._parse_page_range(None, 3) == [0, 1, 2]
    assert export._parse_page_range("all", 3) == [0, 1, 2]
    assert export._parse_page_range(" 1, 2-3, 2 ", 4) == [0, 1, 2]
    assert export._parse_page_range("2-99", 3) == [1, 2]

    with pytest.raises(ValueError):
        export._parse_page_range("first", 3)


def test_playwright_export_launch_uses_reviewed_managed_executable(monkeypatch) -> None:
    from row_bot.browser import runtime

    calls: list[dict] = []

    class _Chromium:
        @staticmethod
        def launch(**kwargs):
            calls.append(kwargs)
            return object()

    monkeypatch.setattr(
        runtime,
        "playwright_chromium_launch_options",
        lambda: {
            "headless": True,
            "executable_path": "C:/synthetic/managed/chrome.exe",
        },
    )

    browser = export._launch_playwright_browser(type("PW", (), {"chromium": _Chromium()})())

    assert browser is not None
    assert calls == [
        {
            "headless": True,
            "executable_path": "C:/synthetic/managed/chrome.exe",
        }
    ]


def test_destination_description_and_next_available_path(tmp_path) -> None:
    project = _project()
    project.name = "Bad / Name: Demo?"

    assert export._sanitize_name("  ***  ") == "___"
    assert export.describe_export_destination(project, "html", directory=tmp_path).name == "Bad _ Name_ Demo_.html"
    assert export.describe_export_destination(project, "png", pages="1-2", directory=tmp_path).name == (
        "Bad _ Name_ Demo__pages.zip"
    )
    assert export.describe_export_destination(project, "pptx", mode="structured", directory=tmp_path).name.endswith(
        "_editable.pptx"
    )

    target = tmp_path / "export.html"
    target.write_text("existing", encoding="utf-8")
    (tmp_path / "export (2).html").write_text("existing", encoding="utf-8")

    assert export._next_available_export_path(target) == tmp_path / "export (3).html"


def test_save_bytes_returns_exported_bytes_and_permission_message(tmp_path) -> None:
    saved = export._save_bytes(tmp_path / "nested" / "demo.bin", b"payload", "Demo")

    assert isinstance(saved, export.ExportedBytes)
    assert bytes(saved) == b"payload"
    assert saved.saved_path == tmp_path / "nested" / "demo.bin"
    assert saved.saved_path.read_bytes() == b"payload"
    assert "file is open or locked" in export._permission_denied_message(saved.saved_path, "Demo")


def test_html_export_escapes_titles_attrs_and_selected_pages(monkeypatch) -> None:
    import row_bot.designer.fonts as fonts

    monkeypatch.setattr(fonts, "get_font_css_embedded", lambda _family: "")

    html = export.build_html_export(_project(), pages="2").decode("utf-8")

    assert "Quarterly &lt;Plan&gt; &amp; \"Notes\"" in html
    assert "Details &amp; Risks" in html
    assert "Overview <Q1>" not in html
    assert "Page 1:" not in html
    assert "Page 2: Details &amp; Risks" in html
    assert "&lt;section&gt;three&lt;/section&gt;" in html
    assert 'srcdoc="' in html
    assert "<section>three</section>" not in html


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("16px", 16.0),
        ("1.5em", 1.5),
        (24, 24.0),
        ("calc(10px + 2px)", 10.0),
        ("n/a", 7.0),
    ],
)
def test_css_length_parsing(value, expected) -> None:
    assert export._css_length_to_px(value, default=7.0) == expected


def test_unit_conversions_font_weight_and_alignment() -> None:
    from pptx.enum.text import PP_ALIGN

    assert export._px_to_emu("96px") == 914400
    assert export._px_to_pt("16px") == pytest.approx(12.0)
    assert export._font_name_from_css('"Inter", Arial, sans-serif') == "Inter"
    assert export._weight_from_css("bold") == 700
    assert export._weight_from_css("450") == 450
    assert export._alignment_from_css("center") == PP_ALIGN.CENTER
    assert export._alignment_from_css("unknown") == PP_ALIGN.LEFT


def test_parse_css_color_handles_named_hex_rgba_percentages_and_invalid() -> None:
    assert export._parse_css_color("orange") == ((255, 165, 0), 1.0)
    assert export._parse_css_color("#0f8") == ((0, 255, 136), 1.0)
    assert export._parse_css_color("#33669980") == ((51, 102, 153), pytest.approx(128 / 255))
    assert export._parse_css_color("rgba(10, 20, 30, 0.25)") == ((10, 20, 30), 0.25)
    assert export._parse_css_color("rgb(50%, 25%, 0%)") == ((128, 64, 0), 1.0)
    assert export._parse_css_color("transparent") is None
    assert export._parse_css_color("not-a-color") is None


def test_gradient_and_shadow_parsers_are_stable() -> None:
    assert export._split_css_top_level("rgb(1, 2, 3), linear-gradient(red, blue), tail") == [
        "rgb(1, 2, 3)",
        "linear-gradient(red, blue)",
        "tail",
    ]
    assert export._parse_css_angle("to top right") == 45.0
    assert export._parse_css_angle("0.5turn") == 180.0

    linear = export._parse_linear_gradient("linear-gradient(90deg, red 0%, rgba(0, 0, 255, .5) 100%)")
    assert linear is not None
    assert linear["type"] == "linear"
    assert linear["angle"] == 90.0
    assert linear["stops"][0] == (0.0, ((255, 0, 0), 1.0))

    radial = export._parse_radial_gradient("radial-gradient(circle, #fff 0%, rgb(0, 0, 0) 100%)")
    assert radial is not None
    assert radial["type"] == "radial"
    assert len(radial["stops"]) == 2

    shadows = export._parse_box_shadow("inset 0 0 4px #000, 2px 4px 8px 1px rgba(10, 20, 30, 0.5)")
    assert shadows == [{"x": 2.0, "y": 4.0, "blur": 8.0, "spread": 1.0, "rgb": (10, 20, 30), "alpha": 0.5}]
    assert export._parse_box_shadow("none") == []


def test_drawingml_fill_and_shadow_helpers_emit_stable_strings() -> None:
    assert export._hex_rgb((1, 16, 255)) == "0110FF"
    assert export._build_solid_fill_xml((255, 0, 0), 0.5) == (
        '<a:solidFill xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:srgbClr val="FF0000"><a:alpha val="50000"/></a:srgbClr>'
        "</a:solidFill>"
    )

    linear_xml = export._build_linear_gradient_xml(
        {"type": "linear", "angle": 180.0, "stops": [(0.0, ((255, 0, 0), 1.0)), (1.0, ((0, 0, 255), 0.5))]}
    )
    assert '<a:lin ang="5400000" scaled="0"/>' in linear_xml
    assert '<a:gs pos="100000"><a:srgbClr val="0000FF"><a:alpha val="50000"/></a:srgbClr></a:gs>' in linear_xml

    radial_xml = export._build_radial_gradient_xml({"type": "radial", "stops": [(0.0, ((255, 255, 255), 1.0))]})
    assert '<a:path path="circle">' in radial_xml

    shadow_xml = export._build_outer_shadow_xml({"x": 3, "y": 4, "blur": 6, "rgb": (1, 2, 3), "alpha": 0.4})
    assert 'dist="47625"' in shadow_xml
    assert '<a:srgbClr val="010203"><a:alpha val="40000"/></a:srgbClr>' in shadow_xml


def test_rendered_sort_key_and_text_dedupe_keep_distinct_items() -> None:
    items = [
        {"kind": "text", "text": "Heading", "x": 10, "y": 10, "width": 100, "height": 20, "order": 1},
        {"kind": "text", "text": "Heading", "x": 12, "y": 11, "width": 98, "height": 20, "order": 2},
        {"kind": "text", "text": "Heading Accent", "x": 10, "y": 40, "width": 140, "height": 20, "order": 3},
        {"kind": "text", "text": "Accent", "x": 85, "y": 40, "width": 60, "height": 20, "order": 4},
        {"kind": "text", "text": "Separate", "x": 300, "y": 300, "width": 50, "height": 20, "order": 5},
        {"kind": "shape", "order": 0},
    ]

    deduped = export._dedupe_text_items(items)

    assert [item.get("text") for item in deduped if item.get("kind") == "text"] == [
        "Heading",
        "Heading Accent",
        "Separate",
    ]
    assert export._rendered_item_sort_key({"kind": "shape", "zIndex": 2, "order": 5}) < export._rendered_item_sort_key(
        {"kind": "text", "zIndex": 2, "order": 5}
    )
