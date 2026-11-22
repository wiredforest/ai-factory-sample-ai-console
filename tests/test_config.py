import pytest

from console import config


@pytest.mark.parametrize("llm", ["fake", "fake-down", "anthropic"])
def test_accepts_each_llm_choice(monkeypatch, llm):
    monkeypatch.setenv("CONSOLE_LLM", llm)
    assert config.load().llm == llm


def test_llm_defaults_to_anthropic(monkeypatch):
    monkeypatch.delenv("CONSOLE_LLM")
    assert config.load().llm == "anthropic"
