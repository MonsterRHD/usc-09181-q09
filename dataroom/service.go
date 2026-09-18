package dataroom

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"
)

// AccessError 携带机器可读的拒绝原因，HTTP 层据此返回受控提示，
// 绝不回退到“把内容或内部错误堆栈吐给调用方”。
type AccessError struct {
	Reason  string
	Message string
}

func (e *AccessError) Error() string { return e.Reason + ": " + e.Message }

func denied(reason, msg string) *AccessError { return &AccessError{Reason: reason, Message: msg} }

var ErrNotFound = errors.New("not found")
var ErrConflict = errors.New("conflict")
var ErrValidation = errors.New("validation")

type Service struct {
	st *store
}

func New(persistPath string) *Service {
	return &Service{st: newStore(persistPath)}
}

// ---------- 工具 ----------

func hashBytes(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func newToken() string {
	b := make([]byte, 24)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// deadlineInstant 把“当地挂钟时间 + IANA 时区”换算成绝对时刻，
// 跨时区协作者面对的是同一个瞬间。
func deadlineInstant(local, zone string) (time.Time, error) {
	loc, err := time.LoadLocation(zone)
	if err != nil {
		return time.Time{}, fmt.Errorf("%w: 时区 %q 无法识别", ErrValidation, zone)
	}
	t, err := time.ParseInLocation("2006-01-02T15:04:05", local, loc)
	if err != nil {
		return time.Time{}, fmt.Errorf("%w: 截止时间格式应为 YYYY-MM-DDTHH:MM:SS", ErrValidation)
	}
	return t, nil
}

func (r *Request) DeadlineInstant() (time.Time, error) {
	return deadlineInstant(r.DeadlineLocal, r.DeadlineZone)
}

func (r *Request) Overdue(at time.Time) bool {
	dl, err := r.DeadlineInstant()
	if err != nil {
		return false
	}
	return at.After(dl)
}

// ---------- 用户与授权 ----------

func (s *Service) CreateUser(id, name string, teams []string, isAdmin bool) error {
	s.st.mu.Lock()
	defer s.st.mu.Unlock()
	if _, ok := s.st.users[id]; ok {
		return fmt.Errorf("%w: 用户已存在", ErrConflict)
	}
	t := append([]string(nil), teams...)
	s.st.users[id] = &User{ID: id, Name: name, Teams: t, IsAdmin: isAdmin, Active: true}
	s.st.save()
	return nil
}

// DeactivateUser 处理成员离职：立即失效。后续任何下载/链接访问都会被拒，
// 但历史访问记录原样保留。
func (s *Service) DeactivateUser(id string, at time.Time) error {
	s.st.mu.Lock()
	defer s.st.mu.Unlock()
	u, ok := s.st.users[id]
	if !ok {
		return fmt.Errorf("%w: 用户不存在", ErrNotFound)
	}
	u.Active = false
	s.st.save()
	return nil
}

// SetGrant 调整某用户在某项目的职能范围与密级上限。
// 判定发生在每一次访问时，因此调整对“尚未下载”的内容立即生效，
// 已落盘的审计记录不会被改写。
func (s *Service) SetGrant(userID, projectID string, teams []string, maxLevel Level, actor string, at time.Time) (*Grant, error) {
	s.st.mu.Lock()
	defer s.st.mu.Unlock()
	if _, ok := s.st.users[userID]; !ok {
		return nil, fmt.Errorf("%w: 用户不存在", ErrNotFound)
	}
	if _, ok := s.st.projects[projectID]; !ok {
		return nil, fmt.Errorf("%w: 项目不存在", ErrNotFound)
	}
	for _, t := range teams {
		if !ValidTeam(t) {
			return nil, fmt.Errorf("%w: 团队 %q 非法", ErrValidation, t)
		}
	}
	g := &Grant{UserID: userID, ProjectID: projectID, Teams: append([]string(nil), teams...), MaxLevel: maxLevel}
	s.st.grants[grantKey(userID, projectID)] = g
	s.st.save()
	return g, nil
}

// authorize 必须在持锁状态下调用。
func (s *Service) authorize(userID, projectID, team string, level Level) (bool, string) {
	u, ok := s.st.users[userID]
	if !ok || !u.Active {
		return false, ReasonInactive
	}
	if u.IsAdmin {
		return true, ""
	}
	g, ok := s.st.grants[grantKey(userID, projectID)]
	if !ok {
		return false, ReasonForbidden
	}
	if level > g.MaxLevel {
		return false, ReasonForbidden
	}
	if team != "" {
		allowed := false
		for _, t := range g.Teams {
			if t == team {
				allowed = true
				break
			}
		}
		if !allowed {
			return false, ReasonForbidden
		}
	}
	return true, ""
}

// ---------- 项目 ----------

func (s *Service) CreateProject(id, name, country string, at time.Time) (*Project, error) {
	s.st.mu.Lock()
	defer s.st.mu.Unlock()
	if _, ok := s.st.projects[id]; ok {
		return nil, fmt.Errorf("%w: 项目已存在", ErrConflict)
	}
	p := &Project{ID: id, Name: name, Country: country, CreatedAt: at}
	s.st.projects[id] = p
	s.st.save()
	return p, nil
}

func (s *Service) GetProject(id string) (*Project, error) {
	s.st.mu.RLock()
	defer s.st.mu.RUnlock()
	p, ok := s.st.projects[id]
	if !ok {
		return nil, fmt.Errorf("%w: 项目不存在", ErrNotFound)
	}
	cp := *p
	return &cp, nil
}

// ---------- 文件与版本 ----------

type UploadResult struct {
	DocID       string `json:"doc_id"`
	Version     int    `json:"version"`
	ContentHash string `json:"content_hash"`
}

// UploadDocument 上传新文件，或向已有文件追加新版本。版本只追加、永不覆盖。
func (s *Service) UploadDocument(projectID, docID, title, team string, level Level, content []byte, summary, uploader string, at time.Time) (*UploadResult, error) {
	if !ValidTeam(team) {
		return nil, fmt.Errorf("%w: 团队 %q 非法", ErrValidation, team)
	}
	if level < LevelInternal || level > LevelRestricted {
		return nil, fmt.Errorf("%w: 保密级别非法", ErrValidation)
	}
	if len(content) == 0 {
		return nil, fmt.Errorf("%w: 文件内容为空", ErrValidation)
	}
	s.st.mu.Lock()
	defer s.st.mu.Unlock()
	if _, ok := s.st.projects[projectID]; !ok {
		return nil, fmt.Errorf("%w: 项目不存在", ErrNotFound)
	}
	if u, ok := s.st.users[uploader]; !ok || !u.Active {
		return nil, fmt.Errorf("%w: 上传者无效或已离职", ErrValidation)
	}

	doc, exists := s.st.documents[docID]
	if docID != "" && exists {
		if doc.ProjectID != projectID {
			return nil, fmt.Errorf("%w: 文件不属于该项目", ErrValidation)
		}
		if doc.Withdrawn {
			return nil, fmt.Errorf("%w: 文件已被供应商撤回，不能再追加版本", ErrConflict)
		}
		next := len(doc.Versions) + 1
		doc.Versions = append(doc.Versions, DocVersion{
			Number: next, Content: append([]byte(nil), content...), Hash: hashBytes(content),
			Size: len(content), Summary: summary, UploadedBy: uploader, UploadedAt: at,
		})
		s.st.save()
		return &UploadResult{DocID: doc.ID, Version: next, ContentHash: doc.Versions[next-1].Hash}, nil
	}

	id := docID
	if id == "" {
		id = s.st.nextID("doc")
	} else if _, taken := s.st.documents[id]; taken {
		return nil, fmt.Errorf("%w: 文件 ID 已被占用", ErrConflict)
	}
	doc = &Document{ID: id, ProjectID: projectID, Title: title, Team: team, Level: level}
	doc.Versions = append(doc.Versions, DocVersion{
		Number: 1, Content: append([]byte(nil), content...), Hash: hashBytes(content),
		Size: len(content), Summary: summary, UploadedBy: uploader, UploadedAt: at,
	})
	s.st.documents[id] = doc
	s.st.save()
	return &UploadResult{DocID: id, Version: 1, ContentHash: doc.Versions[0].Hash}, nil
}

// UploadVersion 向已有文件追加新版本，沿用其团队与密级，不允许借上传改密级。
func (s *Service) UploadVersion(projectID, docID string, content []byte, summary, uploader string, at time.Time) (*UploadResult, error) {
	if len(content) == 0 {
		return nil, fmt.Errorf("%w: 文件内容为空", ErrValidation)
	}
	s.st.mu.Lock()
	defer s.st.mu.Unlock()
	doc, ok := s.st.documents[docID]
	if !ok || doc.ProjectID != projectID {
		return nil, fmt.Errorf("%w: 文件不存在或不属于该项目", ErrNotFound)
	}
	if u, ok := s.st.users[uploader]; !ok || !u.Active {
		return nil, fmt.Errorf("%w: 上传者无效或已离职", ErrValidation)
	}
	if doc.Withdrawn {
		return nil, fmt.Errorf("%w: 文件已被供应商撤回，不能再追加版本", ErrConflict)
	}
	next := len(doc.Versions) + 1
	doc.Versions = append(doc.Versions, DocVersion{
		Number: next, Content: append([]byte(nil), content...), Hash: hashBytes(content),
		Size: len(content), Summary: summary, UploadedBy: uploader, UploadedAt: at,
	})
	s.st.save()
	return &UploadResult{DocID: doc.ID, Version: next, ContentHash: doc.Versions[next-1].Hash}, nil
}

// WithdrawDocument 供应商撤回：内容字节保留（证据与审计需要），
// 但之后任何新的访问只得到受控提示。撤回进入影响链。
func (s *Service) WithdrawDocument(docID, reason, actor string, at time.Time) error {
	s.st.mu.Lock()
	defer s.st.mu.Unlock()
	doc, ok := s.st.documents[docID]
	if !ok {
		return fmt.Errorf("%w: 文件不存在", ErrNotFound)
	}
	if doc.Withdrawn {
		return fmt.Errorf("%w: 文件已处于撤回状态", ErrConflict)
	}
	doc.Withdrawn = true
	when := at
	doc.WithdrawnAt = &when
	doc.WithdrawReason = reason
	parent := s.lastImpactLocked("document", docID)
	s.st.impacts = append(s.st.impacts, ImpactEvent{
		ID: s.st.nextID("impact"), ProjectID: doc.ProjectID, Kind: KindWithdrawn,
		RefType: "document", RefID: docID, Actor: actor, Note: reason, ParentID: parent, At: at,
	})
	s.st.save()
	return nil
}

// ---------- 资料请求与批量导入 ----------

type ImportItem struct {
	ClientKey     string `json:"client_key"`
	Title         string `json:"title"`
	Team          string `json:"team"`
	Country       string `json:"country"`
	Description   string `json:"description"`
	Level         Level  `json:"level"`
	DeadlineLocal string `json:"deadline_local"`
	DeadlineZone  string `json:"deadline_zone"`
}

type ImportReport struct {
	Created []string        `json:"created"`
	Skipped []SkippedImport `json:"skipped"`
}

type SkippedImport struct {
	ClientKey string `json:"client_key"`
	Reason    string `json:"reason"`
}

// ImportRequests 批量导入。ClientKey 是外部清单的稳定标识：
// 重复执行（批量重试/重复通知场景）只会跳过，不会产生重复请求或重复通知。
// 单行非法不影响其他行，保证批量稳定处理。
func (s *Service) ImportRequests(projectID string, items []ImportItem, at time.Time) (*ImportReport, error) {
	s.st.mu.Lock()
	defer s.st.mu.Unlock()
	if _, ok := s.st.projects[projectID]; !ok {
		return nil, fmt.Errorf("%w: 项目不存在", ErrNotFound)
	}
	rep := &ImportReport{Created: []string{}, Skipped: []SkippedImport{}}
	if _, ok := s.st.keyIndex[projectID]; !ok {
		s.st.keyIndex[projectID] = map[string]string{}
	}
	for _, it := range items {
		if strings.TrimSpace(it.ClientKey) == "" {
			rep.Skipped = append(rep.Skipped, SkippedImport{it.ClientKey, "缺少 client_key"})
			continue
		}
		if _, dup := s.st.keyIndex[projectID][it.ClientKey]; dup {
			rep.Skipped = append(rep.Skipped, SkippedImport{it.ClientKey, "重复键，已跳过"})
			continue
		}
		if !ValidTeam(it.Team) {
			rep.Skipped = append(rep.Skipped, SkippedImport{it.ClientKey, "团队非法"})
			continue
		}
		if _, err := deadlineInstant(it.DeadlineLocal, it.DeadlineZone); err != nil {
			rep.Skipped = append(rep.Skipped, SkippedImport{it.ClientKey, "截止时间/时区非法"})
			continue
		}
		req := &Request{
			ID: s.st.nextID("req"), ProjectID: projectID, ClientKey: it.ClientKey,
			Title: it.Title, Team: it.Team, Country: it.Country, Description: it.Description,
			Level: it.Level, Status: StatusOpen,
			DeadlineLocal: it.DeadlineLocal, DeadlineZone: it.DeadlineZone,
		}
		s.st.requests[req.ID] = req
		s.st.keyIndex[projectID][it.ClientKey] = req.ID
		rep.Created = append(rep.Created, req.ID)
		s.notifyLocked(&Notification{
			ID: s.st.nextID("ntf"), UserID: it.Team, // 团队队列
			Key:   fmt.Sprintf("newreq:%s:%s", projectID, it.ClientKey),
			Title: "新资料请求：" + it.Title, Body: it.Description, At: at,
		})
	}
	s.st.save()
	return rep, nil
}

type RequestFilter struct {
	ProjectID string
	Team      string
	Country   string
	Status    string
}

func (s *Service) ListRequests(f RequestFilter) []*Request {
	s.st.mu.RLock()
	defer s.st.mu.RUnlock()
	out := []*Request{}
	for _, r := range s.st.requests {
		if f.ProjectID != "" && r.ProjectID != f.ProjectID {
			continue
		}
		if f.Team != "" && r.Team != f.Team {
			continue
		}
		if f.Country != "" && r.Country != f.Country {
			continue
		}
		if f.Status != "" && r.Status != f.Status {
			continue
		}
		cp := *r
		out = append(out, &cp)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// PendingReview 是“程序再次运行后待复核项可查询”的直接入口。
func (s *Service) PendingReview(projectID string) []*Request {
	return s.ListRequests(RequestFilter{ProjectID: projectID, Status: StatusPendingReview})
}

// RespondRequest 上传方回复请求：钉住具体文件版本，状态进入待复核。
func (s *Service) RespondRequest(requestID, docID string, version int, summary, actor string, at time.Time) error {
	s.st.mu.Lock()
	defer s.st.mu.Unlock()
	req, ok := s.st.requests[requestID]
	if !ok {
		return fmt.Errorf("%w: 请求不存在", ErrNotFound)
	}
	doc, ok := s.st.documents[docID]
	if !ok || doc.ProjectID != req.ProjectID {
		return fmt.Errorf("%w: 文件不存在或不属于该项目", ErrNotFound)
	}
	if doc.Withdrawn {
		return fmt.Errorf("%w: 文件已撤回，不能作为回复", ErrConflict)
	}
	if version < 1 || version > len(doc.Versions) {
		return fmt.Errorf("%w: 版本号非法", ErrValidation)
	}
	if ok, reason := s.authorize(actor, req.ProjectID, doc.Team, doc.Level); !ok {
		return denied(reason, "无权使用该文件回复")
	}
	req.RespDocID = docID
	req.RespVersion = version
	req.RespSummary = summary
	req.RespondedAt = &at
	req.Status = StatusPendingReview
	s.st.save()
	return nil
}

// SubmitReview 记录复核结论。
func (s *Service) SubmitReview(requestID, reviewerID, conclusion, note string, at time.Time) error {
	s.st.mu.Lock()
	defer s.st.mu.Unlock()
	req, ok := s.st.requests[requestID]
	if !ok {
		return fmt.Errorf("%w: 请求不存在", ErrNotFound)
	}
	if req.Status != StatusPendingReview {
		return fmt.Errorf("%w: 当前状态 %s 不可复核", ErrConflict, req.Status)
	}
	if ok, reason := s.authorize(reviewerID, req.ProjectID, req.Team, req.Level); !ok {
		return denied(reason, "无权复核该请求")
	}
	c := strings.ToUpper(conclusion)
	req.Review = &ReviewConclusion{ReviewerID: reviewerID, Conclusion: c, Note: note, At: at}
	if c == "APPROVED" {
		req.Status = StatusReviewed
	} else {
		req.Status = StatusChangesNeeded
	}
	s.st.save()
	return nil
}

// ExtendDeadline 截止延期：保留旧值与新值，挂到该请求的影响链上。
func (s *Service) ExtendDeadline(requestID, actor, newLocal, newZone, note string, at time.Time) error {
	s.st.mu.Lock()
	defer s.st.mu.Unlock()
	req, ok := s.st.requests[requestID]
	if !ok {
		return fmt.Errorf("%w: 请求不存在", ErrNotFound)
	}
	oldInstant, err := req.DeadlineInstant()
	if err != nil {
		return err
	}
	newInstant, err := deadlineInstant(newLocal, newZone)
	if err != nil {
		return err
	}
	if !newInstant.After(oldInstant) {
		return fmt.Errorf("%w: 延期后的截止必须晚于原截止", ErrValidation)
	}
	parent := s.lastImpactLocked("request", requestID)
	s.st.impacts = append(s.st.impacts, ImpactEvent{
		ID: s.st.nextID("impact"), ProjectID: req.ProjectID, Kind: KindDeadlineMoved,
		RefType: "request", RefID: requestID, Actor: actor, Note: note, ParentID: parent, At: at,
		Detail: map[string]any{
			"from": req.DeadlineLocal + " " + req.DeadlineZone,
			"to":   newLocal + " " + newZone,
		},
	})
	req.DeadlineLocal = newLocal
	req.DeadlineZone = newZone
	s.st.save()
	return nil
}

// ChangeScope 尽调范围变更（团队/国家/密级调整），同样进影响链。
func (s *Service) ChangeScope(requestID, actor, newTeam, newCountry string, newLevel Level, note string, at time.Time) error {
	s.st.mu.Lock()
	defer s.st.mu.Unlock()
	req, ok := s.st.requests[requestID]
	if !ok {
		return fmt.Errorf("%w: 请求不存在", ErrNotFound)
	}
	if newTeam != "" && !ValidTeam(newTeam) {
		return fmt.Errorf("%w: 团队非法", ErrValidation)
	}
	before := map[string]any{"team": req.Team, "country": req.Country, "level": int(req.Level)}
	if newTeam != "" {
		req.Team = newTeam
	}
	if newCountry != "" {
		req.Country = newCountry
	}
	if newLevel >= LevelInternal && newLevel <= LevelRestricted {
		req.Level = newLevel
	}
	parent := s.lastImpactLocked("request", requestID)
	s.st.impacts = append(s.st.impacts, ImpactEvent{
		ID: s.st.nextID("impact"), ProjectID: req.ProjectID, Kind: KindScopeChanged,
		RefType: "request", RefID: requestID, Actor: actor, Note: note, ParentID: parent, At: at,
		Detail: map[string]any{"before": before, "after": map[string]any{
			"team": req.Team, "country": req.Country, "level": int(req.Level),
		}},
	})
	s.st.save()
	return nil
}

// ---------- 证据：版本钉住 ----------

// CiteEvidence 在复核结论中引用证据：把版本号与内容哈希同时钉死。
// 文件之后再上传新版本，证据仍解析到被引用的那一版。
func (s *Service) CiteEvidence(requestID, docID string, version int, citedBy, note string, at time.Time) (*Evidence, error) {
	s.st.mu.Lock()
	defer s.st.mu.Unlock()
	req, ok := s.st.requests[requestID]
	if !ok {
		return nil, fmt.Errorf("%w: 请求不存在", ErrNotFound)
	}
	doc, ok := s.st.documents[docID]
	if !ok || doc.ProjectID != req.ProjectID || version < 1 || version > len(doc.Versions) {
		return nil, fmt.Errorf("%w: 文件/版本不存在", ErrNotFound)
	}
	v := doc.Versions[version-1]
	if ok, reason := s.authorize(citedBy, req.ProjectID, doc.Team, doc.Level); !ok {
		return nil, denied(reason, "无权引用该证据")
	}
	e := &Evidence{
		ID: s.st.nextID("ev"), ProjectID: req.ProjectID, RequestID: requestID,
		DocID: docID, Version: version, ContentHash: v.Hash, CitedBy: citedBy, Note: note, At: at,
	}
	s.st.evidences[e.ID] = e
	s.st.save()
	return e, nil
}

func (s *Service) GetEvidence(id string) (*Evidence, error) {
	s.st.mu.RLock()
	defer s.st.mu.RUnlock()
	e, ok := s.st.evidences[id]
	if !ok {
		return nil, fmt.Errorf("%w: 证据不存在", ErrNotFound)
	}
	cp := *e
	return &cp, nil
}

// ---------- 访问链接 ----------

type IssueLinkParams struct {
	ActorID   string
	ViewerID  string
	ProjectID string
	DocID     string
	Version   int // 0 表示签发时解析为最新版并钉住
	Purpose   string
	TTL       time.Duration
	Watermark WatermarkPolicy
}

// IssueLink 签发受控链接，绑定查看者、用途、有效期、水印策略。
func (s *Service) IssueLink(p IssueLinkParams, at time.Time) (*AccessLink, error) {
	if strings.TrimSpace(p.Purpose) == "" {
		return nil, fmt.Errorf("%w: 必须声明访问用途", ErrValidation)
	}
	if p.TTL <= 0 {
		return nil, fmt.Errorf("%w: 有效期必须为正", ErrValidation)
	}
	s.st.mu.Lock()
	defer s.st.mu.Unlock()
	doc, ok := s.st.documents[p.DocID]
	if !ok || doc.ProjectID != p.ProjectID {
		return nil, fmt.Errorf("%w: 文件不存在", ErrNotFound)
	}
	ver := p.Version
	if ver == 0 {
		ver = len(doc.Versions)
	}
	if ver < 1 || ver > len(doc.Versions) {
		return nil, fmt.Errorf("%w: 版本号非法", ErrValidation)
	}
	// 签发者必须自己有权（管理员或授权成员）；查看者权限在每次访问时再判一次。
	if ok, reason := s.authorize(p.ActorID, p.ProjectID, doc.Team, doc.Level); !ok {
		return nil, denied(reason, "无权签发链接")
	}
	if u, ok := s.st.users[p.ViewerID]; !ok || !u.Active {
		return nil, denied(ReasonInactive, "查看者无效或已离职")
	}
	l := &AccessLink{
		Token: newToken(), ViewerID: p.ViewerID, ProjectID: p.ProjectID, DocID: p.DocID,
		Version: ver, Purpose: p.Purpose, ExpiresAt: at.Add(p.TTL), Watermark: p.Watermark, CreatedAt: at,
	}
	s.st.links[l.Token] = l
	s.st.save()
	return l, nil
}

type AccessResult struct {
	Content     []byte
	DocID       string
	Version     int
	ContentHash string
	Watermark   string
	Purpose     string
	Record      AccessRecord
}

// resolveContent 是所有取内容路径的统一闸门：
// 成员状态、授权（实时）、撤回、链接有效期都在这里判定，
// 无论允许还是拒绝都写一条只追加审计记录。
func (s *Service) resolveContent(viewerID, projectID, docID string, version int, token, purpose, action string, at time.Time) (*AccessResult, *AccessError) {
	s.st.mu.Lock()
	rec := AccessRecord{
		ID: s.st.nextID("rec"), At: at, Token: token, ViewerID: viewerID, ActorID: viewerID,
		ProjectID: projectID, DocID: docID, Version: version, Action: action, Purpose: purpose,
	}
	fail := func(reason, msg string) (*AccessResult, *AccessError) {
		rec.Decision = DecisionDenied
		rec.Reason = reason
		s.st.records = append(s.st.records, rec)
		s.st.save()
		s.st.mu.Unlock()
		return nil, denied(reason, msg)
	}

	doc, ok := s.st.documents[docID]
	if !ok {
		return fail(ReasonInvalid, "链接无效：资料不存在")
	}
	if doc.ProjectID != projectID {
		return fail(ReasonInvalid, "链接无效：资料不属于该项目")
	}
	if version < 1 || version > len(doc.Versions) {
		return fail(ReasonInvalid, "链接无效：版本不存在")
	}
	v := doc.Versions[version-1]

	if ok, reason := s.authorize(viewerID, projectID, doc.Team, doc.Level); !ok {
		return fail(reason, controlledMessage(reason))
	}
	if doc.Withdrawn {
		rec.ContentHash = v.Hash
		return fail(ReasonWithdrawn, "该资料已被供应商撤回，如需访问请联系项目管理员")
	}

	rec.Decision = DecisionAllowed
	rec.ContentHash = v.Hash
	wm := ""
	s.st.records = append(s.st.records, rec)
	s.st.save()
	content := append([]byte(nil), v.Content...)
	servedHash := hashBytes(content)
	s.st.mu.Unlock()

	if servedHash != v.Hash {
		// 版本一致性自检：实际交付字节必须等于版本入库时的哈希。
		return nil, denied(ReasonInvalid, "内容完整性校验失败")
	}
	return &AccessResult{
		Content: content, DocID: docID, Version: version, ContentHash: v.Hash,
		Watermark: wm, Purpose: purpose, Record: rec,
	}, nil
}

func controlledMessage(reason string) string {
	switch reason {
	case ReasonExpired:
		return "链接已过期，请向项目管理员重新申请"
	case ReasonInactive:
		return "账号已停用，访问被拒绝"
	case ReasonForbidden:
		return "您无权访问该保密级别或职能范围的资料"
	default:
		return "访问未被授权"
	}
}

// AccessByLink 通过受控链接访问。过期链接不返回任何文件元数据，只有受控提示。
func (s *Service) AccessByLink(token, viewerID string, at time.Time) (*AccessResult, *AccessError) {
	s.st.mu.RLock()
	link, ok := s.st.links[token]
	s.st.mu.RUnlock()
	if !ok {
		// 无效 token 也要留痕，但没有项目/文件上下文。
		s.st.mu.Lock()
		s.st.records = append(s.st.records, AccessRecord{
			ID: s.st.nextID("rec"), At: at, Token: token, ViewerID: viewerID,
			Action: ActionEvidence, Decision: DecisionDenied, Reason: ReasonInvalid,
		})
		s.st.save()
		s.st.mu.Unlock()
		return nil, denied(ReasonInvalid, controlledMessage(ReasonInvalid))
	}
	if viewerID != link.ViewerID {
		s.st.mu.Lock()
		s.st.records = append(s.st.records, AccessRecord{
			ID: s.st.nextID("rec"), At: at, Token: token, ViewerID: viewerID, ActorID: viewerID,
			ProjectID: link.ProjectID, DocID: link.DocID, Version: link.Version,
			Action: ActionEvidence, Decision: DecisionDenied, Reason: ReasonForbidden,
		})
		s.st.save()
		s.st.mu.Unlock()
		return nil, denied(ReasonForbidden, controlledMessage(ReasonForbidden))
	}
	if !at.Before(link.ExpiresAt) {
		s.st.mu.Lock()
		s.st.records = append(s.st.records, AccessRecord{
			ID: s.st.nextID("rec"), At: at, Token: token, ViewerID: viewerID,
			ProjectID: link.ProjectID, DocID: link.DocID, Version: link.Version,
			Action: ActionEvidence, Purpose: link.Purpose, Decision: DecisionDenied, Reason: ReasonExpired,
		})
		s.st.save()
		s.st.mu.Unlock()
		return nil, denied(ReasonExpired, controlledMessage(ReasonExpired))
	}
	res, err := s.resolveContent(link.ViewerID, link.ProjectID, link.DocID, link.Version,
		token, link.Purpose, ActionEvidence, at)
	if err == nil && link.Watermark.Enabled {
		// 水印策略随每次访问记录；呈现层把查看者、用途、时间叠加到页面。
		wm := link.Watermark.Text
		if wm == "" {
			wm = fmt.Sprintf("机密 %s 用途:%s %s", link.ViewerID, link.Purpose, at.Format(time.RFC3339))
		}
		res.Watermark = wm
		s.st.mu.Lock()
		for i := range s.st.records {
			if s.st.records[i].ID == res.Record.ID {
				s.st.records[i].Watermark = wm
				s.st.save()
				break
			}
		}
		s.st.mu.Unlock()
	}
	return res, err
}

// Download 项目成员直接下载（并发下载场景）。version 为 0 时取最新版。
func (s *Service) Download(viewerID, projectID, docID string, version int, purpose string, at time.Time) (*AccessResult, *AccessError) {
	if version == 0 {
		s.st.mu.RLock()
		doc, ok := s.st.documents[docID]
		if ok {
			version = len(doc.Versions)
		}
		s.st.mu.RUnlock()
	}
	return s.resolveContent(viewerID, projectID, docID, version, "", purpose, ActionDownload, at)
}

// AccessEvidence 通过证据引用访问：解析到被钉住的版本，并校验交付哈希等于证据哈希。
// 这样“文件的新版本”永远不会替换“已引用的证据”。
func (s *Service) AccessEvidence(evidenceID, viewerID string, at time.Time) (*AccessResult, *AccessError) {
	s.st.mu.RLock()
	ev, ok := s.st.evidences[evidenceID]
	s.st.mu.RUnlock()
	if !ok {
		return nil, denied(ReasonInvalid, "证据不存在")
	}
	res, err := s.resolveContent(viewerID, ev.ProjectID, ev.DocID, ev.Version,
		"", "证据复核:"+evidenceID, ActionEvidence, at)
	if err != nil {
		return nil, err
	}
	if res.ContentHash != ev.ContentHash {
		return nil, denied(ReasonInvalid, "证据版本与引用哈希不一致")
	}
	return res, nil
}

// ---------- 审计 ----------

type RecordFilter struct {
	ProjectID string
	ViewerID  string
	DocID     string
	Decision  string
}

// ListRecords 查询访问记录。记录只追加：没有删除/改写接口，
// 离职、收回权限、撤回文件都不会抹去历史。
func (s *Service) ListRecords(f RecordFilter) []AccessRecord {
	s.st.mu.RLock()
	defer s.st.mu.RUnlock()
	out := make([]AccessRecord, 0, len(s.st.records))
	for _, r := range s.st.records {
		if f.ProjectID != "" && r.ProjectID != f.ProjectID {
			continue
		}
		if f.ViewerID != "" && r.ViewerID != f.ViewerID {
			continue
		}
		if f.DocID != "" && r.DocID != f.DocID {
			continue
		}
		if f.Decision != "" && r.Decision != f.Decision {
			continue
		}
		out = append(out, r)
	}
	return out
}

// ---------- 影响链 ----------

func (s *Service) lastImpactLocked(refType, refID string) string {
	for i := len(s.st.impacts) - 1; i >= 0; i-- {
		e := s.st.impacts[i]
		if e.RefType == refType && e.RefID == refID {
			return e.ID
		}
	}
	return ""
}

// ImpactChain 返回某请求的完整影响链：请求自身的范围变更/延期事件，
// 加上其回复文件的撤回事件，按时间排序。即使文件后来撤回，链也完整保留。
func (s *Service) ImpactChain(requestID string) ([]ImpactEvent, error) {
	s.st.mu.RLock()
	defer s.st.mu.RUnlock()
	req, ok := s.st.requests[requestID]
	if !ok {
		return nil, fmt.Errorf("%w: 请求不存在", ErrNotFound)
	}
	docIDs := map[string]bool{}
	if req.RespDocID != "" {
		docIDs[req.RespDocID] = true
	}
	for _, ev := range s.st.evidences {
		if ev.RequestID == requestID {
			docIDs[ev.DocID] = true
		}
	}
	out := []ImpactEvent{}
	for _, e := range s.st.impacts {
		if e.RefType == "request" && e.RefID == requestID {
			out = append(out, e)
			continue
		}
		if e.RefType == "document" && docIDs[e.RefID] {
			out = append(out, e)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].At.Before(out[j].At) })
	return out, nil
}

// ---------- 项目复制 ----------

type CopyReport struct {
	NewProjectID string            `json:"new_project_id"`
	Documents    map[string]string `json:"documents"` // 旧 docID -> 新 docID
	Requests     int               `json:"requests"`
	Evidences    int               `json:"evidences"`
}

// CopyProject 把项目复制到新的国家/名称下，用于验证最小权限与版本一致性：
//   - 文件连同全部不可变版本复制，版本号与内容哈希保持一致；
//   - 请求、回复（重映射到新文件的同版本）、复核结论、证据（同版本同哈希）复制；
//   - 授权 Grant 不复制：新项目默认谁都看不了，必须重新授权，杜绝权限蔓延；
//   - 链接、访问记录不复制（它们属于原项目的审计边界）；
//   - 在新旧项目各挂一条复制事件，保留影响链。
func (s *Service) CopyProject(srcProjectID, newID, newName, newCountry, actor string, at time.Time) (*CopyReport, error) {
	s.st.mu.Lock()
	defer s.st.mu.Unlock()
	if _, ok := s.st.projects[srcProjectID]; !ok {
		return nil, fmt.Errorf("%w: 源项目不存在", ErrNotFound)
	}
	if _, ok := s.st.projects[newID]; ok {
		return nil, fmt.Errorf("%w: 新项目 ID 已存在", ErrConflict)
	}
	rep := &CopyReport{NewProjectID: newID, Documents: map[string]string{}}
	s.st.projects[newID] = &Project{ID: newID, Name: newName, Country: newCountry, CreatedAt: at}

	// 文件：逐版本复制字节，并断言哈希一致。
	for oldID, d := range s.st.documents {
		if d.ProjectID != srcProjectID {
			continue
		}
		nd := &Document{
			ID: s.st.nextID("doc"), ProjectID: newID, Title: d.Title, Team: d.Team, Level: d.Level,
			Withdrawn: d.Withdrawn, WithdrawReason: d.WithdrawReason,
		}
		if d.WithdrawnAt != nil {
			t := *d.WithdrawnAt
			nd.WithdrawnAt = &t
		}
		for _, v := range d.Versions {
			nv := DocVersion{
				Number: v.Number, Content: append([]byte(nil), v.Content...), Hash: hashBytes(v.Content),
				Size: v.Size, Summary: v.Summary, UploadedBy: v.UploadedBy, UploadedAt: v.UploadedAt,
			}
			if nv.Hash != v.Hash {
				return nil, fmt.Errorf("复制后版本哈希不一致: %s v%d", oldID, v.Number)
			}
			nd.Versions = append(nd.Versions, nv)
		}
		s.st.documents[nd.ID] = nd
		rep.Documents[oldID] = nd.ID
	}

	// 请求：保留 client_key（在新项目命名空间下不冲突）、回复与复核结论。
	oldToNewReq := map[string]string{}
	for _, r := range s.st.requests {
		if r.ProjectID != srcProjectID {
			continue
		}
		nr := *r
		nr.ID = s.st.nextID("req")
		nr.ProjectID = newID
		if r.RespDocID != "" {
			nr.RespDocID = rep.Documents[r.RespDocID]
		}
		if r.RespondedAt != nil {
			t := *r.RespondedAt
			nr.RespondedAt = &t
		}
		if r.Review != nil {
			rv := *r.Review
			nr.Review = &rv
		}
		s.st.requests[nr.ID] = &nr
		oldToNewReq[r.ID] = nr.ID
		if _, ok := s.st.keyIndex[newID]; !ok {
			s.st.keyIndex[newID] = map[string]string{}
		}
		s.st.keyIndex[newID][r.ClientKey] = nr.ID
		rep.Requests++
	}

	// 证据：同版本、同哈希指向复制后的文件。
	for _, e := range s.st.evidences {
		if e.ProjectID != srcProjectID {
			continue
		}
		ne := *e
		ne.ID = s.st.nextID("ev")
		ne.ProjectID = newID
		ne.RequestID = oldToNewReq[e.RequestID]
		ne.DocID = rep.Documents[e.DocID]
		if s.st.documents[ne.DocID].Versions[ne.Version-1].Hash != e.ContentHash {
			return nil, fmt.Errorf("复制后证据哈希不一致: %s", e.ID)
		}
		s.st.evidences[ne.ID] = &ne
		rep.Evidences++
	}

	s.st.impacts = append(s.st.impacts,
		ImpactEvent{
			ID: s.st.nextID("impact"), ProjectID: srcProjectID, Kind: KindCopied,
			RefType: "project", RefID: srcProjectID, Actor: actor, At: at,
			Note: "复制为新项目 " + newID, Detail: map[string]any{"new_project_id": newID, "country": newCountry},
		},
		ImpactEvent{
			ID: s.st.nextID("impact"), ProjectID: newID, Kind: KindCopied,
			RefType: "project", RefID: newID, Actor: actor, At: at,
			Note: "复制自项目 " + srcProjectID, Detail: map[string]any{"source_project_id": srcProjectID},
		},
	)
	s.st.save()
	return rep, nil
}

// ---------- 通知 ----------

func (s *Service) notifyLocked(n *Notification) {
	if _, exists := s.st.notifByKey[n.Key]; exists {
		return // 同一业务事件只通知一次（重复导入/重试场景）
	}
	s.st.notifByKey[n.Key] = n
}

func (s *Service) ListNotifications(userOrTeam string) []Notification {
	s.st.mu.RLock()
	defer s.st.mu.RUnlock()
	out := []Notification{}
	for _, n := range s.st.notifByKey {
		if n.UserID == userOrTeam {
			out = append(out, *n)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}
