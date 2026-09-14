# Chart Probe Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for bounded components and independent review. Daily decisions are delegated by the maintainer; no additional scope approval is needed.

**Goal:** 现有图表随鼠标或触摸吸附显示准确统计值，并可用键盘读取。

**Architecture:** 无依赖共享 ChartProbe 负责坐标、近邻、浮层与生命周期，各图表只提供安全的数值点。私人部署仅应用本轮源码差异，公开仓库承担版本和合成测试。

**Tech Stack:** 原生 JS / SVG / CSS，Python unittest + Playwright，Node 内置测试。

## 1. 共享探针（主代理）

- [x] 新建 `tests/test_chart_probe_ui.py`，合成 SVG 上先断言 `window.ChartProbe` 存在，移动到两个点之间验证最近值；运行并记录预期失败。
- [x] 新建 `dashboard/static/chart-probe.js`、`chart-probe.css`；在 `index.html` 引入。以 `getScreenCTM().inverse()` 转换指针坐标，二分查最近横坐标；每次渲染只更新当前标签、标记和受容器限制的数据卡。

```js
ChartProbe.attach(svg, {
  plot: {left: 45, right: 855, top: 35, bottom: 240},
  points: [{x: 45, label: '2026-01-01', rows: [
    {label: '你', value: '2 条', tone: 'me'},
    {label: '对方 / 其他成员', value: '3 条', tone: 'other'}
  ], markers: [{y: 100, tone: 'me'}, {y: 190, tone: 'other'}]}]
});
```

- [x] 加入 `detach`、方向键与 Home、End、Escape、触屏轻点与纵向滚动、HTML 横条 `attachRows`；复用 tooltip DOM，切换数据先销毁。
- [x] 运行 `python -m unittest tests.test_chart_probe_ui`（设置 `SHE_LOVE_ME_UI_TESTS=1`）；预期鼠标、键盘、触屏、生命周期、XSS、缩放和边界全通过。

## 2. 会话图表（独立实现代理）

- [x] 新建 `tests/test_chart_probe_dashboard.py`，用已有合成 fixture 验证日轨迹、小时图和横条缺少探针；先运行见红。
- [x] 只改 `dashboard/static/app.js`，重绘前 `ChartProbe.detach(svg)`；绘制后提供日期、双方、合计与真实 marker 坐标。小时图提供24个中心点，比例横条用 attachRows。不要修改 API、统计或省略零值。
- [x] 运行新增测试和 `tests.test_dashboard_ui`，预期无新增请求和无会话 Escape 回归。

## 3. AI 统计图（独立实现代理）

- [x] 新建 `tests/test_chart_probe_analysis.py`，直接传完全合成 metrics，验证日期/小时/星期/类型/份额/间隔精确值与零数据；先运行见红。
- [x] 只改 `dashboard/static/analysis-charts.js`，柱图传真实中心点和完整日期／月份，不用截短轴标签充当卡片标题。HTML 横条共用 attachRows，原有表格保留。
- [x] 运行新增测试与 `tests.test_analysis_ui`；不得引入云端调用或分析确认默认选中。

## 4. 审查、部署和源码交付（主代理）

- [x] 先独立核验规格，再独立检查生命周期、边界与可访问性；任何缺陷加失败回归后修复。
- [x] 全部合成回归：`python -m unittest discover -s tests -p 'test_*.py'`、`node --test tests/test_catalog.cjs`、JS语法；测试临时目录设到项目 scripts/tmp。
- [ ] 更新 `VERSION`、`CHANGELOG.md`、`BACKLOG.md` 和 `docs/reviews/v0.1.5-review.md` 为实际候选验收状态，逐文件审阅暂存、运行 `scripts/check_public_release.py --root .`，提交后独立归档树再扫描。
- [x] 私人部署保存本轮源文件回滚副本，以 apply_patch 加入新资源和精确修改的 renderer、入口；不要拷贝整个 public app.js、CSS 或 index 覆盖既有差异。合成夹具复验私人前端，静态路由回读，无重启、无读取真实会话。
- [ ] 上传已审查的纯源码分支并回读树，建立关联 Issue/PR；对应 CI 通过才合并发布，发布工具受阻则明确保留候选状态。
