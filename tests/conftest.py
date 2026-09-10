"""测试全局隔离。

问题背景：`AppConfig.load()` 末尾会调用 `model_settings.load_model_settings()`，
读取**本机** `data/llm.local.json`（开发者配置的真实模型密钥/地址/模型名）。
这会让测试结果依赖开发者本机状态——例如「未配置密钥时应回退 rule_fallback」的用例，
在配置过模型的机器上会因继承本机密钥而走真实 LLM 失败。

对策：测试期间把**项目默认 data 目录下**的模型配置视为不存在，
而临时目录（各用例自建的 `tmp_path`）下的配置读写保持正常，
从而既保证测试环境无关，又不影响模型配置用例的读写往返验证。
"""
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_local_model_settings(monkeypatch):
    """测试不继承本机 data/llm.local.json，保证环境无关、任何机器上可重复。"""
    from policy_collector import model_settings
    from policy_collector.config import PROJECT_ROOT

    real_settings_path = model_settings.settings_path
    project_settings = (Path(PROJECT_ROOT) / 'data' / 'llm.local.json').resolve()
    sentinel = Path('/nonexistent/policy-collector-test-no-local-llm.json')

    def guarded(cfg):
        try:
            if Path(real_settings_path(cfg)).resolve() == project_settings:
                return sentinel
        except OSError:  # 路径不可解析时不干预
            pass
        return real_settings_path(cfg)

    monkeypatch.setattr(model_settings, 'settings_path', guarded)
