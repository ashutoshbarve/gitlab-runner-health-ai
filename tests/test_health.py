from app.health import compute_health_score


def test_healthy_online_runner_no_failures():
    assessment = compute_health_score("online", 0, [])
    assert assessment.score == 100
    assert assessment.issues == []
    assert assessment.recommended_action == "No action needed"


def test_offline_runner_penalized_and_flagged():
    assessment = compute_health_score("offline", 0, [])
    assert assessment.score == 50
    assert "Runner offline" in assessment.issues
    assert "gitlab-runner service" in assessment.recommended_action


def test_never_contacted_is_worse_than_stale():
    never = compute_health_score("never_contacted", 0, [])
    stale = compute_health_score("stale", 0, [])
    assert never.score < stale.score


def test_failures_reduce_score_and_are_capped():
    few = compute_health_score("online", 2, [])
    many = compute_health_score("online", 20, [])
    assert few.score == 90
    assert many.score == 60  # penalty capped at 40


def test_failure_reason_drives_recommended_action():
    assessment = compute_health_score("online", 1, ["runner_system_failure"])
    assert "runner host health" in assessment.recommended_action
    assert assessment.confidence == 75


def test_score_never_goes_below_zero():
    assessment = compute_health_score("never_contacted", 50, ["script_failure"])
    assert assessment.score == 0
