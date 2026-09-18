# 跨境并购资料室 (Cross-Border M&A Data Room)

面向海外标的尽调场景的资料室服务：财务、法务、税务团队在同一个系统里按
**项目 / 国家 / 保密级别** 组织资料请求、上传摘要与复核结论，并对每次访问记录
**查看者、用途、时间、水印**。核心目标是最小权限、证据版本不被覆盖、撤回与
变更留下完整影响链、链接过期只返回受控提示。

## 为什么是 Python

仓库最初是一个 Go 健康检查脚手架，但本沙箱无法获得 Go 工具链（go.dev、
GCS、apt/conda 镜像均被出口代理拦截）。实现改为 **零第三方依赖的 Python 3.11
标准库**（`http.server` + `sqlite3` + `zoneinfo`），因此在任何装有 Python 3.11+
的机器上可直接运行、测试，无需联网安装依赖。

## 运行

```bash
python3 demo.py --db data/demo.db           # 写入一个演示场景
python3 -m dataroom --db data/demo.db --port 8080
curl -s http://127.0.0.1:8080/health
```

## 测试

```bash
python3 run_tests.py        # 39 个行为测试，零依赖
```

测试直接覆盖交付验收场景：并发下载、撤回后访问、项目复制、链接过期/撤回、
权限即时调整、跨时区截止、批量导入去重、成员离职、重启后可查询。

## 领域规则与实现位置

| 需求 | 规则 | 代码 |
| --- | --- | --- |
| 最小权限 | 每个（项目, 用户）一条有效授权：角色 + 最高保密级别（1–3），每次打开**重新读取**，不缓存 | `services.py: access_document / _live_grant` |
| 权限调整立即生效 | 降级/撤销在下一次打开即命中；已发生的访问不回滚，授权变更全部进 `permission_audit` | `grant_permission / revoke_permission` |
| 版本一致性 | 文档只追加版本；复核与引用固定在**确切版本号 + sha256**，新上传永不替换已引用证据 | `upload_version / cite_evidence / resolve_citation` |
| 供应商撤回 | 撤回后禁止再上传、禁止打开和链接访问；事件进影响链 | `supplier_withdraw` |
| 尽调范围变更 | 国家/级别变更与请求关闭追加 `request_scope_events`，不改写历史 | `change_scope / close_request` |
| 截止延期 | 旧/新截止、原因、操作者全部保留；截止时间按**项目时区**解释（SGT/EST 等） | `extend_deadline`, `clock.py` |
| 受控链接 | 链接固定版本、记录用途/有效期/水印；过期、撤回、配额耗尽、**未知 token** 一律返回同一句 `this link is no longer available`（410），不泄露存在性 | `create_link / access_link` |
| 水印 | 每次渲染都带上国家、查看者、用途、时间；外部链接持有人单独标注 | `watermark.py`, `_render` |
| 影响链 | 上传、撤回、链接撤回、范围变更、延期、克隆来源全部追加不可变事件 | `impact_events` |
| 批量导入 | `(batch_key, item_ref)` 幂等；坏行单独报错且可重试，不产生孤儿请求；通知不重复 | `import_batch` |
| 重复通知 | 通知按 `dedup_key` 唯一，重试/重复导入不产生噪音 | `_notify` |
| 成员离职 | 账号置 departed，全部项目授权立即撤销并留痕，且不能被重新授权 | `mark_user_departed` |
| 项目复制 | 复制文档、版本（同 sha256）、复核与引用；**不复制权限**，新项目从零授权 | `clone_project` |
| 审计 | 允许、水印放行、各类拒绝、过期、撤回全部写 `access_records`，重启后可查 | `access_records`, SQLite WAL |
| 重启可查 | 所有状态（含文件字节，按 sha256 内容寻址）都在 SQLite 中 | `storage.py` |
| 并发 | 单库 + 写锁串行化：并发上传版本号单调无缺口；链接配额条件更新保证 k 次成功；下载全量留痕 | `_tx`, `upload_version`, `access_link` |

## HTTP API（节选）

鉴权用 `X-Actor: <user_id>` 头（生产环境只需替换这一处为 SSO）。

```bash
# 带水印下载（财务，级别够）与越权访问（税务，级别不够，403）
curl "$B/documents/$DOC?actor=usr_fin&purpose=QoE"
curl "$B/documents/$DOC?actor=usr_tax&purpose=peek"
#  → {"error":"denied_clearance", ...}

# 固定版本 + 用途 + 有效期的外链；过期/撤回后只有受控提示
curl -X POST "$B/links" -H "X-Actor: usr_fin" -H "Content-Type: application/json" \
  -d '{"document_id":"'$DOC'","version":1,"purpose":"external counsel","ttl_hours":72}'
curl "$B/links/$LNK"                       # 水印字节，始终 v1
curl -X POST "$B/links/$LNK/revoke" -H "X-Actor: usr_admin" -d '{}'
curl "$B/links/$LNK"                       # 410 link_unavailable

# 已引用证据永远解析到被引用的确切版本（哪怕文档已到 v3）
curl "$B/citations/$CIT?actor=usr_legal&purpose=court"

# 审计 / 待复核 / 影响链
curl "$B/records?project_id=$PID"
curl "$B/reviews/pending?project_id=$PID"
curl "$B/documents/$DOC/impact"
```

完整端点见 `dataroom/app.py` 顶部注释。成功的文件类响应通过
`X-Decision / X-Watermark / X-Document-Version / X-Content-Sha256` 头携带判定
元信息；失败统一为 `{"error": <稳定错误码>, "message": ...}`。

## 目录

```
dataroom/
  clock.py       时钟与跨时区截止解析（可冻结，便于测试）
  errors.py      稳定错误码（denied_clearance / link_unavailable …）
  watermark.py   水印策略
  storage.py     SQLite schema、WAL、串行化访问
  services.py    全部领域规则
  app.py         HTTP JSON API
tests/           39 个行为测试（含真实 HTTP 与并发场景）
demo.py          演示数据
run_tests.py     零依赖测试运行器
```

## 刻意的边界

* HTTP 层认证是占位的 `X-Actor`，生产接入 SSO 时只替换这一处，授权判定不变。
* 文件字节存在 SQLite 的内容寻址 blob 表；大文件/对象存储是后续替换点，
  版本与证据模型不需要改动。
* 保密级别使用 1–3 的有序整数，映射到内部“公开/秘密/绝密”标签在接入时配置。
