"""Матрица правил автостопа (gpu_raid/lifecycle_rules.py — чистый модуль)."""

from gpu_raid import lifecycle_rules as rules

NOW = 1_000_000.0


def _view(**over):
    base = {
        "kind": "cloud", "pinned": False, "state": "online", "busy": False,
        "has_worked": True, "idle_s": 0.0, "session_age_min": 5.0,
        "keep_alive_until": 0,
    }
    base.update(over)
    return base


def _cfg(**over):
    base = {"policy": "eco", "idle_stop_min": 10, "budget_min": 0}
    base.update(over)
    return base


def test_non_cloud_never_stopped():
    for kind in ("local", "lan"):
        d, _ = rules.decide(_cfg(policy="instant"), _view(kind=kind, idle_s=9999), NOW)
        assert d == rules.NONE


def test_pinned_never_stopped():
    d, _ = rules.decide(_cfg(policy="local_only"), _view(pinned=True), NOW)
    assert d == rules.NONE


def test_offline_and_stopped_ignored():
    for state in ("offline", "stopped", "unknown", None):
        d, _ = rules.decide(_cfg(policy="instant"), _view(state=state, idle_s=9999), NOW)
        assert d == rules.NONE


def test_busy_never_stopped():
    d, _ = rules.decide(_cfg(policy="instant"), _view(busy=True, idle_s=9999), NOW)
    assert d == rules.NONE


def test_keep_policy():
    d, _ = rules.decide(_cfg(policy="keep"), _view(idle_s=999999), NOW)
    assert d == rules.NONE


def test_eco_before_and_after_threshold():
    cfg = _cfg(policy="eco", idle_stop_min=10)
    d, _ = rules.decide(cfg, _view(idle_s=9 * 60), NOW)
    assert d == rules.NONE
    d, reason = rules.decide(cfg, _view(idle_s=10 * 60), NOW)
    assert d == rules.STOP
    assert "простой" in reason


def test_eco_unknown_idle_not_stopped():
    d, _ = rules.decide(_cfg(policy="eco"), _view(idle_s=None), NOW)
    assert d == rules.NONE


def test_instant_needs_work_and_grace():
    cfg = _cfg(policy="instant")
    # воркер поднят, но ещё ничего не делал — не трогаем (ждёт первого задания)
    d, _ = rules.decide(cfg, _view(has_worked=False, idle_s=9999), NOW)
    assert d == rules.NONE
    # только что закончил юнит — grace-период
    d, _ = rules.decide(cfg, _view(idle_s=5), NOW)
    assert d == rules.NONE
    d, _ = rules.decide(cfg, _view(idle_s=rules.INSTANT_GRACE_S), NOW)
    assert d == rules.STOP


def test_local_only_stops_cloud():
    d, reason = rules.decide(_cfg(policy="local_only"), _view(idle_s=None, has_worked=False), NOW)
    assert d == rules.STOP
    assert "локально" in reason.lower()


def test_budget_overrides_keep():
    cfg = _cfg(policy="keep", budget_min=60)
    d, reason = rules.decide(cfg, _view(session_age_min=61), NOW)
    assert d == rules.STOP
    assert "бюджет" in reason.lower()
    d, _ = rules.decide(cfg, _view(session_age_min=59), NOW)
    assert d == rules.NONE


def test_keep_alive_until_defers_stop():
    cfg = _cfg(policy="instant")
    d, _ = rules.decide(cfg, _view(idle_s=9999, keep_alive_until=NOW + 60), NOW)
    assert d == rules.NONE
    d, _ = rules.decide(cfg, _view(idle_s=9999, keep_alive_until=NOW - 1), NOW)
    assert d == rules.STOP


def test_keep_alive_does_not_block_budget():
    cfg = _cfg(policy="keep", budget_min=30)
    d, _ = rules.decide(cfg, _view(session_age_min=31, keep_alive_until=NOW + 600), NOW)
    assert d == rules.STOP


def test_unknown_policy_behaves_like_eco():
    d, _ = rules.decide(_cfg(policy="wat", idle_stop_min=1), _view(idle_s=61), NOW)
    assert d == rules.STOP


def test_is_cloud_allowed():
    assert rules.is_cloud_allowed("eco")
    assert rules.is_cloud_allowed("keep")
    assert not rules.is_cloud_allowed("local_only")
