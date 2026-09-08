"""Every shipped instruction must fit a readable Telegram card."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

from PIL import Image
import pytest

from app.guide_art import guide_layout, render_guide
from app.guide_catalog import GUIDE_CATALOG


EXPECTED_TOPICS = {
    "income", "expense", "budget", "history", "limits", "export", "payments",
    "analytics", "tips", "savings", "goals", "reserve", "savings_budget",
    "forecast", "weekly", "search", "family", "settings", "opening", "month",
    "debts", "afford", "credit",
}


def test_feature_catalog_has_public_instruction_for_every_entry_point():
    assert set(GUIDE_CATALOG) == EXPECTED_TOPICS
    for guide in GUIDE_CATALOG.values():
        assert all(guide[field].strip() for field in ("title", "purpose", "inputs", "result", "note", "group"))
        # Keep instructions concise enough for a phone rather than adding a
        # text-heavy manual to a fixed-sized image.
        assert sum(len(guide[field].split()) for field in ("purpose", "inputs", "result", "note")) <= 65


@pytest.mark.parametrize("slug", sorted(EXPECTED_TOPICS))
def test_all_instruction_cards_fit_without_clipped_or_overlapping_copy(slug):
    data = render_guide(slug)
    with Image.open(BytesIO(data)) as image:
        image.load()
        assert image.format == "PNG"
        assert image.size == (1080, 1440)
        assert image.mode == "RGB"
    assert len(data) < 1_000_000

    layout = guide_layout(slug)
    regions = layout["text_regions"]
    for region in regions:
        left, top, right, bottom = region["bounds"]
        assert 60 <= left < right <= layout["width"] - 60, region
        assert 0 <= top < bottom <= layout["height"] - 15, region
        if region["name"] in {"purpose", "inputs", "result", "note"}:
            assert region["font_size"] >= 36
    for index, first in enumerate(regions):
        l1, t1, r1, b1 = first["bounds"]
        for second in regions[index + 1:]:
            l2, t2, r2, b2 = second["bounds"]
            assert r1 <= l2 or r2 <= l1 or b1 <= t2 or b2 <= t1, (slug, first, second)

    # Ensure wrapping preserves all explanatory text, including the caveat.
    for field in ("title", "purpose", "inputs", "result", "note"):
        actual = " ".join(region["text"] for region in regions if region["name"] == field)
        assert actual == " ".join(GUIDE_CATALOG[slug][field].split())


def test_card_cache_is_bounded_and_unknown_topics_cannot_become_arbitrary_content():
    assert render_guide.cache_info().maxsize == 32
    assert render_guide("forecast") is render_guide("forecast")
    for slug in ("", "../private.db", "Ваш личный остаток 12345"):
        with pytest.raises(KeyError):
            render_guide(slug)


def test_parallel_workers_produce_identical_public_cards():
    expected = {slug: render_guide(slug) for slug in ("income", "forecast", "savings_budget")}
    render_guide.cache_clear()
    with ThreadPoolExecutor(max_workers=4) as workers:
        topics = ["income", "forecast", "savings_budget", "income", "forecast", "savings_budget"]
        results = list(workers.map(render_guide, topics))
    assert all(result == expected[slug] for slug, result in zip(topics, results))
