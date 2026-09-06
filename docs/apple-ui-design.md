# 本地档案 UI 主题规范

## 目标与边界

把“人类线上关系可视化”做成安静、清晰的本地档案工具，参考 Apple HIG 的层级、按钮和自适应布局原则；这是 Windows 浏览器上的网页适配，不是原生 Apple 组件或认证产品。主题层只改展示，不改数据库、消息文件、同步、备份、云端调用或逐次授权规则。

## 设计方案

- 信息结构：固定品牌工具栏 → 会话/分析/媒体/同步/回收站/设置导航 → 页面标题 → 主要内容与分组操作。标题直接描述功能。
- 色彩：浅色使用 #f5f5f7 底色、白色内容面板、#1d1d1f 正文；深色使用 #1c1c1e 底色、#2c2c2e 面板。链接、主按钮、成功、警告、危险分别有语义色，不仅用颜色表达状态。
- 排版：本机系统字体，不下载字体或 Apple 资源；正文 15px、辅助文字至少 13px、控件至少 14px（小屏 16px），数字使用等宽数字而非整页等宽字体。
- 控件：主操作实心蓝，次操作中性底，文本操作低强调，危险操作独立红色；最小点击高度 44px。开关轨道 46×28px，整行标签可点击。
- 形状：面板圆角 18px、按钮与表单 10px；边框和投影克制。侧栏与工具栏可轻微透光，内容面板保持实底。
- 自适应：桌面侧栏，小屏三列导航；表格在容器内横向滚动；抽屉支持内部滚动、Escape 和焦点恢复。
- 无障碍：可见键盘焦点、深色模式、增强对比、减少动态效果与强制系统色支持。重要文字对比目标至少 4.5:1。

## 设计自检

不使用假 macOS 窗口红黄绿按钮，不把所有面板做成玻璃，不以模糊/渐变替代信息层级；优先让检索、范围确认、进度、恢复和危险操作易于辨认。图标采用原创简单线条，保留文字标签。警告、外发确认和不完整分析范围不会被视觉弱化。

## 实现约定

`dashboard/static/apple-ui.css` 最后加载：集中定义语义 token，并映射已有变量，以保持业务模块和图表兼容。功能 CSS 保留结构规则，主题层负责跨模块的视觉一致性。新增模块应使用主题变量和现有按钮类，避免新增写死的白底与低对比文字。

独立语音页通过 `voice-gallery.css` 引入共享主题，并单独维护语音布局；不需要重写已导出的私人 HTML。既有独立图片报告使用自己的内嵌样式与严格 CSP，本次保留原样，不改写归档原件。手机导航不重复显示计数，完整数量仍在会话概览和结果栏显示。

验收只用合成数据与随机本机端口；不打开生产聊天页面，不上传真实消息、不触发模型调用。检查浅/深色、320/390/768/1024/1440 宽度、放大文字、键盘、授权默认值以及原有功能回归。

## 参考

- [Apple HIG · Buttons](https://developer.apple.com/design/human-interface-guidelines/buttons)
- [Apple HIG · Typography](https://developer.apple.com/design/human-interface-guidelines/typography)
- [Apple HIG · Color](https://developer.apple.com/design/human-interface-guidelines/color)
- [Apple HIG · Layout](https://developer.apple.com/design/human-interface-guidelines/layout)
- [Apple HIG · Materials](https://developer.apple.com/design/human-interface-guidelines/materials)
- [Apple HIG · Accessibility](https://developer.apple.com/design/human-interface-guidelines/accessibility)

平台原生尺寸单位和系统颜色 API 不直接等同于网页；本项目采用自己的 CSS token 和舒适的 44px 网页点击区域。

## 可复现验收

测试夹具覆盖主题、独立语音页、筛选/分页、音频懒加载和安全边界。窄屏使用完全虚构的聚合数据，检查页面横向溢出、文字放大、浅/深色和键盘交互。具体通过/跳过数量以当前提交的实际测试结果为准，不把旧部署的验收记录作为新版本已通过的证明。

复验命令（在空间充足的非 C 盘项目目录运行）：

```powershell
$env:TEMP = Join-Path (Get-Location) 'scripts\tmp'
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
$env:TMP = $env:TEMP
$env:SHE_LOVE_ME_UI_TESTS = '1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
node --test tests\test_catalog.cjs
```
