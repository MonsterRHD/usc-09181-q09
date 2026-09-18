package dataroom

import "time"

// 保密级别：数字越大越敏感。工资单、合同草案属于 Restricted。
type Level int

const (
	LevelInternal     Level = 1
	LevelConfidential Level = 2
	LevelRestricted   Level = 3
)

func (l Level) String() string {
	switch l {
	case LevelInternal:
		return "internal"
	case LevelConfidential:
		return "confidential"
	case LevelRestricted:
		return "restricted"
	default:
		return "unknown"
	}
}

// 尽调职能团队：各团队维护各自的清单，默认互不可见。
const (
	TeamFinance = "finance"
	TeamLegal   = "legal"
	TeamTax     = "tax"
)

func ValidTeam(t string) bool {
	return t == TeamFinance || t == TeamLegal || t == TeamTax
}

// 请求生命周期。Respond 之后进入 PendingReview，等待复核结论。
const (
	StatusOpen          = "OPEN"
	StatusPendingReview = "PENDING_REVIEW"
	StatusReviewed      = "REVIEWED"
	StatusChangesNeeded = "CHANGES_REQUESTED"
)

// 审计决定与拒绝原因。
const (
	DecisionAllowed = "ALLOWED"
	DecisionDenied  = "DENIED"

	ReasonInvalid   = "INVALID_LINK"
	ReasonExpired   = "EXPIRED"
	ReasonInactive  = "INACTIVE"
	ReasonForbidden = "FORBIDDEN"
	ReasonWithdrawn = "WITHDRAWN"
)

// 影响链事件类型。
const (
	KindScopeChanged  = "SCOPE_CHANGED"
	KindDeadlineMoved = "DEADLINE_EXTENDED"
	KindWithdrawn     = "DOCUMENT_WITHDRAWN"
	KindCopied        = "PROJECT_COPIED"
)

const (
	ActionDownload = "DOWNLOAD"
	ActionEvidence = "EVIDENCE_VIEW"
)

type User struct {
	ID      string   `json:"id"`
	Name    string   `json:"name"`
	Teams   []string `json:"teams"`
	IsAdmin bool     `json:"is_admin"`
	Active  bool     `json:"active"`
}

// Grant 是某用户在某项目上的授权：职能范围 + 最高可见密级。
// 管理员调整 Grant 后，下载时重新判定，因此对“尚未下载”的内容立即生效。
type Grant struct {
	UserID    string   `json:"user_id"`
	ProjectID string   `json:"project_id"`
	Teams     []string `json:"teams"`
	MaxLevel  Level    `json:"max_level"`
}

type Project struct {
	ID        string    `json:"id"`
	Name      string    `json:"name"`
	Country   string    `json:"country"`
	CreatedAt time.Time `json:"created_at"`
}

// DocVersion 一旦写入即不可变：新版本只追加，不能覆盖旧版本。
type DocVersion struct {
	Number     int       `json:"number"`
	Content    []byte    `json:"content"`
	Hash       string    `json:"hash"`
	Size       int       `json:"size"`
	Summary    string    `json:"summary"` // 上传摘要
	UploadedBy string    `json:"uploaded_by"`
	UploadedAt time.Time `json:"uploaded_at"`
}

type Document struct {
	ID        string       `json:"id"`
	ProjectID string       `json:"project_id"`
	Title     string       `json:"title"`
	Team      string       `json:"team"`
	Level     Level        `json:"level"`
	Versions  []DocVersion `json:"versions"`
	// 供应商撤回：内容字节保留（证据/审计需要），但任何新的访问都被拒绝。
	Withdrawn      bool       `json:"withdrawn"`
	WithdrawnAt    *time.Time `json:"withdrawn_at,omitempty"`
	WithdrawReason string     `json:"withdraw_reason,omitempty"`
}

type ReviewConclusion struct {
	ReviewerID string    `json:"reviewer_id"`
	Conclusion string    `json:"conclusion"` // APPROVED / CHANGES_REQUESTED
	Note       string    `json:"note"`
	At         time.Time `json:"at"`
}

