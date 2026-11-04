from security.policy import DEFAULT_PERMISSION, TOOL_PERMISSIONS


def test_day_status_is_explicitly_read_only():
    assert TOOL_PERMISSIONS["day_status"] == "read_only"


def test_log_estimated_meal_is_explicitly_write():
    assert TOOL_PERMISSIONS["log_estimated_meal"] == "write"


def test_log_meal_unchanged():
    assert TOOL_PERMISSIONS["log_meal"] == "write"
    assert DEFAULT_PERMISSION == "write"
