package dataroom

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// Server 把领域服务暴露为 HTTP。演示环境用 X-Actor/X-Viewer 头标识身份；
// 生产部署应替换为网关鉴权后注入的身份。
type Server struct {
	svc *Service
}

func NewServer(svc *Service) http.Handler {
	s := &Server{svc: svc}
	mux := http.NewServeMux()

	mux.HandleFunc("GET /health", s.health)

	mux.HandleFunc("POST /admin/projects", s.createProject)
	mux.HandleFunc("POST /admin/users", s.createUser)
	mux.HandleFunc("POST /admin/users/{userID}/deactivate", s.deactivateUser)
	mux.HandleFunc("POST /admin/grants", s.setGrant)

	mux.HandleFunc("POST /projects/{projectID}/documents", s.upload)
	mux.HandleFunc("POST /documents/{docID}/versions", s.uploadVersion)
	mux.HandleFunc("POST /documents/{docID}/withdraw", s.withdraw)

	mux.HandleFunc("POST /projects/{projectID}/requests/import", s.importRequests)
	mux.HandleFunc("GET /projects/{projectID}/requests", s.listRequests)
	mux.HandleFunc("GET /projects/{projectID}/pending-review", s.pendingReview)
	mux.HandleFunc("POST /requests/{requestID}/respond", s.respond)
	mux.HandleFunc("POST /requests/{requestID}/review", s.review)
	mux.HandleFunc("POST /requests/{requestID}/extend-deadline", s.extendDeadline)
	mux.HandleFunc("POST /requests/{requestID}/scope", s.changeScope)
	mux.HandleFunc("GET /requests/{requestID}/impact", s.impactChain)
	mux.HandleFunc("POST /requests/{requestID}/evidence", s.citeEvidence)
	mux.HandleFunc("GET /evidence/{evidenceID}", s.accessEvidence)

	mux.HandleFunc("POST /links", s.issueLink)
	mux.HandleFunc("GET /links/{token}", s.accessByLink)
	mux.HandleFunc("GET /download", s.download)

	mux.HandleFunc("GET /records", s.listRecords)
	mux.HandleFunc("POST /projects/{projectID}/copy", s.copyProject)

	return logRecover(mux)
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// ---------- 请求体 ----------

type projectReq struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	Country string `json:"country"`
}

type userReq struct {
	ID      string   `json:"id"`
	Name    string   `json:"name"`
	Teams   []string `json:"teams"`
	IsAdmin bool     `json:"is_admin"`
}

type grantReq struct {
	UserID    string   `json:"user_id"`
	ProjectID string   `json:"project_id"`
	Teams     []string `json:"teams"`
	MaxLevel  Level    `json:"max_level"`
}

type uploadReq struct {
	DocID   string `json:"doc_id,omitempty"`
	Title   string `json:"title"`
	Team    string `json:"team"`
	Level   Level  `json:"level"`
	Content []byte `json:"content"` // base64 由 JSON 自动处理
	Summary string `json:"summary"`
}

type respondReq struct {
	DocID   string `json:"doc_id"`
	Version int    `json:"version"`
	Summary string `json:"summary"`
}

type reviewReq struct {
	Conclusion string `json:"conclusion"`
	Note       string `json:"note"`
}

type deadlineReq struct {
	DeadlineLocal string `json:"deadline_local"`
	DeadlineZone  string `json:"deadline_zone"`
	Note          string `json:"note"`
}

type scopeReq struct {
	Team    string `json:"team"`
	Country string `json:"country"`
	Level   Level  `json:"level"`
	Note    string `json:"note"`
}

type evidenceReq struct {
	DocID   string `json:"doc_id"`
	Version int    `json:"version"`
	Note    string `json:"note"`
}

type linkReq struct {
	ViewerID  string          `json:"viewer_id"`
	ProjectID string          `json:"project_id"`
	DocID     string          `json:"doc_id"`
	Version   int             `json:"version"`
	Purpose   string          `json:"purpose"`
	TTLHours  float64         `json:"ttl_hours"`
	Watermark WatermarkPolicy `json:"watermark"`
}

type copyReq struct {
	NewID      string `json:"new_id"`
	NewName    string `json:"new_name"`
	NewCountry string `json:"new_country"`
}

// ---------- handlers ----------

func (s *Server) createProject(w http.ResponseWriter, r *http.Request) {
	var b projectReq
	if !decode(w, r, &b) {
		return
	}
	p, err := s.svc.CreateProject(b.ID, b.Name, b.Country, time.Now().UTC())
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, p)
}

func (s *Server) createUser(w http.ResponseWriter, r *http.Request) {
	var b userReq
	if !decode(w, r, &b) {
		return
	}
	if err := s.svc.CreateUser(b.ID, b.Name, b.Teams, b.IsAdmin); err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]string{"id": b.ID})
}

