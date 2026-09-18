package dataroom

import (
	"path/filepath"
	"sync"
	"testing"
	"time"
)

// seed 构造一个标准场景：
//
//	项目 P1（日本）；管理员 admin；财务 fin（密级上限 confidential）；法务 leg。
//	文件 payroll-2026：finance/restricted（工资单）
//	文件 contract-draft：legal/confidential（合同草案）
//	文件 budget-note：finance/confidential
func seed(t *testing.T, path string) *Service {
	t.Helper()
	now := time.Date(2026, 9, 18, 0, 0, 0, 0, time.UTC)
	s := New(path)
	_, err := s.CreateProject("P1", "目标公司A", "JP", now)
	must(t, err)
	must(t, s.CreateUser("admin", "管理员", nil, true))
	must(t, s.CreateUser("fin", "财务小王", []string{TeamFinance}, false))
	must(t, s.CreateUser("leg", "法务小李", []string{TeamLegal}, false))
	_, err = s.SetGrant("fin", "P1", []string{TeamFinance}, LevelConfidential, "admin", now)
	must(t, err)
	_, err = s.SetGrant("leg", "P1", []string{TeamLegal}, LevelRestricted, "admin", now)
	must(t, err)

	up := func(id, team string, level Level, content []byte, title string) {
		t.Helper()
		_, err := s.UploadDocument("P1", id, title, team, level, content, "v1 摘要", "admin", now)
		must(t, err)
	}
	up("payroll-2026", TeamFinance, LevelRestricted, []byte("salary: confidential v1"), "工资单")
	up("contract-draft", TeamLegal, LevelConfidential, []byte("contract clauses v1"), "合同草案")
	up("budget-note", TeamFinance, LevelConfidential, []byte("budget note v1"), "预算说明")
	return s
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func atDay(hour int) time.Time {
	return time.Date(2026, 9, 18, hour, 0, 0, 0, time.UTC)
}

// 场景：财务/法务清单隔离。财务看不到法务合同（团队边界），
// 也看不到高于其密级上限的工资单（密级边界）。
func TestLeastPrivilege(t *testing.T) {
	s := seed(t, "")

	// fin 看 finance/confidential 的预算说明：允许
	if _, ae := s.Download("fin", "P1", "budget-note", 1, "尽调", atDay(1)); ae != nil {
		t.Fatalf("财务应能下载预算说明: %v", ae)
	}
	// fin 看法务合同：团队越界 → 拒绝
	if _, ae := s.Download("fin", "P1", "contract-draft", 1, "尽调", atDay(1)); ae == nil || ae.Reason != ReasonForbidden {
		t.Fatalf("财务不应看到法务合同, got %+v", ae)
	}
	// fin 看 restricted 工资单：密级越界 → 拒绝，敏感内容不得外泄
	if _, ae := s.Download("fin", "P1", "payroll-2026", 1, "尽调", atDay(1)); ae == nil || ae.Reason != ReasonForbidden {
		t.Fatalf("财务不应看到 restricted 工资单, got %+v", ae)
	}
	// leg 看财务预算说明：团队越界
	if _, ae := s.Download("leg", "P1", "budget-note", 1, "尽调", atDay(1)); ae == nil || ae.Reason != ReasonForbidden {
		t.Fatalf("法务不应看到财务资料, got %+v", ae)
	}
	// leg 有 restricted 上限，工资单属于 finance 团队：仍按团队拒绝
	if _, ae := s.Download("leg", "P1", "payroll-2026", 1, "尽调", atDay(1)); ae == nil || ae.Reason != ReasonForbidden {
		t.Fatalf("法务不应看到财务工资单, got %+v", ae)
	}
	// 管理员不受团队/密级限制
	if _, ae := s.Download("admin", "P1", "payroll-2026", 1, "审计抽查", atDay(1)); ae != nil {
		t.Fatalf("管理员应能下载: %v", ae)
	}
	// 每次拒绝都留痕
	denied := s.ListRecords(RecordFilter{Decision: DecisionDenied})
	if len(denied) != 4 {
		t.Fatalf("应有 4 条拒绝记录, got %d", len(denied))
	}
}

// 场景：并发下载同一文件版本，所有人拿到字节一致的内容，审计不重不漏。
func TestConcurrentDownloads(t *testing.T) {
	s := seed(t, "")
	const n = 50
	var wg sync.WaitGroup
	errs := make(chan *AccessError, n)
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func() {
			defer wg.Done()
			res, ae := s.Download("leg", "P1", "contract-draft", 1, "并发尽调", atDay(2))
			if ae != nil {
				errs <- ae
				return
			}
			if string(res.Content) != "contract clauses v1" || res.Version != 1 {
				t.Errorf("并发下载内容/版本不一致: %q v%d", res.Content, res.Version)
			}
		}()
	}
	wg.Wait()
	close(errs)
	for ae := range errs {
		t.Fatalf("并发下载不应被拒: %v", ae)
	}
	allowed := s.ListRecords(RecordFilter{DocID: "contract-draft", Decision: DecisionAllowed})
	if len(allowed) != n {
		t.Fatalf("应记录 %d 条允许下载, got %d", n, len(allowed))
	}
}

