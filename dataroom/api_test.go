package dataroom

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"
)

func newTestServer(t *testing.T) (http.Handler, *Service) {
	t.Helper()
	svc := New("")
	return NewServer(svc), svc
}

// do 返回状态码与原始响应体。
func do(t *testing.T, h http.Handler, method, path, actor, viewer string, body any) (int, []byte) {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("编码请求体失败: %v", err)
		}
		rdr = bytes.NewReader(b)
	}
	req := httptest.NewRequest(method, path, rdr)
	if actor != "" {
		req.Header.Set("X-Actor", actor)
	}
	if viewer != "" {
		req.Header.Set("X-Viewer", viewer)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec.Code, rec.Body.Bytes()
}

func jsonMap(t *testing.T, b []byte) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("响应不是 JSON 对象: %v (%s)", err, b)
	}
	return m
}

func jsonArr(t *testing.T, b []byte) []any {
	t.Helper()
	var a []any
	if err := json.Unmarshal(b, &a); err != nil {
		t.Fatalf("响应不是 JSON 数组: %v (%s)", err, b)
	}
	return a
}

func TestHTTPEndToEndControlledAccess(t *testing.T) {
	h, _ := newTestServer(t)

	// 初始化项目、用户、授权
	if code, _ := do(t, h, "POST", "/admin/projects", "admin", "",
		map[string]string{"id": "P1", "name": "目标公司A", "country": "JP"}); code != 201 {
		t.Fatalf("建项目应 201, got %d", code)
	}
	for _, u := range []map[string]any{
		{"id": "admin", "name": "管理员", "teams": []string{}, "is_admin": true},
		{"id": "fin", "name": "财务", "teams": []string{TeamFinance}, "is_admin": false},
		{"id": "leg", "name": "法务", "teams": []string{TeamLegal}, "is_admin": false},
	} {
		if code, b := do(t, h, "POST", "/admin/users", "admin", "", u); code != 201 {
			t.Fatalf("建用户 %v 应 201, got %d %s", u["id"], code, b)
		}
	}
	if code, b := do(t, h, "POST", "/admin/grants", "admin", "", map[string]any{
		"user_id": "fin", "project_id": "P1", "teams": []string{TeamFinance}, "max_level": 2,
	}); code != 200 {
		t.Fatalf("授权 fin 失败 code=%d %s", code, b)
	}
	if code, b := do(t, h, "POST", "/admin/grants", "admin", "", map[string]any{
		"user_id": "leg", "project_id": "P1", "teams": []string{TeamLegal}, "max_level": 3,
	}); code != 200 {
		t.Fatalf("授权 leg 失败 code=%d %s", code, b)
	}

	// 上传 restricted 工资单
	code, b := do(t, h, "POST", "/projects/P1/documents", "admin", "", map[string]any{
		"doc_id": "payroll", "title": "工资单", "team": TeamFinance, "level": 3,
		"content": []byte("salary v1"), "summary": "v1",
	})
	if code != 201 {
		t.Fatalf("上传应 201, got %d %s", code, b)
	}

	// 最小权限：法务拉取财务 restricted 工资单 → 403 受控错误，无内容
	code, b = do(t, h, "GET", "/download?project=P1&doc=payroll&purpose=dd", "", "leg", nil)
	denied := jsonMap(t, b)
	if code != http.StatusForbidden || denied["reason"] != ReasonForbidden {
		t.Fatalf("越权下载应 403/FORBIDDEN, got %d %v", code, denied)
	}
	if _, leak := denied["content"]; leak {
		t.Fatalf("拒绝响应不得包含文件内容")
	}

	// 管理员给法务签发链接：签发成功（管理员有权），但法务访问时仍被最小权限拦截。
	code, b = do(t, h, "POST", "/links", "admin", "", map[string]any{
		"viewer_id": "leg", "project_id": "P1", "doc_id": "payroll", "purpose": "工资核查", "ttl_hours": 1,
	})
	if code != 201 {
		t.Fatalf("管理员应能签发链接, got %d %s", code, b)
	}
	token := jsonMap(t, b)["token"].(string)
	code, b = do(t, h, "GET", "/links/"+token, "", "leg", nil)
	accessDenied := jsonMap(t, b)
	if code != http.StatusForbidden || accessDenied["reason"] != ReasonForbidden {
		t.Fatalf("法务凭链接仍应被最小权限拦截, got %d %v", code, accessDenied)
	}
	if msg, _ := accessDenied["message"].(string); msg == "" {
		t.Fatalf("应返回面向用户的受控提示")
	}

	// 管理员自己签发一张立即过期的链接 → 410 受控提示，无内容
	code, b = do(t, h, "POST", "/links", "admin", "", map[string]any{
		"viewer_id": "admin", "project_id": "P1", "doc_id": "payroll",
		"purpose": "审计抽查", "ttl_hours": 0.0000001,
		"watermark": map[string]any{"enabled": true, "text": "机密"},
	})
	if code != 201 {
		t.Fatalf("签发短链接失败: %d", code)
	}
	shortToken := jsonMap(t, b)["token"].(string)
	time.Sleep(5 * time.Millisecond)
	code, b = do(t, h, "GET", "/links/"+shortToken, "", "admin", nil)
	expired := jsonMap(t, b)
	if code != http.StatusGone || expired["reason"] != ReasonExpired {
		t.Fatalf("过期链接应 410/EXPIRED, got %d %v", code, expired)
	}
	if _, leak := expired["content"]; leak {
		t.Fatalf("过期响应不得泄露内容")
	}

	// 无效 token → 404/INVALID
	code, b = do(t, h, "GET", "/links/nope", "", "admin", nil)
	bad := jsonMap(t, b)
	if code != http.StatusNotFound || bad["reason"] != ReasonInvalid {
		t.Fatalf("无效链接应 404/INVALID, got %d", code)
	}

	// 审计可查：本轮拒绝全部留痕
	code, b = do(t, h, "GET", "/records?project=P1&decision=DENIED", "admin", "", nil)
	if code != 200 {
		t.Fatalf("审计查询失败 %d", code)
	}
	recs := jsonArr(t, b)
	if len(recs) != 3 {
		t.Fatalf("本轮应有 3 条拒绝记录(越权下载/链接访问/过期), got %d", len(recs))
	}
}

