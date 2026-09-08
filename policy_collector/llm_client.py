"""LLM 客户端（OpenAI 兼容 Chat Completions 接口）。

- base_url / api_key / model 由 config 或环境变量提供（config.yaml 不写明文）。
- 统一输出 JSON，调用方负责校验字段；模型自报置信度仅作参考，不作"已确认"。
"""
from __future__ import annotations

import json
from typing import Any, Optional

import requests

from .config import LLMConfig


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.base_url = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")

    @property
    def available(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.api_key and self.cfg.effective_model)

    def chat_json(self, system: str, user: str, temperature: float = 0.0) -> Optional[dict[str, Any]]:
        """请求模型返回 JSON 对象；失败/超时返回 None（由调用方回退规则分类）。"""
        if not self.available:
            return None
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.cfg.api_key}"},
                json={
                    "model": self.cfg.effective_model,
                    "temperature": temperature,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                timeout=60,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return json.loads(content)
        except Exception:  # noqa: BLE001
            return None