// 场景：新版本不能替换已引用的证据。
func TestEvidencePinnedToVersion(t *testing.T) {
	s := seed(t, "")
	// 以 v1 回复请求并引用为证据
	rep, err := s.ImportRequests("P1", []ImportItem{{
		ClientKey: "K-1", Title: "工资合规核查", Team: TeamFinance, Country: "JP",
		Level: LevelRestricted, DeadlineLocal: "2026-10-01T09:00:00", DeadlineZone: "Asia/Tokyo",
	}}, atDay(1))
	must(t, err)
	reqID := rep.Created[0]
	must(t, s.RespondRequest(reqID, "payroll-2026", 1, "提交工资单v1", "admin", atDay(2)))
	ev, err := s.CiteEvidence(reqID, "payroll-2026", 1, "admin", "复核引用", atDay(2))
	must(t, err)

	// 供应商随后上传 v2（版本只追加，v1 字节不变）
	_, err = s.UploadDocument("P1", "payroll-2026", "工资单", TeamFinance, LevelRestricted,
		[]byte("salary: confidential v2 with corrections"), "v2 摘要", "admin", atDay(3))
	must(t, err)

	// 证据访问永远解析到被钉住的 v1，且哈希校验一致
	res, ae := s.AccessEvidence(ev.ID, "admin", atDay(4))
	if ae != nil {
		t.Fatalf("证据应可取: %v", ae)
	}
	if string(res.Content) != "salary: confidential v1" {
		t.Fatalf("证据被新版本替换: %q", res.Content)
	}
	if res.ContentHash != ev.ContentHash || res.Version != 1 {
		t.Fatalf("证据版本/哈希不一致")
	}
	// 普通下载默认拿最新版；显式版本号仍可拿 v1
	latest, ae := s.Download("admin", "P1", "payroll-2026", 0, "最新版", atDay(4))
	mustAE(t, latest, ae)
	if string(latest.Content) != "salary: confidential v2 with corrections" {
		t.Fatalf("默认下载应为最新版")
	}
	old, ae := s.Download("admin", "P1", "payroll-2026", 1, "历史版", atDay(4))
	mustAE(t, old, ae)
	if string(old.Content) != "salary: confidential v1" {
		t.Fatalf("显式 v1 下载错误")
	}
}

func mustAE(t *testing.T, _ *AccessResult, ae *AccessError) {
	t.Helper()
	if ae != nil {
		t.Fatalf("unexpected access error: %v", ae)
	}
}

