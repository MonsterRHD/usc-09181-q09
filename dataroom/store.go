package dataroom

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

// snapshot 是磁盘上的持久化格式。访问记录、待复核请求等全部落盘，
// 进程重启后原样恢复。
type snapshot struct {
	Users      map[string]*User             `json:"users"`
	Grants     map[string]*Grant            `json:"grants"`
	Projects   map[string]*Project          `json:"projects"`
	Documents  map[string]*Document         `json:"documents"`
	Requests   map[string]*Request          `json:"requests"`
	KeyIndex   map[string]map[string]string `json:"key_index"` // projectID -> clientKey -> requestID
	Evidences  map[string]*Evidence         `json:"evidences"`
	Links      map[string]*AccessLink       `json:"links"`
	Records    []AccessRecord               `json:"records"`
	Impacts    []ImpactEvent                `json:"impacts"`
	NotifByKey map[string]*Notification     `json:"notifications"`
	Counters   map[string]int               `json:"counters"`
}

type store struct {
	mu sync.RWMutex

	users      map[string]*User
	grants     map[string]*Grant
	projects   map[string]*Project
	documents  map[string]*Document
	requests   map[string]*Request
	keyIndex   map[string]map[string]string
	evidences  map[string]*Evidence
	links      map[string]*AccessLink
	records    []AccessRecord
	impacts    []ImpactEvent
	notifByKey map[string]*Notification
	counters   map[string]int

	persistPath string
}

func newStore(path string) *store {
	s := &store{
		users:      map[string]*User{},
		grants:     map[string]*Grant{},
		projects:   map[string]*Project{},
		documents:  map[string]*Document{},
		requests:   map[string]*Request{},
		keyIndex:   map[string]map[string]string{},
		evidences:  map[string]*Evidence{},
		links:      map[string]*AccessLink{},
		notifByKey: map[string]*Notification{},
		counters:   map[string]int{},
	}
	if path != "" {
		s.persistPath = path
		s.load()
	}
	return s
}

func grantKey(userID, projectID string) string { return userID + "@" + projectID }

// nextID 必须在持锁状态下调用。
func (s *store) nextID(kind string) string {
	s.counters[kind]++
	return fmt.Sprintf("%s_%d", kind, s.counters[kind])
}

func (s *store) load() {
	b, err := os.ReadFile(s.persistPath)
	if err != nil {
		return // 首次运行：空库
	}
	var snap snapshot
	if err := json.Unmarshal(b, &snap); err != nil {
		// 持久化文件损坏不应让服务起不来；按空库启动，原文件保留供排查。
		return
	}
	if snap.Users != nil {
		s.users = snap.Users
	}
	for k, g := range snap.Grants {
		s.grants[k] = g
	}
	for k, v := range snap.Projects {
		s.projects[k] = v
	}
	for k, v := range snap.Documents {
		s.documents[k] = v
	}
	for k, v := range snap.Requests {
		s.requests[k] = v
	}
	s.keyIndex = snap.KeyIndex
	if s.keyIndex == nil {
		s.keyIndex = map[string]map[string]string{}
	}
	if snap.Evidences != nil {
		s.evidences = snap.Evidences
	}
	if snap.Links != nil {
		s.links = snap.Links
	}
	s.records = snap.Records
	s.impacts = snap.Impacts
	if snap.NotifByKey != nil {
		s.notifByKey = snap.NotifByKey
	}
	s.counters = snap.Counters
	if s.counters == nil {
		s.counters = map[string]int{}
	}
}

// save 在持锁状态下调用，原子替换文件，避免崩溃留下半截 JSON。
func (s *store) save() {
	if s.persistPath == "" {
		return
	}
	snap := snapshot{
		Users:      s.users,
		Grants:     s.grants,
		Projects:   s.projects,
		Documents:  s.documents,
		Requests:   s.requests,
		KeyIndex:   s.keyIndex,
		Evidences:  s.evidences,
		Links:      s.links,
		Records:    s.records,
		Impacts:    s.impacts,
		NotifByKey: s.notifByKey,
		Counters:   s.counters,
	}
	b, err := json.MarshalIndent(&snap, "", "  ")
	if err != nil {
		return
	}
	dir := filepath.Dir(s.persistPath)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return
	}
	tmp := s.persistPath + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return
	}
	_ = os.Rename(tmp, filepath.Join(dir, filepath.Base(s.persistPath)))
}
