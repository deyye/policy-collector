"""测试全局隔离。

问题背景：有两处**本机配置**会让测试结果依赖开发者环境——

1) `AppConfig.load()` 开头会调用 `_load_dotenv(PROJECT_ROOT/'.env')`，
   把开发者本机的模型地址/模型名/密钥加载进进程环境变量。`LLM_BASE_URL`
   一旦与本机保存的服务地址不同，还会触发「更换服务则清空本机密钥」的分支，
   使「保存密钥后应可读回」这类用例失败。
2) `load_model_settings()` 会读取**本机** `data/llm.local.json`，使
   「未配置密钥时应回退 rule_fallback」的用例在配置过模型的机器上失效。

对策：测试期间既不加载本机 `.env`，也不读取项目默认 data 目录下的模型配置；
而临时目录（各用例自建的 `tmp_path`）下的配置读写保持正常。
"""
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_local_model_settings(monkeypatch):
    """测试不继承本机 .env 与本机 data/llm.local.json，保证环境无关、任何机器上可重复。"""
    from policy_collector import config as config_mod
    from policy_collector import model_settings
    from policy_collector.config import PROJECT_ROOT

    # 1) 只屏蔽**本机** PROJECT_ROOT/.env，其余路径的 .env 加载保持正常
    #    （否则会破坏专门验证 dotenv 解析与优先级的用例）
    real_load_dotenv = config_mod._load_dotenv
    project_env = (Path(PROJECT_ROOT) / '.env').resolve()

    def guarded_load_dotenv(path, *args, **kwargs):
        try:
            if Path(path).resolve() == project_env:
                return None
        except OSError:  # 路径不可解析时不干预
            pass
        return real_load_dotenv(path, *args, **kwargs)

    monkeypatch.setattr(config_mod, '_load_dotenv', guarded_load_dotenv)
    # 清掉可能已被本机 .env 注入的环境变量
    for name in ('LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL', 'DASHSCOPE_API_KEY'):
        monkeypatch.delenv(name, raising=False)

    # 2) 本机 data/llm.local.json 不参与测试
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