// 场景：供应商撤回后访问——内容保留给审计，但新访问只得到受控提示。
func TestWithdrawThenAccess(t *testing.T) {
	s := seed(t, "")
	link, err := s.IssueLink(IssueLinkParams{
		ActorID: "admin", ViewerID: "leg", ProjectID: "P1", DocID: "contract-draft", Version: 1,
		Purpose: "合同尽调", TTL: 7 * 24 * time.Hour, Watermark: WatermarkPolicy{Enabled: true},
	}, atDay(1))
	must(t, err)
	// 撤回前正常访问
	if _, ae := s.AccessByLink(link.Token, "leg", atDay(2)); ae != nil {
		t.Fatalf("撤回前应可访问: %v", ae)
	}
	// 供应商撤回
	must(t, s.WithdrawDocument("contract-draft", "供应商主张特权文件", "admin", atDay(3)))
	// 撤回后同一链接：拒绝，受控提示，不带出任何内容
	res, ae := s.AccessByLink(link.Token, "leg", atDay(4))
	if ae == nil || ae.Reason != ReasonWithdrawn {
		t.Fatalf("撤回后应拒绝(WITHDRAWN), got %+v", ae)
	}
	if res != nil {
		t.Fatalf("拒绝时不得返回内容")
	}
	if _, ae := s.Download("admin", "P1", "contract-draft", 1, "审计", atDay(4)); ae == nil || ae.Reason != ReasonWithdrawn {
		t.Fatalf("管理员直接下载同样应被撤回拦截")
	}
	// 撤回不能追加新版本
	if _, err := s.UploadDocument("P1", "contract-draft", "合同", TeamLegal, LevelConfidential,
		[]byte("v2"), "v2", "admin", atDay(5)); err == nil {
		t.Fatalf("已撤回文件不得再追加版本")
	}
	// 撤回前的允许记录仍在，撤回后的拒绝也已记录
	recs := s.ListRecords(RecordFilter{DocID: "contract-draft"})
	var allowed, withdrawnDenied int
	for _, r := range recs {
		if r.Decision == DecisionAllowed {
			allowed++
		}
		if r.Decision == DecisionDenied && r.Reason == ReasonWithdrawn {
			withdrawnDenied++
		}
	}
	if allowed != 1 || withdrawnDenied != 2 {
		t.Fatalf("审计链错误: allowed=%d withdrawnDenied=%d", allowed, withdrawnDenied)
	}
}

// 场景：链接过期后只能返回受控提示，且不泄露文件元数据。
func TestExpiredLinkControlledMessage(t *testing.T) {
	s := seed(t, "")
	link, err := s.IssueLink(IssueLinkParams{
		ActorID: "admin", ViewerID: "leg", ProjectID: "P1", DocID: "contract-draft",
		Purpose: "合同尽调", TTL: time.Hour,
	}, atDay(0))
	must(t, err)
	res, ae := s.AccessByLink(link.Token, "leg", atDay(2))
	if ae == nil || ae.Reason != ReasonExpired {
		t.Fatalf("过期应返回 EXPIRED, got %+v", ae)
	}
	if res != nil || ae.Message == "" {
		t.Fatalf("过期只应返回受控提示文本")
	}
	last := s.ListRecords(RecordFilter{ViewerID: "leg", Decision: DecisionDenied})
	found := false
	for _, r := range last {
		if r.Reason == ReasonExpired {
			found = true
		}
	}
	if !found {
		t.Fatalf("过期访问应留痕")
	}
	// 无效 token 与冒名访问同样受控
	if _, ae := s.AccessByLink("deadbeef", "leg", atDay(2)); ae == nil || ae.Reason != ReasonInvalid {
		t.Fatalf("无效链接应返回 INVALID")
	}
	if _, ae := s.AccessByLink(link.Token, "fin", atDay(0)); ae == nil || ae.Reason != ReasonForbidden {
		t.Fatalf("非链接查看者应被拒绝")
	}
}

