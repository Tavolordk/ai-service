from app.runtime_policy import choose_runtime_budget


def test_summary_uses_small_cpu_friendly_budget():
    budget = choose_runtime_budget(
        intent="summary", mode="auto", thinking=False, max_num_ctx=12288, max_predict=2400
    )
    assert budget.num_ctx == 6144
    assert budget.num_predict == 900


def test_reasoning_gets_more_room_without_using_maximum_by_default():
    budget = choose_runtime_budget(
        intent="summary", mode="auto", thinking=True, max_num_ctx=12288, max_predict=2400
    )
    assert budget.num_ctx == 8192
    assert budget.num_predict == 1100


def test_deep_never_exceeds_configured_caps():
    budget = choose_runtime_budget(
        intent="relations", mode="deep", thinking=True, max_num_ctx=8192, max_predict=1500
    )
    assert budget.num_ctx == 8192
    assert budget.num_predict == 1500
