"""配置加载：运行参数 + 采集来源 + 分类口径。

优先级：命令行参数 > 环境变量 > config.yaml 默认值。
来源与分类口径 YAML 由 cli 传入路径，默认在项目 config/ 目录。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 项目根 = policy_collector 包上一级
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "config.yaml"
DEFAULT_SOURCES = PROJECT_ROOT / "config" / "sources.yaml"
DEFAULT_CLASSIFY = PROJECT_ROOT / "config" / "classification.yaml"


@dataclass
class LLMConfig:
    enabled: bool = False
    base_url: str = ""
    api_key_env: str = "LLM_API_KEY"
    model: str = ""

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")

    @property
    def effective_model(self) -> str:
        return self.model or os.environ.get("LLM_MODEL", "")


@dataclass
class FetchConfig:
    timeout_seconds: int = 15
    retries: int = 2
    retry_backoff_seconds: float = 2.0
    user_agent: str = "Mozilla/5.0"
    max_page_bytes: int = 20 * 1024 * 1024
    verify_ssl: bool = True


@dataclass
class AppConfig:
    data_dir: Path = PROJECT_ROOT / "data"
    downloads_dir: Path = PROJECT_ROOT / "data" / "downloads"
    db_path: Path = PROJECT_ROOT / "data" / "policy.db"
    llm: LLMConfig = field(default_factory=LLMConfig)
    fetch: FetchConfig = field(default_factory=FetchConfig)
    schedule_interval_minutes: int = 60
    sources: dict[str, "SourceConfig"] = field(default_factory=dict)
    classification: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
        sources_path: str | Path | None = None,
        classify_path: str | Path | None = None,
    ) -> "AppConfig":
        cfg = cls()
        cfg._apply_yaml(Path(config_path) if config_path else DEFAULT_CONFIG)
        # 显式路径只读用户传入；否则不强制存在（demo 模式可零配置）
        if sources_path or Path(DEFAULT_SOURCES).exists():
            cfg.sources = cls._load_sources(Path(sources_path) if sources_path else DEFAULT_SOURCES)
        if classify_path or Path(DEFAULT_CLASSIFY).exists():
            cfg.classification = cls._load_yaml(Path(classify_path) if classify_path else DEFAULT_CLASSIFY)
        # 环境变量覆盖
        if os.environ.get("POLICY_DATA_DIR"):
            cfg.data_dir = Path(os.environ["POLICY_DATA_DIR"])
            cfg.downloads_dir = cfg.data_dir / "downloads"
            cfg.db_path = cfg.data_dir / "policy.db"
        return cfg

    def _apply_yaml(self, path: Path) -> None:
        if not path.exists():
            return
        raw = self._load_yaml(path)
        if raw.get("data_dir"):
            self.data_dir = Path(str(raw["data_dir"]))
            if not self.data_dir.is_absolute():
                self.data_dir = PROJECT_ROOT / self.data_dir
            self.downloads_dir = self.data_dir / "downloads"
            self.db_path = self.data_dir / "policy.db"
        if raw.get("schedule", {}).get("default_interval_minutes"):
            self.schedule_interval_minutes = int(raw["schedule"]["default_interval_minutes"])
        lm = raw.get("llm") or {}
        self.llm = LLMConfig(
            enabled=bool(lm.get("enabled", False)),
            base_url=str(lm.get("base_url", "")),
            api_key_env=str(lm.get("api_key_env", "LLM_API_KEY")),
            model=str(lm.get("model", "")),
        )
        fc = raw.get("fetch") or {}
        self.fetch = FetchConfig(
            timeout_seconds=int(fc.get("timeout_seconds", 15)),
            retries=int(fc.get("retries", 2)),
            retry_backoff_seconds=float(fc.get("retry_backoff_seconds", 2.0)),
            user_agent=str(fc.get("user_agent", "Mozilla/5.0")),
            max_page_bytes=int(fc.get("max_page_bytes", 20 * 1024 * 1024)),
            verify_ssl=bool(fc.get("verify_ssl", True)),
        )

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @staticmethod
    def _load_sources(path: Path) -> dict[str, "SourceConfig"]:
        raw = AppConfig._load_yaml(path)
        out: dict[str, SourceConfig] = {}
        for item in raw.get("sources", []):
            sc = SourceConfig(**{k: v for k, v in item.items() if k in SourceConfig.__dataclass_fields__})
            out[sc.name] = sc
        return out


@dataclass
class SourceConfig:
    """一条采集来源（对应数据库 source_configs 表）。"""

    name: str
    site: str = ""
    region: str = ""
    category: str = ""
    enabled: bool = True
    list_url: str = ""
    link_selector: str = ""
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    max_pages: int = 3
