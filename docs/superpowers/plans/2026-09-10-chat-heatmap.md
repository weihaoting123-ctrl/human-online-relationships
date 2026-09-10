# Chat Heatmap Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for independently owned units, with spec then quality review. User delegates routine decisions.

**Goal:** 单会话本机日历热度与星期/小时分布。
**Architecture:** 纯统计模块 + 受保护只读接口 + 懒加载独立 UI；沿用详情生命周期。
**Tech Stack:** Python stdlib, vanilla JS/CSS, unittest, Playwright 合成夹具。

## 1. 聚合与接口

- [x] 新建 `tests/test_chat_heatmap.py`：先断言 `dashboard.chat_heatmap` 可发现，再验证规范的计数和边界；运行 `.venv/Scripts/python.exe -m unittest discover -s tests -p test_chat_heatmap.py`，观察缺失功能失败。
- [x] 新建 `dashboard/chat_heatmap.py`：实现 `build_chat_heatmap(payload, scope, now=None)`，精确采用设计的 DTO 和日期/排除规则。
- [x] 新建 `tests/test_chat_heatmap_api.py`：随机端口合成档案，POST 成功/403/隐藏/400及源文件不变；先确认路由 404 失败。
- [x] 在 `dashboard/app.py` 接入只读 POST `/api/activity/heatmap`，先 `require_visible`，再用 `scoped_ai._source` 读取，严格 JSON 字段白名单；返回 `{"status":"ok","heatmap": build_chat_heatmap(payload, scope)}`。
- [x] 运行两个测试文件及原时间线测试，核对输出守恒 `sum(days.count) == sum(map(sum, weekday_hour)) == summary.message_count`。

## 2. 页面

- [x] 新建 `tests/test_chat_heatmap_ui.py`，用既有静态合成夹具；断言详情中 `#chat-heatmap` 存在、展开时才只读 POST，先观察缺少元素失败。
- [x] 新建 `dashboard/static/chat-heatmap.js` / `.css`：对外只提供 `ChatHeatmap.mount(host, bundleId)` 和 `clear(host)`；使用核心 `api`/`el`，内部 WeakMap 管理 AbortController 与递增版本。
- [x] 在 `index.html` 增加 host 和脚本/样式；在 `app.js` 渲染会话后 mount，旧会话清理点 clear。
- [x] 月历用 CSS grid 7 列，日期+数量直接可读；时段用语义 table，七行二十四列，局部横向滚动；按设计输出摘要与最活跃日清单。
- [x] 补充日期修改、超限/格式错误、重置、空数据、请求失败/恢复、延迟旧会话结果、键盘及各尺寸/深色/减少动态效果测试；运行 `SHE_LOVE_ME_UI_TESTS=1` 的定向 unittest。

## 3. 收尾

- [x] 独立检查设计符合性，再代码质量/隐私边界；有问题先补回归并修复。
- [x] 更新产品文档、BACKLOG、CHANGELOG、VERSION 及 review 证据。功能隔离进入 v0.1.3，不宣称候选 v0.2.0 整体完成。
- [ ] 全部 unittest + Node 测试，审查合成截图；公开检查器扫描暂存与独立发布树。
- [ ] 上传审阅的源码分支、PR，经 CI 后发布；回读远端 SHA/版本，明确公开源码与私人部署的区别。
