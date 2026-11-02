from datetime import date

from research.planner import next_monday
from research.scorer import (clean_title, cluster, competition_score, contains_any, demand_score,
                             ease_score, non_latin_letters, outlier_ratio, scaled_log, skip_reason,
                             total_score, trend_score)
from research.youtube import parse_duration


def test_parse_duration():
    assert parse_duration("PT1H2M3S") == 3723
    assert parse_duration("PT45S") == 45
    assert parse_duration("P1DT1M") == 86460
    assert parse_duration("P0D") == 0
    assert parse_duration("garbage") == 0


def test_outlier_ratio_uses_floor_for_tiny_channels():
    assert outlier_ratio(50_000, 100, floor=1000) == 50
    assert outlier_ratio(50_000, 10_000, floor=1000) == 5


def test_scaled_log_bounds():
    assert scaled_log(0.5, 50) == 0
    assert scaled_log(50, 50) == 1
    assert scaled_log(5000, 50) == 1
    assert 0 < scaled_log(7, 50) < 1


def test_demand_rewards_bigger_outliers_and_clusters():
    assert demand_score(30, 20_000, 1) > demand_score(4, 20_000, 1)
    assert demand_score(10, 5_000, 3) > demand_score(10, 5_000, 1)
    assert demand_score(1000, 10**7, 10) == 1.0


def test_competition_drops_with_big_channels():
    assert competition_score(0) == 1.0
    assert competition_score(3) == 0.0
    assert competition_score(10) == 0.0


def test_term_matching_is_word_bounded():
    assert contains_any("Make Money with AI", ["money"])
    assert not contains_any("Livestream recap", ["live"])
    assert contains_any("Copy.ai review", ["copy.ai"])


def test_ease_prefers_faceless_formats():
    easy, hard = ["how to", "tools"], ["vlog", "reaction"]
    assert ease_score("10 AI tools you need", easy, hard) == 1.0
    assert ease_score("My AI vlog", easy, hard) == 0.3
    assert ease_score("Something else", easy, hard) == 0.6


def test_total_score_range():
    weights = {"a": 0.5, "b": 0.5}
    assert total_score({"a": 1, "b": 1}, weights) == 100
    assert total_score({"a": 0, "b": 0}, weights) == 0


def test_clean_title_strips_emoji_and_hashtags():
    assert clean_title("🔥 10 AI Tools #ai #shorts") == "10 AI Tools"


def test_cluster_groups_similar_titles():
    items = [{"clean": "10 AI tools that will save you hours"},
             {"clean": "10 AI Tools That Save You Hours"},
             {"clean": "How to cook pasta"}]
    groups = cluster(items, threshold=75)
    assert len(groups) == 2
    assert len(groups[0]) == 2


def test_next_monday_is_always_in_the_future():
    assert next_monday(date(2026, 10, 21)) == date(2026, 10, 26)  # Wednesday -> next Monday
    assert next_monday(date(2026, 10, 26)) == date(2026, 11, 2)   # Monday -> the following Monday


def video(**overrides):
    return {"title": "10 AI tools for work", "tags": "", "language": "en", "views": 50_000,
            "likes": 900, "comments": 40, **overrides}


def test_skip_reason_filters_ads_languages_and_off_niche():
    niche = ["ai", "tools"]
    assert skip_reason(video(), niche) is None
    assert skip_reason(video(likes=0, comments=0), niche) == "paid ad"
    assert skip_reason(video(views=9_000_000, likes=35, comments=0), niche) == "paid ad"
    assert skip_reason(video(likes=0, comments=12), niche) is None  # likes hidden but people comment
    assert skip_reason(video(language="hi"), niche) == "not English"
    assert skip_reason(video(language="en-US"), niche) is None
    assert skip_reason(video(title="CapCut जैसे 3 FREE AI Apps"), niche) == "not English"
    assert skip_reason(video(title="Rapunzel gets a love letter"), niche) == "off-niche"
    assert skip_reason(video(title="Rapunzel gets a love letter", tags="ai,animation"), niche) is None


def test_styled_bold_letters_count_as_latin():
    assert non_latin_letters("𝐀𝐈 𝐖𝐚𝐫𝐧𝐢𝐧𝐠") == 0
    assert non_latin_letters("程序员鱼皮") == 5


def test_trend_score():
    assert trend_score(None) == 0.5
    assert trend_score(0.5) == 0.0
    assert abs(trend_score(1.1) - 0.5) < 1e-9
    assert trend_score(2.0) == 1.0
