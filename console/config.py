"""環境変数から実行時の設定を読み、値を検査する。"""

import os
from dataclasses import dataclass

LLM_CHOICES = ("fake", "fake-down", "anthropic")
DEFAULT_LLM = "anthropic"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    db_path: str
    llm: str


def load() -> Config:
    db_path = os.environ.get("CONSOLE_DB_PATH", "")
    if not db_path:
        raise ConfigError("CONSOLE_DB_PATH が設定されていません (会話を記録する SQLite ファイルのパス)")
    llm = os.environ.get("CONSOLE_LLM", DEFAULT_LLM)
    if llm not in LLM_CHOICES:
        raise ConfigError(f"CONSOLE_LLM の値 {llm!r} は使えません (使える値: {', '.join(LLM_CHOICES)})")
    return Config(db_path=db_path, llm=llm)