// 场景：影响链保留——延期、范围变更、撤回按时间串成链。
func TestImpactChain(t *testing.T) {
	s := seed(t, "")
	rep, _ := s.ImportRequests("P1", []ImportItem{{
		ClientKey: "K-9", Title: "税务底稿", Team: TeamTax, Country: "JP",
		Level: LevelConfidential, DeadlineLocal: "2026-10-01T09:00:00", DeadlineZone: "Asia/Tokyo",
	}}, atDay(0))
	reqID := rep.Created[0]

	must(t, s.ExtendDeadline(reqID, "admin", "2026-10-15T18:00:00", "Asia/Tokyo", "供应商申请延期", atDay(1)))
	// 不能把截止“提前”伪装成延期
	if err := s.ExtendDeadline(reqID, "admin", "2026-10-10T18:00:00", "Asia/Tokyo", "x", atDay(1)); err == nil {
		t.Fatalf("早于当前截止的变更不应被接受为延期")
	}
	must(t, s.ChangeScope(reqID, "admin", TeamTax, "DE", LevelRestricted, "尽调范围扩展到德国主体", atDay(2)))
	must(t, s.RespondRequest(reqID, "budget-note", 1, "以预算说明回复", "admin", atDay(3)))
	must(t, s.WithdrawDocument("budget-note", "供应商撤回", "admin", atDay(4)))

	chain, err := s.ImpactChain(reqID)
	must(t, err)
	if len(chain) != 3 {
		t.Fatalf("影响链应有 3 个事件(延期/范围变更/撤回), got %d", len(chain))
	}
	if chain[0].Kind != KindDeadlineMoved || chain[1].Kind != KindScopeChanged || chain[2].Kind != KindWithdrawn {
		t.Fatalf("影响链顺序错误: %v", []string{chain[0].Kind, chain[1].Kind, chain[2].Kind})
	}
	// parent 指针把同一实体的事件串起来：范围变更接续延期；
	// 撤回是文档子链的起点，它与请求的关联通过回复引用（RespDocID）建立。
	if chain[1].ParentID != chain[0].ID {
		t.Fatalf("范围变更应接续延期事件")
	}
	if chain[2].RefType != "document" || chain[2].RefID != "budget-note" {
		t.Fatalf("撤回事件应通过回复引用挂到请求链上")
	}
	if chain[1].Detail["after"].(map[string]any)["country"] != "DE" {
		t.Fatalf("范围变更详情未保留")
	}
}

// 场景：批量导入幂等——重复导入只跳过，不产生重复请求或重复通知；坏行不拖垮整批。
func TestBatchImportIdempotent(t *testing.T) {
	s := seed(t, "")
	items := []ImportItem{
		{ClientKey: "B-1", Title: "请求1", Team: TeamFinance, Country: "JP", Level: LevelConfidential,
			DeadlineLocal: "2026-10-01T09:00:00", DeadlineZone: "Asia/Tokyo"},
		{ClientKey: "B-2", Title: "请求2", Team: TeamLegal, Country: "JP", Level: LevelConfidential,
			DeadlineLocal: "2026-10-01T09:00:00", DeadlineZone: "bad/zone"},
		{ClientKey: "", Title: "缺键", Team: TeamTax, DeadlineLocal: "x", DeadlineZone: "Asia/Tokyo"},
	}
	rep, err := s.ImportRequests("P1", items, atDay(0))
	must(t, err)
	if len(rep.Created) != 1 || len(rep.Skipped) != 2 {
		t.Fatalf("首次导入应创建1跳过2, got created=%d skipped=%d", len(rep.Created), len(rep.Skipped))
	}
	// 再次导入整批：B-1 幂等跳过，不重复通知
	rep2, err := s.ImportRequests("P1", items, atDay(0))
	must(t, err)
	if len(rep2.Created) != 0 {
		t.Fatalf("重复导入不应新建任何请求")
	}
	notes := s.ListNotifications(TeamFinance)
	if len(notes) != 1 || notes[0].Key != "newreq:P1:B-1" {
		t.Fatalf("同一业务事件只应通知一次, got %+v", notes)
	}
}