func (s *Server) deactivateUser(w http.ResponseWriter, r *http.Request) {
	if err := s.svc.DeactivateUser(r.PathValue("userID"), time.Now().UTC()); err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "deactivated"})
}

func (s *Server) setGrant(w http.ResponseWriter, r *http.Request) {
	var b grantReq
	if !decode(w, r, &b) {
		return
	}
	actor := actorOf(r)
	g, err := s.svc.SetGrant(b.UserID, b.ProjectID, b.Teams, b.MaxLevel, actor, time.Now().UTC())
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, g)
}

func (s *Server) upload(w http.ResponseWriter, r *http.Request) {
	var b uploadReq
	if !decode(w, r, &b) {
		return
	}
	res, err := s.svc.UploadDocument(r.PathValue("projectID"), b.DocID, b.Title, b.Team, b.Level,
		b.Content, b.Summary, actorOf(r), time.Now().UTC())
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, res)
}

func (s *Server) uploadVersion(w http.ResponseWriter, r *http.Request) {
	// 新版本沿用文档既有的标题/团队/密级，项目归属必须显式携带，
	// 避免仅凭 docID 跨项目引用。
	var b struct {
		ProjectID string `json:"project_id"`
		Content   []byte `json:"content"`
		Summary   string `json:"summary"`
	}
	if !decode(w, r, &b) {
		return
	}
	docID := r.PathValue("docID")
	res, err := s.svc.UploadVersion(b.ProjectID, docID, b.Content, b.Summary,
		actorOf(r), time.Now().UTC())
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, res)
}

func (s *Server) withdraw(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Reason string `json:"reason"`
	}
	if !decode(w, r, &b) {
		return
	}
	if err := s.svc.WithdrawDocument(r.PathValue("docID"), b.Reason, actorOf(r), time.Now().UTC()); err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "withdrawn"})
}

func (s *Server) importRequests(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Items []ImportItem `json:"items"`
	}
	if !decode(w, r, &b) {
		return
	}
	rep, err := s.svc.ImportRequests(r.PathValue("projectID"), b.Items, time.Now().UTC())
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rep)
}

func (s *Server) listRequests(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	f := RequestFilter{
		ProjectID: r.PathValue("projectID"),
		Team:      q.Get("team"),
		Country:   q.Get("country"),
		Status:    q.Get("status"),
	}
	writeJSON(w, http.StatusOK, s.svc.ListRequests(f))
}

func (s *Server) pendingReview(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.svc.PendingReview(r.PathValue("projectID")))
}

func (s *Server) respond(w http.ResponseWriter, r *http.Request) {
	var b respondReq
	if !decode(w, r, &b) {
		return
	}
	if err := s.svc.RespondRequest(r.PathValue("requestID"), b.DocID, b.Version, b.Summary,
		actorOf(r), time.Now().UTC()); err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": StatusPendingReview})
}

func (s *Server) review(w http.ResponseWriter, r *http.Request) {
	var b reviewReq
	if !decode(w, r, &b) {
		return
	}
	if err := s.svc.SubmitReview(r.PathValue("requestID"), actorOf(r), b.Conclusion, b.Note, time.Now().UTC()); err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "reviewed"})
}

func (s *Server) extendDeadline(w http.ResponseWriter, r *http.Request) {
	var b deadlineReq
	if !decode(w, r, &b) {
		return
	}
	err := s.svc.ExtendDeadline(r.PathValue("requestID"), actorOf(r), b.DeadlineLocal, b.DeadlineZone, b.Note, time.Now().UTC())
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "deadline_extended"})
}

func (s *Server) changeScope(w http.ResponseWriter, r *http.Request) {
	var b scopeReq
	if !decode(w, r, &b) {
		return
	}
	err := s.svc.ChangeScope(r.PathValue("requestID"), actorOf(r), b.Team, b.Country, b.Level, b.Note, time.Now().UTC())
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "scope_changed"})
}

func (s *Server) impactChain(w http.ResponseWriter, r *http.Request) {
	chain, err := s.svc.ImpactChain(r.PathValue("requestID"))
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, chain)
}

func (s *Server) citeEvidence(w http.ResponseWriter, r *http.Request) {
	var b evidenceReq
	if !decode(w, r, &b) {
		return
	}
	ev, err := s.svc.CiteEvidence(r.PathValue("requestID"), b.DocID, b.Version, actorOf(r), b.Note, time.Now().UTC())
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, ev)
}

