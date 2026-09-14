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


@pytest.fixture(autouse=True)
def _isolate_project_dotenv(monkeypatch):
    """测试不继承**项目根**的 `.env`（本机密钥/服务地址/模型名）。

    问题背景：要用模型就必须配 `.env`，而它一旦存在就会污染测试：
    `.env` 里的 `LLM_BASE_URL` 会覆盖本机配置，进而触发
    「更换服务地址即清空已保存密钥」的逻辑，把用例刚保存的测试密钥抹掉。
    实测：配好 `.env` 后 89 项里 3 项转红——
      test_agent_flow::test_actual_local_compatible_api_without_key
      test_expansion::test_model_page_does_not_echo_key_and_save_works
      test_regressions::test_model_http_contract_and_truncated_response
    表现是 `cfg.llm.api_key` 从 `'test-secret'` 变成 `''`。

    对策：只屏蔽项目根那一个 `.env` 文件，**不禁用 `_load_dotenv` 本身**——
    否则专门验证 .env 解析与优先级规则的用例会一起挂。
    """
    from policy_collector import config as config_mod

    real_load = config_mod._load_dotenv
    project_env = (Path(config_mod.PROJECT_ROOT) / '.env').resolve()

    def guarded(path):
        try:
            if Path(path).resolve() == project_env:
                return None
        except OSError:  # 路径不可解析时不干预
            pass
        return real_load(path)

    monkeypatch.setattr(config_mod, '_load_dotenv', guarded)
    # .env 用 setdefault 注入，一旦进过进程环境就会残留，逐用例清掉
    for name in ('LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL', 'DASHSCOPE_API_KEY'):
        monkeypatch.delenv(name, raising=False)
