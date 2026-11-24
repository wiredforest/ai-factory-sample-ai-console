"""環境変数から実行時の設定を読み、値を検査する。"""

import os
from dataclasses import dataclass

LLM_CHOICES = ("fake", "fake-down", "anthropic")
DEFAULT_LLM = "anthropic"
DEFAULT_MODEL = "claude-haiku-5-5"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    db_path: str
    llm: str
    model: str


def load() -> Config:
    db_path = os.environ.get("CONSOLE_DB_PATH", "")
    if not db_path:
        raise ConfigError("CONSOLE_DB_PATH が設定されていません (会話を記録する SQLite ファイルのパス)")
    llm = os.environ.get("CONSOLE_LLM", DEFAULT_LLM)
    if llm not in LLM_CHOICES:
        raise ConfigError(f"CONSOLE_LLM の値 {llm!r} は使えません (使える値: {', '.join(LLM_CHOICES)})")
    # CONSOLE_MODEL: 本物の AI (CONSOLE_LLM=anthropic) に送るモデル。変えたらアプリを再起動して反映する。
    #   claude-haiku-5-5  (未設定・空のとき) 速い・普段の相談に十分・料金はいちばん安い。検索は基本の版 web_search_20250305
    #   claude-sonnet-5-5  Haiku より遅いが賢い・料金は中くらい。検索は結果を絞り込む版 web_search_20260209
    #   claude-opus-5-5    いちばん遅いがいちばん賢い・料金はいちばん高い。難しい相談向け。検索は絞り込む版 web_search_20260209
    # 名前の妥当さはここでは検査しない (どの値でも起動する)。偽の応答役 (fake・fake-down) はこの値を使わない。
    model = os.environ.get("CONSOLE_MODEL", "") or DEFAULT_MODEL
    return Config(db_path=db_path, llm=llm, model=model)