func TestHTTPEvidenceVersionAndCopyFlow(t *testing.T) {
	h, svc := newTestServer(t)
	now := time.Date(2026, 9, 18, 0, 0, 0, 0, time.UTC)
	if err := seedForHTTP(svc, now); err != nil {
		t.Fatal(err)
	}

	// 导入法务合同核查请求
	code, b := do(t, h, "POST", "/projects/P1/requests/import", "admin", "", map[string]any{
		"items": []map[string]any{{
			"client_key": "HTTP-1", "title": "合同核查", "team": TeamLegal, "country": "JP", "level": 2,
			"deadline_local": "2026-10-01T09:00:00", "deadline_zone": "Asia/Tokyo",
		}},
	})
	if code != 200 {
		t.Fatalf("导入失败 %d %s", code, b)
	}
	imp := jsonMap(t, b)
	reqID := imp["created"].([]any)[0].(string)

	// 法务以合同 v1 回复 → 待复核
	if code, b := do(t, h, "POST", "/requests/"+reqID+"/respond", "leg", "", map[string]any{
		"doc_id": "contract-draft", "version": 1, "summary": "合同v1",
	}); code != 200 {
		t.Fatalf("回复失败 %d %s", code, b)
	}
	code, b = do(t, h, "GET", "/projects/P1/pending-review", "admin", "", nil)
	if code != 200 || len(jsonArr(t, b)) != 1 {
		t.Fatalf("应存在 1 个待复核项, code=%d body=%s", code, b)
	}

	// 引用 v1 为证据，随后上传 v2
	code, b = do(t, h, "POST", "/requests/"+reqID+"/evidence", "admin", "", map[string]any{
		"doc_id": "contract-draft", "version": 1, "note": "复核证据",
	})
	if code != 201 {
		t.Fatalf("引用证据失败 %d %s", code, b)
	}
	evID := jsonMap(t, b)["id"].(string)
	if code, b := do(t, h, "POST", "/documents/contract-draft/versions", "admin", "", map[string]any{
		"project_id": "P1", "content": []byte("contract clauses v2"), "summary": "v2",
	}); code != 201 {
		t.Fatalf("追加 v2 失败 %d %s", code, b)
	}
	// 证据仍解析到 v1
	code, b = do(t, h, "GET", "/evidence/"+evID, "admin", "", nil)
	if code != 200 {
		t.Fatalf("证据访问失败 %d %s", code, b)
	}
	got := jsonMap(t, b)
	// JSON 里 []byte 以 base64 返回，解码后必须仍是被钉住的 v1 内容。
	payload, err := base64.StdEncoding.DecodeString(got["content"].(string))
	if err != nil {
		t.Fatalf("content 不是合法 base64: %v", err)
	}
	if string(payload) != "contract clauses v1" {
		t.Fatalf("证据应钉在 v1, got content=%q", payload)
	}

	// 复制项目：法务在新项目默认无权（403）；管理员可取且哈希一致
	code, b = do(t, h, "POST", "/projects/P1/copy", "admin", "", map[string]any{
		"new_id": "P2", "new_name": "德国扩展", "new_country": "DE",
	})
	if code != 201 {
		t.Fatalf("复制失败 %d %s", code, b)
	}
	cp := jsonMap(t, b)
	newDoc := cp["documents"].(map[string]any)["contract-draft"].(string)
	if code, b := do(t, h, "GET", "/download?project=P2&doc="+newDoc+"&version=1", "", "leg", nil); code != 403 {
		t.Fatalf("复制不得携带授权, got %d %s", code, b)
	}
	code, b = do(t, h, "GET", "/download?project=P2&doc="+newDoc+"&version=1", "", "admin", nil)
	if code != 200 {
		t.Fatalf("管理员应能下载复制件, got %d", code)
	}
	dst := jsonMap(t, b)
	code, b = do(t, h, "GET", "/download?project=P1&doc=contract-draft&version=1", "", "admin", nil)
	src := jsonMap(t, b)
	if dst["content_hash"] != src["content_hash"] {
		t.Fatalf("复制后版本哈希不一致: %v vs %v", dst["content_hash"], src["content_hash"])
	}
}