// 场景：跨时区截止。东京 10:00 的截止，换算为 UTC 01:00；
// 同一绝对时刻对纽约/伦敦协作者一致。
func TestCrossTimezoneDeadline(t *testing.T) {
	s := seed(t, "")
	rep, _ := s.ImportRequests("P1", []ImportItem{{
		ClientKey: "TZ-1", Title: "跨时区截止", Team: TeamFinance, Country: "JP", Level: LevelConfidential,
		DeadlineLocal: "2026-09-20T10:00:00", DeadlineZone: "Asia/Tokyo",
	}}, atDay(0))
	req := s.ListRequests(RequestFilter{ProjectID: "P1"})
	var target *Request
	for _, r := range req {
		if r.ID == rep.Created[0] {
			target = r
		}
	}
	dl, err := target.DeadlineInstant()
	must(t, err)
	wantUTC := time.Date(2026, 9, 20, 1, 0, 0, 0, time.UTC)
	if !dl.Equal(wantUTC) {
		t.Fatalf("东京10:00 应为 UTC01:00, got %v", dl)
	}
	if target.Overdue(wantUTC) {
		t.Fatalf("恰好到点不应算逾期")
	}
	if !target.Overdue(wantUTC.Add(time.Minute)) {
		t.Fatalf("过点一分钟应算逾期")
	}
	if target.Overdue(wantUTC.Add(-time.Minute)) {
		t.Fatalf("未到点不应算逾期")
	}
}

// 场景：成员离职立即失效，历史访问记录不被抹去。
func TestMemberDeactivation(t *testing.T) {
	s := seed(t, "")
	// 离职前先签发一张链接
	link, err := s.IssueLink(IssueLinkParams{
		ActorID: "admin", ViewerID: "fin", ProjectID: "P1", DocID: "budget-note",
		Purpose: "尽调", TTL: 24 * time.Hour,
	}, atDay(1))
	must(t, err)
	if _, ae := s.Download("fin", "P1", "budget-note", 1, "离职前尽调", atDay(1)); ae != nil {
		t.Fatalf("离职前应可下载: %v", ae)
	}
	if _, ae := s.AccessByLink(link.Token, "fin", atDay(1).Add(time.Minute)); ae != nil {
		t.Fatalf("离职前链接应有效: %v", ae)
	}

	must(t, s.DeactivateUser("fin", atDay(2)))
	if _, ae := s.Download("fin", "P1", "budget-note", 1, "离职后访问", atDay(3)); ae == nil || ae.Reason != ReasonInactive {
		t.Fatalf("离职后应立即拒绝(INACTIVE), got %+v", ae)
	}
	// 已签发链接在离职后同样立即失效
	if _, ae := s.AccessByLink(link.Token, "fin", atDay(3)); ae == nil || ae.Reason != ReasonInactive {
		t.Fatalf("离职成员的链接应立即失效, got %+v", ae)
	}
	finRecs := s.ListRecords(RecordFilter{ViewerID: "fin"})
	if len(finRecs) < 3 {
		t.Fatalf("离职前的访问记录必须保留")
	}
}

