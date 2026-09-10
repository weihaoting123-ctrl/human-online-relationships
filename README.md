# 人类线上关系可视化

**Human Online Relationships** — 从本机聊天档案中看见互动阶段、联系间隔和有依据的计划进展。

一个本地优先的关系回顾工具，适用于朋友、家人、同事、合作对象与亲密关系。它不鉴定他人的真实动机，不用消息量判断感情深浅，不给人格诊断或心理评分。

公开仓库只包含源码、文档和合成测试，不包含开发者的聊天、联系人、媒体、数据库、凭据、备份或运行日志。

[自动测试](https://github.com/weihaoting123-ctrl/human-online-relationships/actions/workflows/tests.yml) · [版本发布](https://github.com/weihaoting123-ctrl/human-online-relationships/releases) · [迭代里程碑](https://github.com/weihaoting123-ctrl/human-online-relationships/milestones) · [功能与缺陷待办](https://github.com/weihaoting123-ctrl/human-online-relationships/issues) · [公开验证记录](docs/release-verification.md)

## 当前功能

| 模块 | 能做什么 |
| --- | --- |
| 档案资料库 | 会话查找、中文搜索、筛选、排序和分页 |
| 关系时间线 | 最近归档记录、双方最近发言、周频率、高频阶段与无记录间隔 |
| 按范围 AI | 单一会话、日期范围、抽样或全量文字分段处理、覆盖率与独立历史报告 |
| 聊天热度 | 单会话月历热度、星期 × 小时分布、活跃日排行；本机计算，可选最多 366 天的完整范围 |
| 事件整理 | 计划 → 推进 → 结果，显示匿名证据位置，区分自述、推测与证据不足 |
| 媒体与语音 | 已在本机取得的图片/语音归档与本地语音转写；缺失内容明确标记 |
| 本机管理库 | SQLite 保存标签、备注、别名、置顶、回收站与模块开关；不覆盖原始聊天 |
| 增量与备份 | 检查变更分片、保留真实重复消息、内容寻址备份、恢复到新目录 |
| 可访问 UI | 系统字体、浅/深色、移动布局、键盘、减少动态效果与平滑详情弹窗 |

### 时间线不是读心术

- “多久没联系”准确含义是：距这份会话归档的最近有效记录多久。它不能代表线下或其他平台的交流。
- 历史日期过滤不会改变全归档最近记录的参考点；报告会标明统计时点。
- “高频”是有公开阈值的本机统计，不代表关系质量或感情升温。
- 提出计划不代表已经实现。只有后续样本明确自述时才标记自述落实/取消，仍未经现实核实。
- 少联系的原因默认为未知；抽样、跨段摘要及未完成结果都可能缺少上下文。
- 当前不跨会话合并人员身份，也不生成全联系人社交网络。

## 快速开始（Windows）

已测试的主要运行环境：Windows 10/11、Python 3.12、Node.js 22。微信本机读取、DPAPI 凭据存储和媒体处理以 Windows 为主；其他平台主要用于离线逻辑测试，不宣称完整支持。

先在空间充足的非 C 盘目录打开 PowerShell：

```powershell
git clone https://github.com/weihaoting123-ctrl/human-online-relationships.git
cd human-online-relationships
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start_dashboard.ps1
```

打开 <http://127.0.0.1:8765/>。启动页面不会自动导出聊天、创建定时任务或运行云端分析。停止页面服务前请确认没有正在进行的分析或备份，然后运行 `stop_dashboard.ps1`。

### 导入与模型配置

1. 已有 JSON 可通过控制台导入；原件和派生归档留在 `data/`。
2. Windows 微信首次读取需要本人授权、已登录的微信和唯一目标账号。按 [数据来源说明](.agents/skills/she-love-me/references/data-sources.md) 进行项目内固定版本引导；多账号必须选择，不自动发送微信消息。
3. 语音转写需要另外下载本机模型及固定依赖。未在本机取得的媒体不能凭空恢复。
4. 云端分析是可选功能。只在本地页面输入自己的 API Key，保存到当前 Windows 用户的 DPAPI 加密存储；不要发到 Issue、PR、日志或聊天窗口。
5. 每次选择会话、日期、文字覆盖方式和是否包含转写，核对接收方与调用计划后单独确认。服务商可能收费；自动脱敏不能保证自由文本完全匿名。

“全部日期”只改变日期范围，不自动切换到全量文字。全量文字有明确安全上限，超限拒绝，不静默截断；分段失败/状态未知不会自动重试。图片、原始音频及其他会话不会混入文字分析。

离线导入的保存与复用契约、旧包兼容和并发边界见 [导入保存说明](docs/import-storage.md)。重复导入复用不等于删除聊天中的真实重复记录。

## 数据与安全边界

- 原始聊天、语音、图片和备份都属于私人材料，即使加密也不能提交到 Git。
- 不修改、删除或发送微信原记录；“回收站”仅隐藏管理视图。
- 同步和备份是本机工作流，不触发云端 AI。定时运行需由使用者另行配置，克隆仓库不会自动注册任务。
- 控制台仅监听 `127.0.0.1`，不是可直接暴露公网的多用户服务。不要端口转发、公开部署或开放访问私有归档。
- 同盘备份不能防止整块磁盘损坏。恢复只写新目录，不覆盖活动数据或微信目录。
- 公开发布采用独立工作区和新提交历史，不推送私人运行项目的整个目录或旧历史。

详见 [隐私与发布](docs/public-release.md)、[备份边界](docs/local-backup.md) 和 [安全报告方式](SECURITY.md)。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m playwright install chromium
$taskTemp = Join-Path (Get-Location) 'scripts\tmp'
New-Item -ItemType Directory -Force -Path $taskTemp | Out-Null
$env:TEMP = (Resolve-Path $taskTemp).Path
$env:TMP = $env:TEMP
$env:SHE_LOVE_ME_UI_TESTS = '1'
$env:SHE_LOVE_ME_UI_SCREENSHOTS = '0'
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_catalog.cjs
.\.venv\Scripts\python.exe scripts/check_public_release.py --root .
```

测试只使用合成会话、临时数据库和模拟服务商响应。默认发布检查覆盖 Git 已跟踪/暂存文件及其工作区内容；新增文件必须先审阅再暂存。`--tree` 只用于不含运行目录的干净发布树，会拒绝测试生成的 `data/`、缓存及虚拟环境。不要为了调试向贡献者提供真实聊天或截图。CI 在 GitHub 托管运行器运行，不连接个人微信、不使用云模型凭据，也不上传运行归档。

## 项目与迭代管理

- 当前公开基线版本由 [VERSION](VERSION) 管理，变更记录见 [CHANGELOG](CHANGELOG.md)。
- 功能边界与后续迭代见 [ROADMAP](ROADMAP.md)，提交、PR、标签与里程碑约定见 [CONTRIBUTING](CONTRIBUTING.md)。
- 深度解构、模块边界及真实平台限制见 [项目审查](docs/project-decomposition.md)；分批需求、依赖与验收见 [需求池](BACKLOG.md)。候选版本需确认并实现，不等于已经发布。
- Issues 使用问题/功能模板；按 `type:*`、`area:*`、`priority:*` 分类。未完成的想法不得写成已实现功能。
- 默认分支 `main`；开发使用 `codex/<topic>` 或 `feature/<topic>`，通过 PR、测试和隐私检查进入版本。
- [产品思路](docs/relationship-product.md)、[模块与数据库](.agents/skills/she-love-me/references/local-architecture.md)、[时间线约束](.agents/skills/she-love-me/references/relationship-timeline.md)、[UI 设计](docs/apple-ui-design.md)。

内部 Skill ID 保留 `she-love-me` 以兼容既有工具入口，可用 `$she-love-me` 调用。旧版完整关系报告仅为兼容路径，不代表本项目新的默认分析方法。

## 来源与许可

本项目基于 [863401402/she-love-me](https://github.com/863401402/she-love-me) 扩展。保留上游 MIT 版权声明，新公开版本号与上游版本历史独立。第三方读取器和语音模型不打包在仓库中，安装时适用其各自许可。

见 [LICENSE](LICENSE) 与 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。