// seedForHTTP 直接用领域 API 铺底，避免 HTTP 用例重复初始化噪音。
func seedForHTTP(s *Service, now time.Time) error {
	if _, err := s.CreateProject("P1", "目标公司A", "JP", now); err != nil {
		return err
	}
	if err := s.CreateUser("admin", "管理员", nil, true); err != nil {
		return err
	}
	if err := s.CreateUser("leg", "法务", []string{TeamLegal}, false); err != nil {
		return err
	}
	if _, err := s.SetGrant("leg", "P1", []string{TeamLegal}, LevelConfidential, "admin", now); err != nil {
		return err
	}
	if _, err := s.UploadDocument("P1", "contract-draft", "合同草案", TeamLegal, LevelConfidential,
		[]byte("contract clauses v1"), "v1", "admin", now); err != nil {
		return err
	}
	return nil
}

// 重启后 HTTP 层同样能查到待复核项与审计。
func TestHTTPServerRestart(t *testing.T) {
	path := filepath.Join(t.TempDir(), "dr.json")
	now := time.Date(2026, 9, 18, 0, 0, 0, 0, time.UTC)
	{
		svc := New(path)
		if err := seedForHTTP(svc, now); err != nil {
			t.Fatal(err)
		}
		rep, err := svc.ImportRequests("P1", []ImportItem{{
			ClientKey: "R-1", Title: "合同", Team: TeamLegal, Country: "JP", Level: LevelConfidential,
			DeadlineLocal: "2026-10-01T09:00:00", DeadlineZone: "Asia/Tokyo",
		}}, now)
		if err != nil {
			t.Fatal(err)
		}
		if err := svc.RespondRequest(rep.Created[0], "contract-draft", 1, "v1", "admin", now); err != nil {
			t.Fatal(err)
		}
		// 重启前产生一条访问记录，重启后必须仍可查询。
		if _, ae := svc.Download("admin", "P1", "contract-draft", 1, "重启前下载", now); ae != nil {
			t.Fatal(ae)
		}
	}
	h2 := NewServer(New(path))
	code, b := do(t, h2, "GET", "/projects/P1/pending-review", "admin", "", nil)
	if code != 200 || len(jsonArr(t, b)) != 1 {
		t.Fatalf("重启后待复核项应仍为 1 条, code=%d body=%s", code, b)
	}
	code, b = do(t, h2, "GET", "/records?project=P1", "admin", "", nil)
	if code != 200 || len(jsonArr(t, b)) == 0 {
		t.Fatalf("重启后访问记录应可查询, code=%d", code)
	}
}