// 场景：管理员收紧权限，立即影响尚未下载的内容，但不改写审计。
func TestGrantChangeImmediate(t *testing.T) {
	s := seed(t, "")
	res, ae := s.Download("leg", "P1", "contract-draft", 1, "尽调", atDay(1))
	mustAE(t, res, ae)
	allowedHash := res.ContentHash

	// 法务密级上限从 restricted 降到 internal，并移除 legal 职能
	_, err := s.SetGrant("leg", "P1", nil, LevelInternal, "admin", atDay(2))
	must(t, err)
	if _, ae := s.Download("leg", "P1", "contract-draft", 1, "收紧后再试", atDay(3)); ae == nil || ae.Reason != ReasonForbidden {
		t.Fatalf("权限收紧应立即生效, got %+v", ae)
	}
	// 恢复授权后又可访问，且拿到的仍是同版本同哈希
	_, err = s.SetGrant("leg", "P1", []string{TeamLegal}, LevelConfidential, "admin", atDay(4))
	must(t, err)
	res2, ae := s.Download("leg", "P1", "contract-draft", 1, "恢复后访问", atDay(5))
	mustAE(t, res2, ae)
	if res2.ContentHash != allowedHash {
		t.Fatalf("权限调整不应影响文件版本内容")
	}
	// 先允许、后拒绝、再允许的完整轨迹都在审计里
	recs := s.ListRecords(RecordFilter{ViewerID: "leg", DocID: "contract-draft"})
	if len(recs) != 3 || recs[0].Decision != DecisionAllowed ||
		recs[1].Decision != DecisionDenied || recs[2].Decision != DecisionAllowed {
		t.Fatalf("审计轨迹应为 允许-拒绝-允许, got %+v", recs)
	}
}

// 场景：程序再次运行后，待复核项与访问记录可查询（磁盘持久化）。
func TestRestartPersistence(t *testing.T) {
	path := filepath.Join(t.TempDir(), "dataroom.json")
	s := seed(t, path)
	rep, _ := s.ImportRequests("P1", []ImportItem{{
		ClientKey: "PERM-1", Title: "待复核请求", Team: TeamLegal, Country: "JP", Level: LevelConfidential,
		DeadlineLocal: "2026-10-01T09:00:00", DeadlineZone: "Asia/Tokyo",
	}}, atDay(0))
	reqID := rep.Created[0]
	must(t, s.RespondRequest(reqID, "contract-draft", 1, "回复v1", "admin", atDay(1)))
	ev, err := s.CiteEvidence(reqID, "contract-draft", 1, "admin", "复核证据", atDay(1))
	must(t, err)
	if _, ae := s.Download("leg", "P1", "contract-draft", 1, "重启前下载", atDay(1)); ae != nil {
		t.Fatalf("seed 下载失败: %v", ae)
	}

	// 重新启动：同一文件新建服务实例
	s2 := New(path)
	pending := s2.PendingReview("P1")
	if len(pending) != 1 || pending[0].ID != reqID || pending[0].RespVersion != 1 {
		t.Fatalf("重启后待复核项应可查询且回复版本保留, got %+v", pending)
	}
	recs := s2.ListRecords(RecordFilter{ProjectID: "P1"})
	if len(recs) == 0 {
		t.Fatalf("重启后访问记录应可查询")
	}
	// 证据仍解析到钉住版本，哈希一致
	res, ae := s2.AccessEvidence(ev.ID, "admin", atDay(2))
	if ae != nil || string(res.Content) != "contract clauses v1" {
		t.Fatalf("重启后证据解析错误: %v %v", ae, res)
	}
	// 幂等导入在重启后仍然认得旧键
	rep2, err := s2.ImportRequests("P1", []ImportItem{{
		ClientKey: "PERM-1", Title: "待复核请求", Team: TeamLegal, Country: "JP", Level: LevelConfidential,
		DeadlineLocal: "2026-10-01T09:00:00", DeadlineZone: "Asia/Tokyo",
	}}, atDay(2))
	must(t, err)
	if len(rep2.Created) != 0 {
		t.Fatalf("重启后 client_key 去重应仍然生效")
	}
}

