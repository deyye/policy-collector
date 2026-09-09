# Git 提交指引（2026-09-09 扩展轮）

本次改动已在 `outputs/policy-collector` 全部完成并测试通过（22 项）。沙箱环境无
`.git` 与 GitHub 凭据，提交需在真实仓库执行。以下命令可直接照做。

## 1. 把改动合入你的真实仓库

若真实仓库在另一台机器/目录，先把 `outputs/policy-collector` 下除以下目录外的内容
同步过去（运行产物不入库）：`data/`、`.venv/`、`.pytest_cache/`、`__pycache__/`、
`.env`。

## 2. 在真实仓库执行

```bash
cd <你的仓库>
git add -A
git status          # 核对：应包含以下文件，不应包含 data/ .env 等
git commit -m "feat: 接通浙江动态列表全量翻页与江苏通知公告，新增省份探测与 .env 模型配置

- 浙江 zjfgw_gsgg：unitbuild 动态列表全量翻页（paramJson pageNo 空页自停），
  真实源验证 387 条/27 页；--limit 15 入库验证，附件 JPaaS 网关下载留证
- 江苏 jsfgw_tzgg：TRS jpage 列表（script 内嵌 recordset/CDATA）通用解析，
  新增 _jsfgw_detail 详情适配器；真实入库 7 条，幂等成立
- collector.ListPageParser 展开 script 内嵌 XML 列表（CDATA）；离线样本
  samples/jiangsu、samples/zj 不依赖网络回归；测试 18→22 全绿
- scripts/probe_sources.py 站点探测框架（html-static/recordset/unitbuild/js-render）
- config 支持根目录 .env 自动加载（不覆盖已有环境变量）+ .env.example
- README/VALIDATION/EXPANSION/PLAN 同步（7 来源、四种列表形态、验收边界）"
git push origin main   # 或你的默认分支
```

首次推送若提示身份，先执行一次：

```bash
git config user.name  "你的名字"
git config user.email "你的邮箱"
```

HTTPS 推送密码处输入 GitHub Personal Access Token（Settings → Developer settings →
Personal access tokens，需 `repo` 权限）。若用 SSH：`git remote set-url origin git@github.com:<你>/<仓库>.git` 后推送。

## 3. 如需沙箱代提交

把以下信息发回对话即可由助手执行（HTTPS 方案）：
- 仓库 URL
- 提交作者名/邮箱
- 推送凭据（Personal Access Token）