type Request struct {
	ID          string `json:"id"`
	ProjectID   string `json:"project_id"`
	ClientKey   string `json:"client_key"` // 批量导入时的外部唯一键
	Title       string `json:"title"`
	Team        string `json:"team"`
	Country     string `json:"country"`
	Description string `json:"description"`
	Level       Level  `json:"level"`
	Status      string `json:"status"`

	// 截止时间用“当地挂钟时间 + IANA 时区”表达，
	// 不同时区的协作者看到的是同一个绝对时刻。
	DeadlineLocal string `json:"deadline_local"` // 2006-01-02T15:04:05
	DeadlineZone  string `json:"deadline_zone"`  // Asia/Tokyo

	// 回复钉住具体的文件版本，而不是“文件的最新版”。
	RespDocID   string     `json:"resp_doc_id,omitempty"`
	RespVersion int        `json:"resp_version,omitempty"`
	RespSummary string     `json:"resp_summary,omitempty"`
	RespondedAt *time.Time `json:"responded_at,omitempty"`

	Review *ReviewConclusion `json:"review,omitempty"`
}

// Evidence 是被复核结论引用的证据：创建时把文件版本号与内容哈希一起钉死。
// 之后文件再传新版本，证据仍然解析到被引用的那一版。
type Evidence struct {
	ID          string    `json:"id"`
	ProjectID   string    `json:"project_id"`
	RequestID   string    `json:"request_id"`
	DocID       string    `json:"doc_id"`
	Version     int       `json:"version"`
	ContentHash string    `json:"content_hash"`
	CitedBy     string    `json:"cited_by"`
	Note        string    `json:"note"`
	At          time.Time `json:"at"`
}

type WatermarkPolicy struct {
	Enabled bool   `json:"enabled"`
	Text    string `json:"text"`
}

// AccessLink 是受控访问链接：绑定查看者、用途、有效期与水印策略。
type AccessLink struct {
	Token     string          `json:"token"`
	ViewerID  string          `json:"viewer_id"`
	ProjectID string          `json:"project_id"`
	DocID     string          `json:"doc_id"`
	Version   int             `json:"version"`
	Purpose   string          `json:"purpose"`
	ExpiresAt time.Time       `json:"expires_at"`
	Watermark WatermarkPolicy `json:"watermark"`
	CreatedAt time.Time       `json:"created_at"`
}

// AccessRecord 只追加、不提供删除接口；成员离职或权限收回都不会抹去审计。
type AccessRecord struct {
	ID          string    `json:"id"`
	At          time.Time `json:"at"`
	Token       string    `json:"token,omitempty"`
	ViewerID    string    `json:"viewer_id,omitempty"`
	ActorID     string    `json:"actor_id,omitempty"`
	ProjectID   string    `json:"project_id,omitempty"`
	DocID       string    `json:"doc_id,omitempty"`
	Version     int       `json:"version,omitempty"`
	Action      string    `json:"action"`
	Decision    string    `json:"decision"`
	Reason      string    `json:"reason,omitempty"`
	Purpose     string    `json:"purpose,omitempty"`
	Watermark   string    `json:"watermark,omitempty"`
	ContentHash string    `json:"content_hash,omitempty"`
	ServedHash  string    `json:"served_hash,omitempty"`
}

// ImpactEvent 组成影响链：尽调范围变更、截止延期、供应商撤回、项目复制。
// ParentID 指向上一事件，形成按请求可追溯的链条。
type ImpactEvent struct {
	ID        string         `json:"id"`
	ProjectID string         `json:"project_id"`
	Kind      string         `json:"kind"`
	RefType   string         `json:"ref_type"` // request / document / project
	RefID     string         `json:"ref_id"`
	Actor     string         `json:"actor"`
	Note      string         `json:"note,omitempty"`
	ParentID  string         `json:"parent_id,omitempty"`
	At        time.Time      `json:"at"`
	Detail    map[string]any `json:"detail,omitempty"`
}

// Notification 以 Key 去重，批量导入重复执行不会产生重复通知。
type Notification struct {
	ID     string    `json:"id"`
	UserID string    `json:"user_id"`
	Key    string    `json:"key"`
	Title  string    `json:"title"`
	Body   string    `json:"body"`
	At     time.Time `json:"at"`
	Read   bool      `json:"read"`
}
