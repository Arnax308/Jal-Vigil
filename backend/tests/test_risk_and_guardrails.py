"""Unit tests for the deterministic parts: risk scoring and the alert fact check (no network)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shapely.geometry import box  # noqa: E402

from app.clients.openmeteo import Climatology  # noqa: E402
from app.agent.nodes import FOCUS_RADIUS_KM, _focus_around  # noqa: E402
from app.geo import Cell, haversine_km  # noqa: E402
from app.guardrails import check_alert, check_translation  # noqa: E402
from app.risk import assess, imd_category, max_rolling_sum  # noqa: E402

CLIM = [Climatology(lat=13.0, lon=80.2, two_year_1d=88.0, two_year_3d=140.0, record_1d=256.0, years=20)]


def _cells(n: int) -> list[Cell]:
    return [Cell(f"r0c{i}", 13.0, 80.2 + i * 0.01, box(80.2 + i * 0.01, 13.0, 80.21 + i * 0.01, 13.01)) for i in range(n)]


def _flat(mm_per_hour: float, hours: int = 72) -> list[float]:
    return [mm_per_hour] * hours


def test_imd_categories():
    assert imd_category(10) == "light"
    assert imd_category(70) == "heavy"
    assert imd_category(120) == "very heavy"
    assert imd_category(210) == "extremely heavy"


def test_rolling_sum_finds_wettest_day():
    series = [0.0] * 24 + [5.0] * 24 + [0.0] * 24
    assert max_rolling_sum(series, 24) == 120.0


def test_moderate_rain_is_never_high():
    # ~43 mm/day for 3 days (130 mm): below IMD "heavy" and below the 2-year 3-day total of 140 mm.
    risks = assess(_cells(3), [_flat(1.8)] * 3, [2.0, 10.0, 20.0], CLIM)
    assert all(r.level in ("low", "moderate") for r in risks)


def test_long_soaking_rain_can_be_high():
    # ~50 mm/day for 3 days (150 mm) exceeds the 2-year 3-day total, so the IMD cap is lifted.
    risks = assess(_cells(1), [_flat(2.1)], [1.0], CLIM)
    assert risks[0].level in ("high", "severe")


def test_extremely_heavy_rain_is_severe_everywhere():
    risks = assess(_cells(3), [_flat(10.0)] * 3, [2.0, 10.0, 20.0], CLIM)
    assert all(r.level == "severe" for r in risks)


def test_lower_cells_score_higher_for_same_rain():
    low, high = assess(_cells(2), [_flat(4.0)] * 2, [1.0, 25.0], CLIM)
    assert low.score > high.score


def test_focus_marks_only_nearby_cells():
    risks = assess(_cells(2), [_flat(1.0)] * 2, [5.0, 5.0], CLIM, focus=(13.0, 80.2, 0.5))
    assert [r.in_focus for r in risks] == [True, False]


FACTS = {"worst_areas": [{"locality": "Velachery", "max_24h_rain_mm": 134}], "helplines": {"GCC": "1913"}}


def _alert(**overrides):
    alert = {
        "headline_en": "Flood warning for Velachery",
        "body_en": "Up to 134 mm of rain expected. Call 1913.",
        "actions_en": ["Avoid subways"],
    }
    alert.update(overrides)
    return alert


def test_grounded_alert_passes():
    assert check_alert(_alert(), FACTS) == []


def test_invented_number_is_rejected():
    problems = check_alert(_alert(body_en="Up to 200 mm of rain expected. Call 1913."), FACTS)
    assert any("200" in p for p in problems)


def test_invented_helpline_is_rejected():
    problems = check_alert(_alert(body_en="Up to 134 mm expected. Call 9876543210."), FACTS)
    assert any("9876543210" in p for p in problems)


def test_missing_field_is_rejected():
    problems = check_alert(_alert(actions_en=[]), FACTS)
    assert any("actions_en" in p for p in problems)


EN = "Up to 134 mm of rain expected. Call 1913."
TA = "வேளச்சேரியில் 134 மிமீ வரை மழை பெய்ய வாய்ப்புள்ளது. உதவிக்கு 1913 ஐ அழைக்கவும்."


def test_faithful_translation_passes():
    assert check_translation(EN, TA) == []


def test_tamil_digits_count_as_the_same_numbers():
    assert check_translation(EN, TA.replace("134", "௧௩௪")) == []


def test_translation_must_be_tamil_script():
    assert any("Tamil script" in p for p in check_translation(EN, EN))


def test_dropped_helpline_is_caught():
    problems = check_translation(EN, TA.replace(" 1913", ""))
    assert any("lost" in p and "1913" in p for p in problems)


def test_altered_number_is_caught():
    problems = check_translation(EN, TA.replace("134", "143"))
    assert any("added" in p and "143" in p for p in problems)


def test_focus_circle_covers_every_named_place():
    adyar, velachery = {"name": "Adyar", "lat": 13.0012, "lon": 80.2565}, {"name": "Velachery", "lat": 12.9791, "lon": 80.2209}
    focus = _focus_around([adyar, velachery])
    assert focus["name"] == "Adyar, Velachery"
    for place in (adyar, velachery):
        assert haversine_km(focus["lat"], focus["lon"], place["lat"], place["lon"]) < focus["radius_km"]


def test_single_place_keeps_default_radius():
    assert _focus_around([{"name": "Velachery", "lat": 12.9791, "lon": 80.2209}])["radius_km"] == FOCUS_RADIUS_KM


def _approved_run(replay_date):
    from app.events import Run

    run = Run(id="t", request={})
    run.result = {
        "replay_date": replay_date,
        "cells": {"features": [{"properties": {"level": "severe", "in_focus": True}}]},
        "alert": {"headline_en": "Flood <warning> & alert", "body_en": "134 mm expected.", "actions_en": ["Call 1913"]},
    }
    return run


def test_replay_post_is_labelled_demo_and_escaped():
    from app.agent.runner import _format

    post = _format(_approved_run("2022-11-01"), "en", 0)
    assert post.startswith("🧪 <b>DEMO</b>") and "not a current warning" in post
    assert "SEVERE FLOOD RISK" in post
    assert "Flood &lt;warning&gt; &amp; alert" in post


def test_live_post_has_no_demo_label():
    from app.agent.runner import _format

    assert "DEMO" not in _format(_approved_run(None), "en", 0)


def test_translation_retries_sentence_by_sentence_when_numbers_drop(monkeypatch):
    from app.agent import nodes

    english = "Up to 134 mm of rain is expected. The total may reach 182 mm."
    sentences = {
        "Up to 134 mm of rain is expected.": "134 மிமீ வரை மழை எதிர்பார்க்கப்படுகிறது.",
        "The total may reach 182 mm.": "மொத்தம் 182 மிமீ வரை இருக்கலாம்.",
    }
    # The whole passage comes back missing a figure; the sentences come back intact.
    monkeypatch.setattr(nodes.bodhan, "translate", lambda text: sentences.get(text, "134 மிமீ வரை மழை."))
    tamil, problems = nodes._translate_checked(english)
    assert problems == [] and "182" in tamil


def test_approval_passcode(monkeypatch):
    from dataclasses import replace

    from app import main

    monkeypatch.setattr(main, "settings", replace(main.settings, approve_passcode=""))
    assert main.passcode_ok(None)  # no passcode configured: approval is open, as before
    monkeypatch.setattr(main, "settings", replace(main.settings, approve_passcode="monsoon-42"))
    assert main.passcode_ok("monsoon-42")
    assert not main.passcode_ok("monsoon-41")
    assert not main.passcode_ok(None)
