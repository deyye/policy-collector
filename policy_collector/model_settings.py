"""本机模型配置；只保存到被忽略的数据目录，不进入源码。"""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

PROVIDERS = {
    'dashscope': {'base_url':'https://dashscope.aliyuncs.com/compatible-mode/v1','model':'qwen-plus','api_key_env':'DASHSCOPE_API_KEY'},
    'custom': {'base_url':'','model':'','api_key_env':'LLM_API_KEY'},
}

def settings_path(cfg):
    return cfg.data_dir / 'llm.local.json'

def load_model_settings(cfg):
    path=settings_path(cfg)
    if path.exists():
        data=json.loads(path.read_text(encoding='utf-8'))
        for name in ('base_url','model','api_key_env','enabled','enable_thinking'):
            if name in data:setattr(cfg.llm,name,data[name])
        cfg.llm.local_api_key=str(data.get('api_key',''))
    override = os.environ.get('LLM_BASE_URL')
    if override and override.rstrip('/') != cfg.llm.base_url.rstrip('/'):
        # 更换服务时不把旧服务的本机密钥、专用参数带过去。
        cfg.llm.local_api_key = ''
        cfg.llm.enable_thinking = None
    cfg.llm.base_url=override or cfg.llm.base_url
    cfg.llm.model=os.environ.get('LLM_MODEL') or cfg.llm.model
    return cfg

def save_model_settings(cfg, provider='dashscope', base_url='', model='', api_key='', api_key_env=''):
    if provider not in PROVIDERS:raise ValueError('未知模型服务类型')
    preset=PROVIDERS[provider]
    base_url=(base_url or preset['base_url']).strip().rstrip('/')
    model=(model or preset['model']).strip()
    p=urlsplit(base_url)
    if p.scheme not in ('https','http') or not p.hostname or p.username or p.password or p.query or p.fragment or '{' in base_url:
        raise ValueError('请输入有效的模型 Base URL，不含密钥、占位符或查询参数')
    if p.scheme=='http' and p.hostname not in ('localhost','127.0.0.1','::1'):
        raise ValueError('远程模型请使用 HTTPS；本机服务允许 HTTP')
    if not model:raise ValueError('必须填写模型名称')
    path=settings_path(cfg)
    previous=json.loads(path.read_text()) if path.exists() else {}
    if not api_key and previous.get('api_key'):
        if previous.get('base_url') != base_url:
            raise ValueError('更换服务地址时请重新填写密钥，避免向新服务发送旧密钥')
        api_key=previous['api_key']
    data={'enabled':True,'provider':provider,'base_url':base_url,'model':model,
          'api_key_env':api_key_env or preset['api_key_env'],'api_key':api_key,
          'enable_thinking':False if provider=='dashscope' else None}
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(data,f,ensure_ascii=False,indent=2)
    os.replace(temp,path)
    path.chmod(0o600)
    load_model_settings(cfg)
    return path

def connection_check(cfg):
    from .llm_client import LLMClient
    client=LLMClient(cfg.llm)
    if not client.available:return {'ok':False,'error':'未配置实际 API Key 或模型名称；请在本机配置','model':cfg.llm.effective_model}
    result=client.chat_json('只输出JSON。','连接检查：请输出 {"ok":true}。')
    return {'ok':bool(result and result.get('ok') is True),'error':client.last_error or ('' if result and result.get('ok') is True else '模型未返回预期JSON'),
            'model':cfg.llm.effective_model,'usage':client.usage}
