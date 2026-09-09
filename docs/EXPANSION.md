# 站点接入记录（EXPANSION）

> v0.4 更新：福建、陕西、湖南已增加适配；浙江与江苏能力保留。本文以下保留历史记录，最新状态见 [v0.4 验证说明](VALIDATION-v0.4.md)。


本文件记录真实接通与探测状态的站点。接入以「真实采集验证」为准，不能以「配置了网址」代替「已采集成功」。

## 浙江动态列表已接通（2026-09-09）

来源：`zjfgw_gsgg`（浙江省发展改革委 · 行政规范性文件栏目）

### 站点机制

浙江该栏目列表**不在 HTML 中**，而是 hanweb「页面构建单元」动态加载：

1. 栏目页 `<script ... AuthorizedRead/unitbuild.js url="…" queryData="…">` 声明构建参数（webId/tplSetId/pageId/tagId 等，单引号 JSON）；
2. 前端据此 `GET` 同域 JPaaS 单元接口 `/api-gateway/jpaas-publish-server/front/page/build/unit`；
3. 响应 `{"data":{"html":"<ul>…政策列表…</ul>"}}`，即列表正文。

实现（`policy_collector/collector.py`，`list_format: zj_unit`）：
- `extract_unitbuild_spec(page_html)`：从栏目页提取接口 URL 与 queryData 参数（**随页面动态读取，不硬编码单元 ID**）；
- `unit_list_links(list_html, src, base_url)`：解析 `data.html` 详情链接，标题优先取 `<a title>` 完整属性；
- `discover_zj_unit_links(...)`：两跳组合 + **全量翻页**（`paramJson={"pageNo":N,"pageSize":15}` 逐页递增，空页自动停止），接口 JSON 原件逐页落盘留证。

### 实测结果（2026-09-09，独立数据目录，规则模式）

- **全量翻页验证**：请求 27 页后空页自停（26 页有数据），去重详情链接 **387 条**，耗时约 14s（`scripts/zj_live_check.py` 复跑）；
- 时间覆盖 2026-06 新近至 2020-01-07 发布（最早成文 2019-12-31，文号 浙发改能源〔2019〕532号）；
- 新旧 art 路径并存：新 `/col/col1229123351/art/YYYY/art_<32位hex>.html` 6 条 + 旧 `/art/YYYY/M/D/art_<栏目id>_<文章id>.html` 381 条，均被 `include` 与 `detail_url_pattern` 覆盖；
- **真实入库**（`--limit 15`）：15 篇全部入库待复核，文号/发布机构/日期/正文元数据正确；附件经 JPaaS 下载网关下载 11 个成功、2 个网关限流失败（补采可自愈）、WPS/旧 DOC 6 个保留原件待解析；
- 分类样例命中准确：招投标监管指导意见→准入/激励约束、新能源上网电价改革→激励约束/准入、设备更新以旧换新方案→引导/激励/保障等。

### 边界

- 网关 `fileUrl` 带时效，短时间高频轮询触发限流属正常；`schedule` 间隔场景失败可自愈。
- 浙江正文分「印发通知（短正文）+ 附件全文」：实施类政策全文在附件，须靠附件解析才能分类。

## 江苏通知公告已接通（2026-09-09）

来源：`jsfgw_tzgg`（江苏省发展改革委 · 通知公告栏目，col284）

### 站点机制

江苏为 hanweb/TRS jpage 站点：列表第一页由服务端以 `<script>` 内嵌
`<datastore><recordset><record><![CDATA[<li><a…>…]]>` XML 序列化输出（30 条）；
翻页走 `/module/web/jpage/dataproxy.jsp`（POST、返回 XML、页面含多个 jpage 单元）。

对项目的影响：
- `ListPageParser` 增加通用能力：**展开 script 内嵌 recordset 记录**（剥 CDATA 标记后并入 `<a>` 解析）——对其它 TRS jpage 站点同样生效；
- 详情页正文在 `div.TRS_Editor`（与 NDRC 同容器）；新增 `site_adapters._jsfgw_detail` 清洗 `<title>` 的「站点名+栏目名」前缀。

### 实测结果（2026-09-09，独立数据目录，规则模式）

- 列表发现 30 条（最新公告，含成品油调价、招聘公示等混合内容），`detail_url_pattern` 精确锁定 `art_284_` 栏目码；
- 真实入库两批 7 条待复核，重复运行无重复 policy_key（幂等成立）；
- 规则正确排除 3 条非政策内容；成品油价格调整公告等进入待复核待人工判定——**「通知公告」栏目噪音较高，人工复核负担大于规范性文件栏目**，如需投资政策建议后续改接江苏「行政规范性文件」目录（信息公开板块为 JS 渲染，需另行适配）。

### 边界

- 历史翻页（dataproxy POST/XML、双 jpage 单元）未接入，`max_pages=1` 只取最新 30 条，对增量采集够用；
- 站点详情页无 h1，标题取清洗后的 `<title>`。

## 探测结论：暂未接入的站点（2026-09-09）

用 `scripts/probe_sources.py` 只读探测以下省级站点，结论如实记录：

| 站点 | 现象 | 结论 |
|---|---|---|
| 安徽 fzggw.ah.gov.cn | 栏目列表为前端模板渲染（Epoint，`<?=el.link?>` 占位 + JS），信息公开目录 `/public/column/7011` 需走其数据接口 | 待适配其 public-api |
| 江西 drc.jiangxi.gov.cn | hanweb col 体系但栏目页 0 静态链接（正文区 JS 拉取）；首页可达 | 待定位其列表数据接口 |
| 陕西 sndrc.shaanxi.gov.cn | 首页可达但疑似 WAF/反爬（导航外链异常） | 需人工核实真实栏目地址 |
| 福建/湖南/广东/山东/四川/湖北/河南（探测域名） | 连接失败或域名变更 | 需人工确认真实官网域名 |
| 江苏信息公开 col50963 | 1.5KB 空壳，内容 JS 渲染 | 待适配，找到规范性文件目录 |

> 经验：省级发改委官网普遍采用「列表不在静态 HTML」的 JS/动态构建，静态可直抓的站点越来越少；接入成本排序约为 html-static < recordset(TRS jpage) < unitbuild(hanweb JPaaS) < Epoint/自研 JS 数据接口。`probe_sources.py` 判定类别后即可对号入座选择 `sources.yaml` 配置形态或适配器。

## 规划中

| 站点 | 状态 | 说明 |
|---|---|---|
| 浙江无障碍副本等其它栏目 | 规划 | 视业务需要扩展 |
| 江苏历史翻页（dataproxy POST） | 规划 | 需实现 POST/XML/多单元解析 |
| 江苏/浙江等「行政规范性文件」专用栏目 | 规划 | 业务价值高于通知公告，需定位入口 |
| 安徽 Epoint、江西列表数据接口 | 规划 | 见上表 |
