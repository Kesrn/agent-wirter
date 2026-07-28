"""LLM 设置 API Key 掩码测试。"""

from api.llm_settings import _mask_plain_api_key


def test_api_key_mask_preserves_only_safe_hint():
    api_key = "sk-very-secret-key-value-9abc"

    masked = _mask_plain_api_key(api_key)

    assert masked == "sk-••••••••••••9abc"
    assert "very-secret" not in masked
    assert api_key not in masked


def test_non_standard_key_mask_does_not_invent_prefix():
    assert _mask_plain_api_key("custom-token-1234") == "••••••••••••1234"
    assert _mask_plain_api_key("") is None
    assert _mask_plain_api_key(None) is None
