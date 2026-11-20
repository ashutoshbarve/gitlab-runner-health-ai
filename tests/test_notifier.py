from app.notifier import _adaptive_card, _fact


def test_adaptive_card_wraps_in_message_attachment_envelope():
    card_message = _adaptive_card("Test Title", [_fact("Key", "Value")])

    assert card_message["type"] == "message"
    attachment = card_message["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert attachment["content"]["type"] == "AdaptiveCard"


def test_adaptive_card_body_includes_title_and_facts():
    card_message = _adaptive_card("My Alert", [_fact("Status", "offline"), _fact("Score", 42)])
    content = card_message["attachments"][0]["content"]

    title_block = content["body"][0]
    assert title_block["type"] == "TextBlock"
    assert title_block["text"] == "My Alert"

    fact_set = content["body"][1]
    assert fact_set["type"] == "FactSet"
    assert fact_set["facts"] == [{"title": "Status", "value": "offline"}, {"title": "Score", "value": "42"}]


def test_adaptive_card_adds_open_url_action_when_given():
    card_message = _adaptive_card("Alert", [], open_url="https://gitlab.example.com/jobs/1")
    content = card_message["attachments"][0]["content"]

    assert content["actions"] == [
        {"type": "Action.OpenUrl", "title": "View in GitLab", "url": "https://gitlab.example.com/jobs/1"}
    ]


def test_adaptive_card_omits_actions_when_no_url():
    card_message = _adaptive_card("Alert", [])
    content = card_message["attachments"][0]["content"]

    assert "actions" not in content


def test_adaptive_card_requests_full_width_in_teams():
    card_message = _adaptive_card("Alert", [])
    content = card_message["attachments"][0]["content"]

    assert content["msteams"] == {"width": "Full"}


def test_fact_handles_none_value():
    assert _fact("Issues", None) == {"title": "Issues", "value": ""}
