# 跨境并购资料室（Cross-border M&A Data Room）

为跨境并购尽调提供受控资料访问的可运行服务：按**项目 / 国家 / 保密级别**组织资料请求、上传摘要与复核结论；
访问链接记录**查看者、用途、有效期限、水印策略**；最小权限、版本一致性、撤回与影响链、审计留痕全部可验证。

## 要解决的问题

财务、法务、税务团队各自维护清单，权限边界模糊，导致工资单、合同草案等敏感资料被不该看到的人转发。
本系统把边界做成**每次访问时强制判定**的不变量，而不是靠流程自觉。

## 核心不变量

| 不变量 | 实现方式 |
| --- | --- |
| 最小权限 | 授权 `Grant` = 职能团队 + 最高可见密级；每次下载/链接访问/引用都实时判定。跨团队、超密级一律拒绝 |
| 权限调整立即生效 | 授权不随链接/内容下发，访问时重新查 `Grant`；收紧后尚未下载的内容立刻不可得 |
| 证据版本钉住 | 证据在引用时同时钉死**版本号 + 内容哈希**；文件再传新版本，证据仍解析到被引用的那一版 |
| 版本只追加 | `DocVersion` 不可变、不可覆盖；新版本追加，旧版本字节与哈希永久保留 |
| 供应商撤回 | 撤回后内容字节保留（证据/审计需要），但任何新访问只得到 410 受控提示，且不能再追加版本 |
| 过期链接受控 | 过期/无效/冒名访问只返回原因码与面向用户的提示（410/404/403），不泄露任何文件元数据 |
| 影响链可追溯 | 范围变更、截止延期、文件撤回、项目复制都写只追加事件，按请求聚合成链 |
| 审计不被抹去 | 访问记录只追加，无删除/改写接口；离职、收权、撤回都不改写历史 |
| 离职即时失效 | 停用后直接下载与已签发链接立即全部拒绝（INACTIVE） |
| 批量稳定处理 | 导入按 `client_key` 幂等去重，坏行（团队/时区非法）跳过不拖垮整批；通知按键去重 |
| 跨时区截止 | 截止用「当地挂钟时间 + IANA 时区」表达，换算成同一绝对时刻判定逾期 |
| 复制不蔓延权限 | 项目复制逐版本复制字节并断言哈希一致，但授权/链接/访问记录不复制，新项目默认谁都看不了 |
| 重启可查询 | 全量状态以 JSON 快照原子落盘；重启后待复核项、访问记录、证据、撤回状态原样恢复 |

## 目录

```
dataroom/
  domain.go       领域模型：项目/用户/授权/文件版本/请求/证据/链接/审计/影响链/通知
  store.go        存储：内存结构 + JSON 快照（原子 rename 落盘）
  service.go      业务规则：权限判定、版本化、请求流转、受控访问闸门、影响链、项目复制
  api.go          HTTP 适配层：把拒绝映射为受控响应（403/410/404），绝不回显内容或堆栈
  *_test.go       覆盖全部交付验证场景
main.go           服务入口
```

## 运行

```bash
go run .                       # 默认持久化到 data/dataroom.json
DATAROOM_DB=/path/db.json PORT=8080 go run .
DATAROOM_DB="" go run .        # 纯内存模式（重启不保留，仅用于测试）
```

演示环境用请求头标识身份（生产应由鉴权网关注入）：`X-Actor`（操作人）、`X-Viewer`（查看人）。

## 主要 HTTP 接口

- 管理：`POST /admin/projects`、`POST /admin/users`、`POST /admin/users/{id}/deactivate`、`POST /admin/grants`
- 文件：`POST /projects/{p}/documents`、`POST /documents/{d}/versions`、`POST /documents/{d}/withdraw`
- 请求：`POST /projects/{p}/requests/import`、`GET /projects/{p}/requests`、`GET /projects/{p}/pending-review`
- 流转：`POST /requests/{r}/respond`、`POST /requests/{r}/review`、`POST /requests/{r}/extend-deadline`、`POST /requests/{r}/scope`、`GET /requests/{r}/impact`
- 证据：`POST /requests/{r}/evidence`、`GET /evidence/{id}`
- 受控访问：`POST /links`、`GET /links/{token}`、`GET /download?project=&doc=&version=&purpose=`
- 审计/复制：`GET /records`、`POST /projects/{p}/copy`

访问成功返回内容（base64）+ 版本号 + 内容哈希 + 水印；失败只返回：

```json
{"reason":"EXPIRED","message":"链接已过期，请向项目管理员重新申请"}
```

## 交付验证场景 → 测试

| 场景 | 测试 |
| --- | --- |
| 最小权限（财务看不到合同/工资单） | `TestLeastPrivilege`、`TestHTTPEndToEndControlledAccess` |
| 并发下载（50 并发，`-race`，字节一致、审计不重不漏） | `TestConcurrentDownloads` |
| 撤回后访问 | `TestWithdrawThenAccess` |
| 链接过期 / 无效 / 冒名 | `TestExpiredLinkControlledMessage`、HTTP 端到端 |
| 新版本不能替换已引用证据 | `TestEvidencePinnedToVersion`、`TestHTTPEvidenceVersionAndCopyFlow` |
| 影响链（延期/范围/撤回） | `TestImpactChain` |
| 项目复制的版本一致性与权限隔离 | `TestProjectCopy`、HTTP 端到端 |
| 批量导入去重 / 坏行隔离 | `TestBatchImportIdempotent` |
| 跨时区截止 | `TestCrossTimezoneDeadline` |
| 成员离职即时生效 | `TestMemberDeactivation` |
| 权限收紧立即影响未下载内容 | `TestGrantChangeImmediate` |
| 重启后待复核项与访问记录可查询 | `TestRestartPersistence`、`TestHTTPServerRestart` |
| 水印与用途留痕 | `TestWatermarkRecorded` |

```bash
go test ./... -race -count=1
```

## 说明

- 存储采用单文件 JSON 快照，目标是把领域不变量讲清楚、可随时重启验证；高并发生产部署应替换为
  支持事务的持久化实现，`service.go` 的业务规则与锁语义不依赖具体存储。
- 真实文件内容、水印渲染、鉴权网关注入身份为接入项；当前水印随访问记录留痕，呈现层据此叠加查看者/用途/时间。
