# 项目需求池

需求池修订：R3 · 2026-09-07。初始审查基线 `94180d0` / 软件版本 `0.1.0`。R2 记录首轮实施与验收，R3 选择 DATA-01 第一阶段；需求文档修订不是软件 Release。

目标：把本机聊天档案变成可检索、可回顾、范围明确的线上关系时间线。原始数据只读、云分析按次确认；不推断真实动机、不绘制未经确认的人际身份网络。

[项目解构](docs/project-decomposition.md) · [既有路线图](ROADMAP.md) · [GitHub 待办](https://github.com/weihaoting123-ctrl/human-online-relationships/issues)

## 使用规则

- 稳定 ID 不随标题或版本变化；一个需求只保留一个主 Issue，拆分任务引用父需求，不重复创建。
- 状态依次为：候选 → 已确认 → 实现中 → 待验收 → 已发布；暂缓需说明依赖，不能删掉历史假装从未计划。
- A 批 UX-01 至 UX-05 已发布为 v0.1.1；DATA-01 第一阶段选定为 v0.1.2，其余新增项仍为候选。已有 #7–#10 保持开放及原里程碑；以下版本建议未自动移动它们。
- 优先级沿用 CONTRIBUTING：P1 是核心阻断/数据保护门槛；P2 是常规缺陷与迭代。没有绕行方式的程度未经验证时，不随意标为 P1。
- 验收使用完全虚构材料。没有私有数据、真实凭证或真实微信也应能验证通用功能。
- 每次交付更新“实际版本 / PR / 测试证据”，不能把候选版本当作完成记录。
- 按 [项目托管规范](docs/project-stewardship.md)，后续常规范围确认由受托代理逐批判断并记录，不再重复要求维护者选择技术方案；仍保留数据授权、实机验收与发布门槛。

## A：先修好首次使用闭环（v0.1.1，已发布）

[设计规范](docs/superpowers/specs/2026-09-06-v011-first-use-design.md) · [测试与发布计划](docs/superpowers/plans/2026-09-06-v011-first-use.md) · [审查记录](docs/reviews/v0.1.1-review.md) · [迭代里程碑](https://github.com/weihaoting123-ctrl/human-online-relationships/milestone/3)

| ID | 需求 / 优先级 | 完成条件 | 非目标与依赖 |
| --- | --- | --- | --- |
| UX-01 | 文件来源状态一致 / P2 | 文件选择和拖放均覆盖 Markdown→JSON、JSON→Markdown；自动判断与手动来源选择有明确规则；失败保留输入，不自动重试 | 不改源格式和归档身份；已合成复现 |
| UX-02 | 导入结果可达 / P2 | 导入成功明确呈现结果，并能进入正确会话；切换工作区期间的晚到结果不得覆盖另一会话 | 不自动运行 AI；依赖 UX-01 |
| UX-03 | 首次使用、无匹配、错误分开 / P2 | 零档案只显示首次导入引导；有档案但无匹配才显示筛选提示；读取失败不能伪装空档案 | 不需要真实微信或演示包 |
| UX-04 | 导航与焦点闭环 / P2 | 用户导航可前进/后退；只保存视图 ID；打开详情移交焦点，关闭回到对应会话，目标消失时有可预测回退 | URL 不含联系人、搜索词、日期或正文；保留旧 hash |
| UX-05 | 稳定请求错误提示 / P2 | 断网、非 JSON、空响应、拒绝与超时用合成响应覆盖；固定可理解提示；无法确认写入结果时说明先核对，不自动重传 | 不弱化 AI 已有计费/未知调用保护 |

首轮用户流程：选择文件 → 正确识别 → 本机导入 → 看见结果 → 进入会话 → 返回清单。验收浅/深色、键盘、减少动态效果、320/390px 和原有权限回归。整体视觉重排单独进入 B，不把修复扩大成技术栈重写。

## B：建立可理解、可扩展的工作台（候选 v0.2.0）

| ID | 需求 / 优先级 | 完成条件 | 依赖 / 已有 Issue |
| --- | --- | --- | --- |
| PLAT-01 | 能力清单与最小运行依赖 / P2 | 分开 enabled、supported、installed、ready；展示固定阻塞原因/下一步；离线浏览不要求已配置微信；探测不安装、不读消息、不输出私人路径 | 作业入口继续做服务端准入；先覆盖 Windows/Linux 能力差异 |
| IMP-01 | 来源适配与质量预检 / P2 | JSON/Markdown 先只读预检；显示来源、有效/拒绝数、日期和发送方质量；预览不持久化聊天包，确认时校验同一输入与设置；异常不回显原话 | 依赖 DATA-01；不把不支持平台伪装成“缺安装” |
| DATA-01 | 统一归档保存契约 / P1 门槛 | 网页/兼容命令行统一新包、幂等复用、显式合并语义；合成验证旧记录保留、真实重复消息保留、并发与中断恢复 | 不改已有包 ID，不默认合并同名人；详细安全审查非公开 |
| UI-01 | 工作区重组与共享组件 / P2 | 资料、导入、分析、媒体、维护、设置职责清楚；导入不埋在维护页；工作区注册独立；复用主题 token/状态/按钮，不依赖其他可选脚本存在 | 保留兼容导航；依赖 UX-02/04、PLAT-01 |
| UI-02 | 小屏信息优先级 / P2 | 高级筛选可折叠并显示启用数；会话主信息无需先横向寻找；键盘可展开；测试 320/390/768/1440px、深色和文字放大 | 不以整页无溢出代替可用性；依赖 UI-01 |
| OBS-01 | 单一状态轮询 / P2 | 每个状态源最多一条在途请求；旧响应不能覆盖新状态；可见性切换安全；保留最近成功时点；拒绝自由文本错误原文 | 用延迟/乱序 mock 验证；不启动新后台作业 |
| DEMO-01 | 完全合成独立演示 / P2 | 干净克隆无需微信/Key；持续“合成演示”标识；不发现真实资料；失败与退出仅影响明确演示目录 | 复用 [#7](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/7)，不重复建单 |
| DIAG-01 | 无内容诊断摘要 / P2 | 主动触发、导出前预览；固定版本/能力/错误码白名单；嵌套异常不带内容、账号、路径、凭据；不自动上传 | 复用 [#8](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/8)，依赖 PLAT-01 |
| LIB-01 | 批量管理预览与版本冲突 / P2 | 精确目标与字段预览、逐项版本核对、重复提交保护、结果可核对；软隐藏可恢复，保留已删标签关联 | 复用 [#9](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/9)；不得批量物理删聊天 |
| A11Y-01 | 时间线辅助技术人工验收 / P2 | 至少一种实际屏幕阅读器记录版本与步骤；理解统计/AI/覆盖/节点；键盘、弹窗和焦点可用；无环境则保持未完成 | 复用 [#10](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/10)，不能以自动测试代替 |
| REL-01 | 可追踪版本与发布门禁 / P2 | VERSION 作为应用产品版本单一来源；页面/状态/变更记录/标签一致；验证关联 CI 与提交；报告标注生成版本但不重写旧报告 | 不把数据库/缓存 schema 跟随产品号自动升级；保留旧 tag |
| SRC-01 | 兼容入口与知识文档收敛 / P1 门槛 | 活跃/离线兼容/已停用来源清楚分开；入口说明一致；旧客户端先独立安全验收再接入作业；不索取聊天窗口凭据 | 公开只记录建设目标；凭据与潜在漏洞细节保持非公开 |

这一批不是单个巨型 PR。先 PLAT-01、DATA-01、IMP-01，再重组 UI 和演示；批量管理与人工无障碍分别验收。是否全部进入 v0.2.0，在首轮范围确认后再调整 GitHub 里程碑。

### DATA-01 第一阶段：v0.1.2

受托代理选择先统一四个离线转换器的不可覆盖保存，并加强网页身份/完整性复用与共享锁。实现与合成验收按 [设计](docs/superpowers/specs/2026-09-07-v012-import-storage-design.md) 和 [计划](docs/superpowers/plans/2026-09-07-v012-import-storage.md) 推进；行为边界见 [导入保存说明](docs/import-storage.md)。

本阶段追踪：[子任务 #22](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/22) · [v0.1.2 里程碑](https://github.com/weihaoting123-ctrl/human-online-relationships/milestone/4)。

本阶段不接管旧微信/QQ 在线提取器、显式文件输出、原生同步检查点，也不实现预检向导。主需求 [#15](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/15) 保持开放；DATA-01 与 IMP-01 不因这个补丁全部关闭。UI 工作区重组仍按依赖后续独立迭代。

## C：处理流程与数据覆盖（候选 v0.3.0）

| ID | 需求 / 优先级 | 完成条件 | 非目标与依赖 |
| --- | --- | --- | --- |
| JOB-01 | 统一阶段结果与作业记录 / P1 门槛 | 分开进程成功、等待初始化、部分覆盖、剩余队列、失败；记录本轮 ID 与新鲜度；旧归档备份不声称含本轮全部新内容 | 保留已完成检查点；不自动重试未知付费调用；依赖 PLAT-01/OBS-01 |
| ACCT-01 | 多账号本机确认闭环 / P1 门槛 | 多候选先停止；仅本机展示必要选择信息；确认绑定不透明身份；账号消失/变化重新确认；不按目录时间猜选 | 不后台遍历所有账号；不在 Issue 放候选资料 |
| MEDIA-01 | 资产覆盖与可靠关联 / P2 | 原件、预览、首帧、未解码、缺失与歧义分别计数；按证据唯一关联；日期注明来自文件还是消息；不按月份猜联系人 | 不重编码丢原件；不承诺恢复本机不存在媒体 |
| BKP-01 | 模块备份贡献契约 / P2 | 每个模块声明数据、凭据排除、SQLite 和锁；新增清单版本保留 v1/v2 恢复；核验范围明确；恢复仍为新目录 | AI 历史是否纳入须单独设计，不能连同 Key 打包 |
| PERF-01 | 大档案投影与检索基准 / P2 | 固定种子合成 1千/1万会话，记录数据量/机器条件/冷暖加载与检索；减少重复摘要读取后维持中文子串语义、覆盖提示、失效修复 | 先建立基准再定性能预算；管理库不可当缓存删除 |

## D：真实的多操作系统适配（候选 v0.4.0）

| ID | 需求 / 优先级 | 完成条件 | 非目标与依赖 |
| --- | --- | --- | --- |
| OS-01 | 各系统离线安装与启动验收 / P2 | Windows/Linux/macOS 分别从干净环境运行离线导入、检索、管理、时间线与合成备份；记录 CPU 架构和依赖；平台不支持时给固定原因 | macOS 未验证前不写“已支持”；不开放公网监听 |
| OS-02 | 可替换的系统凭据存储 / P2 | 平台安全存储接口独立、不可用时停止而非明文回退；迁移不自动解密旧资料或重发请求；合成测试异常与拒绝路径 | 依赖独立设计与安全审查；不是直接复制 DPAPI 文件到其他系统 |
| OS-03 | 平台语音运行时 / P2 | 各平台依赖/模型来源、校验、架构与失败状态明确；离线识别和缓存复用可复验；不支持时保留录音与队列 | 不以云转写替代本机授权；不重新识别所有成功项 |

微信原生读取的 macOS/Linux 适配尚无已验证来源，暂列研究依赖，不承诺在 v0.4.0 完成。移动网页适配不等同手机微信读取，QQ 与其他聊天平台也需各自来源契约。

## 首轮范围记录

已确认 A 批作为 v0.1.1：只修复导入、结果导航、空状态、焦点与错误提示，并进行独立 review/debug。B–D 是完整需求池，不意味着可以跳过其设计、权限和平台验收。

按独立实现规范与测试计划开发。完成代码和验证后才创建新版本标签/Release，并更新此处的实际版本记录。

## GitHub 主待办映射

已核对既有待办，新增 #12–#19；#12/#13 纳入 v0.1.1，其余候选不自动分配新里程碑或关闭。未列独立 Issue 的需求保留在池中，进入详细设计时再拆分；涉及潜在安全问题的细节保持非公开。

| 主 Issue | 稳定需求 ID |
| --- | --- |
| [#12 文件切换与导入结果](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/12) | UX-01、UX-02 |
| [#13 空状态、导航与反馈](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/13) | UX-03、UX-04、UX-05 |
| [#14 能力矩阵与离线启动](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/14) | PLAT-01、OS-01（分阶段） |
| [#15 导入预检与提交契约](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/15) | IMP-01、DATA-01 |
| [#16 工作区与小屏布局](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/16) | UI-01、UI-02 |
| [#17 作业与覆盖状态](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/17) | OBS-01、JOB-01（分阶段） |
| [#18 逐版发布验收](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/18) | REL-01 |
| [#19 媒体与备份契约](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/19) | MEDIA-01、BKP-01（独立子项） |
| [#7 合成演示](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/7) | DEMO-01，沿用原 Issue |
| [#8 诊断摘要](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/8) | DIAG-01，沿用原 Issue |
| [#9 批量管理](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/9) | LIB-01，沿用原 Issue |
| [#10 辅助技术验收](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/10) | A11Y-01，沿用原 Issue |

## 实际交付记录

| 项目 | 状态 | 证据 |
| --- | --- | --- |
| 公开软件 v0.1.0 | 已发布 | [Release](https://github.com/weihaoting123-ctrl/human-online-relationships/releases/tag/v0.1.0) |
| 解构与需求池 R1 → R3 | A 批已发布；DATA-01 第一阶段已确认 | 本文件与 [解构报告](docs/project-decomposition.md) |
| v0.1.1 | 代码与独立审查、本机完整回归已完成；远端发布状态以 Release 为准 | [审查记录](docs/reviews/v0.1.1-review.md) · [Release](https://github.com/weihaoting123-ctrl/human-online-relationships/releases/tag/v0.1.1) |
| v0.1.2 / DATA-01 第一阶段 | 代码、独立审查与本机回归完成；远端发布以 Release 为准 | [审查记录](docs/reviews/v0.1.2-review.md) · [Release](https://github.com/weihaoting123-ctrl/human-online-relationships/releases/tag/v0.1.2) |
| B–D 剩余需求 | 未实现、未发布 | 不以需求单、草案 PR 或旧 CI 代替完成证据 |