func (s *Server) accessEvidence(w http.ResponseWriter, r *http.Request) {
	res, ae := s.svc.AccessEvidence(r.PathValue("evidenceID"), viewerOf(r), time.Now().UTC())
	if ae != nil {
		writeAccessError(w, ae)
		return
	}
	writeAccessResult(w, res)
}

func (s *Server) issueLink(w http.ResponseWriter, r *http.Request) {
	var b linkReq
	if !decode(w, r, &b) {
		return
	}
	ttl := time.Duration(b.TTLHours * float64(time.Hour))
	link, err := s.svc.IssueLink(IssueLinkParams{
		ActorID: actorOf(r), ViewerID: b.ViewerID, ProjectID: b.ProjectID, DocID: b.DocID,
		Version: b.Version, Purpose: b.Purpose, TTL: ttl, Watermark: b.Watermark,
	}, time.Now().UTC())
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, link)
}

func (s *Server) accessByLink(w http.ResponseWriter, r *http.Request) {
	res, ae := s.svc.AccessByLink(r.PathValue("token"), viewerOf(r), time.Now().UTC())
	if ae != nil {
		writeAccessError(w, ae)
		return
	}
	writeAccessResult(w, res)
}

func (s *Server) download(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	version := 0
	if v := q.Get("version"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "version 必须是整数"})
			return
		}
		version = n
	}
	res, ae := s.svc.Download(viewerOf(r), q.Get("project"), q.Get("doc"), version, q.Get("purpose"), time.Now().UTC())
	if ae != nil {
		writeAccessError(w, ae)
		return
	}
	writeAccessResult(w, res)
}

func (s *Server) listRecords(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	f := RecordFilter{ProjectID: q.Get("project"), ViewerID: q.Get("viewer"), DocID: q.Get("doc"), Decision: q.Get("decision")}
	writeJSON(w, http.StatusOK, s.svc.ListRecords(f))
}

func (s *Server) copyProject(w http.ResponseWriter, r *http.Request) {
	var b copyReq
	if !decode(w, r, &b) {
		return
	}
	rep, err := s.svc.CopyProject(r.PathValue("projectID"), b.NewID, b.NewName, b.NewCountry, actorOf(r), time.Now().UTC())
	if err != nil {
		writeDomainError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, rep)
}

// ---------- 辅助 ----------

func actorOf(r *http.Request) string {
	if v := r.Header.Get("X-Actor"); v != "" {
		return v
	}
	return r.Header.Get("X-Viewer")
}

func viewerOf(r *http.Request) string {
	if v := r.Header.Get("X-Viewer"); v != "" {
		return v
	}
	return r.Header.Get("X-Actor")
}

func decode(w http.ResponseWriter, r *http.Request, dst any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 32<<20)
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(dst); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "请求体不是合法 JSON: " + err.Error()})
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// writeAccessResult 成功时返回内容（base64）、版本、哈希与水印；
// 失败路径完全不经过这里。
func writeAccessResult(w http.ResponseWriter, res *AccessResult) {
	writeJSON(w, http.StatusOK, map[string]any{
		"doc_id":       res.DocID,
		"version":      res.Version,
		"content_hash": res.ContentHash,
		"content":      res.Content,
		"watermark":    res.Watermark,
		"purpose":      res.Purpose,
	})
}

// writeAccessError 把访问拒绝映射为受控响应：
// 只给原因码与面向用户的受控提示，绝不携带文件内容、内部路径或堆栈。
func writeAccessError(w http.ResponseWriter, ae *AccessError) {
	status := http.StatusForbidden
	switch ae.Reason {
	case ReasonExpired, ReasonWithdrawn:
		status = http.StatusGone // 410：资源存在但当前不可得
	case ReasonInvalid:
		status = http.StatusNotFound
	case ReasonInactive:
		status = http.StatusForbidden
	case ReasonForbidden:
		status = http.StatusForbidden
	}
	writeJSON(w, status, map[string]string{"reason": ae.Reason, "message": ae.Message})
}

func writeDomainError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, ErrNotFound):
		writeJSON(w, http.StatusNotFound, map[string]string{"error": err.Error()})
	case errors.Is(err, ErrConflict):
		writeJSON(w, http.StatusConflict, map[string]string{"error": err.Error()})
	case errors.Is(err, ErrValidation):
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
	default:
		var ae *AccessError
		if errors.As(err, &ae) {
			writeAccessError(w, ae)
			return
		}
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
	}
}

// logRecover 是最薄的一层兜底：panic 不泄漏细节，只回 500。
func logRecover(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				if !strings.HasPrefix(r.URL.Path, "/health") {
					// 真实部署写结构化日志；这里避免向调用方泄漏内部信息。
					_ = rec
				}
				writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
			}
		}()
		h.ServeHTTP(w, r)
	})
}
