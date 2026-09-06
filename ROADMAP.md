# 路线图

路线图是待验证的工作计划，不是已交付功能或固定发布日期承诺。范围调整通过 Issue 讨论；不以“已写文档”代替代码和验收完成。

## v0.1.0：首个公开基线

当前已实现能力见 [CHANGELOG](CHANGELOG.md)。本基线聚焦单会话、本机优先、只读原件、独立管理字段，以及有明确证据和授权边界的关系时间线。

公开发布门槛：

- [x] 仅发布代码、许可文档和合成夹具；检查待发布文件及 Git 历史不含个人数据或凭据。
- [x] 从干净克隆复验文档入口，记录相关测试通过/跳过/失败，不借用私人部署环境证明可安装。
- [ ] 核对来源和许可证、`VERSION`、CHANGELOG、标签与 Release 一致。
- [x] 启用 GitHub 私下漏洞报告，核对表单链接和基础分诊标签。

以上按实际结果勾选，证据见 [公开验证记录](docs/release-verification.md) 和 [发布验收 Issue #6](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/6)。版本与 Release 核对在发布后单独完成。

## v0.2.0：可复现体验与管理效率（候选，未实现）

| 待办 | 建议标签 | 可验收完成条件 |
| --- | --- | --- |
| 完全合成的独立演示模式 | `area:dashboard`、`type:feature`、`priority:P2` | 从干净克隆按文档启动虚构会话演示；不需要微信登录、个人文件或云密钥；页面持续显示“合成演示”；不发现/修改真实资料目录；退出后只清理明确的演示临时目录 |
| 无内容诊断摘要导出 | `area:privacy-security`、`type:feature`、`priority:P2` | 用户主动导出版本、模块可用性和固定错误码白名单；默认及异常分支均不含聊天、账号、文件路径、请求正文、日志原文和凭据；合成敏感标记测试证明未泄漏；导出前可预览内容 |
| 批量标签/置顶/隐藏的预览与冲突保护 | `area:library`、`type:feature`、`priority:P2` | 先显示精确目标数量和拟改字段再确认；按各会话版本检测冲突，冲突时不静默覆盖且结果逐项可核对；保留软删标签关联；隐藏可恢复；不批量物理删除聊天；覆盖中途失败与重复提交测试 |
| 时间线屏幕阅读器人工验收 | `area:timeline`、`type:test`、`priority:P2` | 仅用合成记录，记录至少一种桌面屏幕阅读器的名称/版本和验收步骤；可理解统计与AI的区别、部分覆盖、节点类型、弹窗名称和分页状态；键盘完成打开/关闭/返回焦点；缺陷修复后提供可重复验收记录，不冒称无障碍认证 |

每个候选先建立独立 Issue，明确非目标和测试方法。仅当验收全部满足、回归通过并写入 CHANGELOG 后，才能移入已完成清单。需要扩展数据收集、联网、云费用或数据删除权限的设计必须重新讨论，不由路线图默认授权。

## 暂不规划

- 跨联系人身份归并或自动绘制“真实社交关系”网络。
- 根据沉默、频率或语音转写推断感情、人格、欺骗或真实停联原因。
- 默认上传全量聊天、后台定时付费分析、绕过逐次确认。
- 物理删除微信/QQ 源数据、公开个人归档或收集真实聊天作评测集。

## 管理方式

已建立 [v0.1.0 · 公开基线](https://github.com/weihaoting123-ctrl/human-online-relationships/milestone/1) 和 [v0.2.0 · 可复现体验与管理效率](https://github.com/weihaoting123-ctrl/human-online-relationships/milestone/2)，不预设到期日。首个里程碑只有发布门槛完成后关闭；后者的四项工作分别记录在 [#7](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/7)、[#8](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/8)、[#9](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/9)、[#10](https://github.com/weihaoting123-ctrl/human-online-relationships/issues/10)，仍待实现或人工验收。标签说明见 [贡献指南](CONTRIBUTING.md)。
