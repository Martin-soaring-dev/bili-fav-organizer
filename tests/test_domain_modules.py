from bili_fav.domain.versions import version_key, is_newer_version
from bili_fav.domain.llm_settings import thinking_params
from bili_fav.security.redaction import mask_secret
import pytest


def test_versions():
    assert version_key("v0.209.1") == (0, 209, 1)
    assert version_key("0.209-beta") is None
    assert is_newer_version("v0.210", "v0.209.9")
    assert not is_newer_version("v0.209.0", "0.209")
    with pytest.raises(ValueError):
        is_newer_version("broken", "v0.209")


def test_llm_settings():
    assert thinking_params("auto") == {}
    assert thinking_params("off") == {"enable_thinking": False}
    assert thinking_params("xhigh") == {"enable_thinking": True, "reasoning_effort": "max"}


def test_masking():
    assert mask_secret("") == ""
    assert mask_secret("abc") == "****"
    assert mask_secret("abcdefgh") == "****efgh"