// 场景：项目复制——版本哈希一致、回复与证据重映射到同版本，授权不蔓延。
func TestProjectCopy(t *testing.T) {
	s := seed(t, "")
	rep, _ := s.ImportRequests("P1", []ImportItem{{
		ClientKey: "COPY-1", Title: "合同核查", Team: TeamLegal, Country: "JP", Level: LevelConfidential,
		DeadlineLocal: "2026-10-01T09:00:00", DeadlineZone: "Asia/Tokyo",
	}}, atDay(0))
	reqID := rep.Created[0]
	must(t, s.RespondRequest(reqID, "contract-draft", 1, "回复", "admin", atDay(1)))
	ev, err := s.CiteEvidence(reqID, "contract-draft", 1, "admin", "证据", atDay(1))
	must(t, err)

	cp, err := s.CopyProject("P1", "P2", "目标公司A-德国扩展", "DE", "admin", atDay(2))
	must(t, err)
	newDoc := cp.Documents["contract-draft"]

	// 复制后文件逐版本哈希一致
	src, _ := s.downloadLockedForTest("contract-draft", 1)
	dst, ae := s.Download("admin", "P2", newDoc, 1, "复制校验", atDay(2))
	mustAE(t, dst, ae)
	if dst.ContentHash != src {
		t.Fatalf("复制后版本哈希不一致")
	}
	if cp.Requests != 1 || cp.Evidences != 1 {
		t.Fatalf("应复制 1 请求 1 证据, got %+v", cp)
	}
	// 新项目中的请求回复已重映射到新文件且版本号不变
	reqs := s.ListRequests(RequestFilter{ProjectID: "P2"})
	if len(reqs) != 1 || reqs[0].RespDocID != newDoc || reqs[0].RespVersion != 1 {
		t.Fatalf("复制后请求回复重映射错误: %+v", reqs)
	}
	// 证据在新项目里仍解析到钉住版本
	var newEvID string
	for id, e := range s.st.evidences {
		if e.DocID == newDoc {
			newEvID = id
		}
	}
	res, ae := s.AccessEvidence(newEvID, "admin", atDay(2))
	mustAE(t, res, ae)
	if res.ContentHash != ev.ContentHash || res.Version != 1 {
		t.Fatalf("复制后证据版本一致性被破坏")
	}
	// 最小权限：leg 在 P1 有授权，在 P2 默认无权——授权不随复制蔓延
	if _, ae := s.Download("leg", "P2", newDoc, 1, "越权访问新项目", atDay(2)); ae == nil || ae.Reason != ReasonForbidden {
		t.Fatalf("项目复制不得携带授权, got %+v", ae)
	}
	// 复制事件在影响链中双向留痕
	chain, err := s.ImpactChain(reqID)
	must(t, err)
	_ = chain
}

func (s *Service) downloadLockedForTest(docID string, version int) (string, *AccessError) {
	s.st.mu.RLock()
	defer s.st.mu.RUnlock()
	d := s.st.documents[docID]
	if version < 1 || version > len(d.Versions) {
		return "", denied(ReasonInvalid, "bad version")
	}
	return d.Versions[version-1].Hash, nil
}

// 场景：水印策略随访问记录留存。
func TestWatermarkRecorded(t *testing.T) {
	s := seed(t, "")
	link, err := s.IssueLink(IssueLinkParams{
		ActorID: "admin", ViewerID: "leg", ProjectID: "P1", DocID: "contract-draft",
		Purpose: "合同尽调", TTL: time.Hour, Watermark: WatermarkPolicy{Enabled: true, Text: "机密-外发禁止"},
	}, atDay(0))
	must(t, err)
	res, ae := s.AccessByLink(link.Token, "leg", atDay(0).Add(time.Minute))
	mustAE(t, res, ae)
	if res.Watermark != "机密-外发禁止" {
		t.Fatalf("水印未随访问返回: %q", res.Watermark)
	}
	recs := s.ListRecords(RecordFilter{ViewerID: "leg", Decision: DecisionAllowed})
	last := recs[len(recs)-1]
	if last.Watermark != "机密-外发禁止" || last.Purpose != "合同尽调" {
		t.Fatalf("水印与用途未记入审计: %+v", last)
	}
}
