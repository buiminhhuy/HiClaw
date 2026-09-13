package server

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	v1beta1 "github.com/agentscope-ai/AgentTeams/agentteams-controller/api/v1beta1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

// Opt-in compare-and-set on update.
//
// Two people editing the same Worker in a console is not an exotic case, and
// without this the second save silently overwrites the first from a form that
// was rendered before the first edit existed. Nobody is told; the first edit
// simply is not there any more.

func newIfMatchRig(t *testing.T) (*ResourceHandler, string) {
	t.Helper()
	scheme := newServerTestScheme(t)
	worker := &v1beta1.Worker{
		ObjectMeta: metav1.ObjectMeta{Name: "alice", Namespace: "default"},
		Spec:       v1beta1.WorkerSpec{Model: "claude", Soul: "original"},
	}
	k8s := fake.NewClientBuilder().WithScheme(scheme).WithObjects(worker).Build()
	h := NewResourceHandler(k8s, "default", nil, "", nil)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/workers/alice", nil)
	req.SetPathValue("name", "alice")
	rec := httptest.NewRecorder()
	h.GetWorker(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("setup GET failed: %d %s", rec.Code, rec.Body)
	}
	var resp WorkerResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if resp.ResourceVersion == "" {
		t.Fatal("GET did not return a resourceVersion — a client has nothing to send back")
	}
	return h, resp.ResourceVersion
}

func putWorkerWithIfMatch(t *testing.T, h *ResourceHandler, name, ifMatch, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPut, "/api/v1/workers/"+name, bytes.NewReader([]byte(body)))
	req.SetPathValue("name", name)
	if ifMatch != "" {
		req.Header.Set("If-Match", ifMatch)
	}
	rec := httptest.NewRecorder()
	h.UpdateWorker(rec, req)
	return rec
}

func TestUpdateWithMatchingIfMatchSucceeds(t *testing.T) {
	h, rv := newIfMatchRig(t)

	rec := putWorkerWithIfMatch(t, h, "alice", rv, `{"soul":"edited"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("code = %d, body = %s", rec.Code, rec.Body)
	}
}

// The quoted form is what HTTP entity tags look like, and a client copying that
// convention should not silently never match.
func TestUpdateAcceptsAQuotedIfMatch(t *testing.T) {
	h, rv := newIfMatchRig(t)

	rec := putWorkerWithIfMatch(t, h, "alice", `"`+rv+`"`, `{"soul":"edited"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("code = %d, body = %s", rec.Code, rec.Body)
	}
}

// The case the whole feature exists for.
func TestSecondEditorWithAStaleVersionIsRefused(t *testing.T) {
	h, rv := newIfMatchRig(t)

	// First editor saves.
	if rec := putWorkerWithIfMatch(t, h, "alice", rv, `{"soul":"first edit"}`); rec.Code != http.StatusOK {
		t.Fatalf("first edit failed: %d %s", rec.Code, rec.Body)
	}
	// Second editor still holds the version they rendered their form from.
	rec := putWorkerWithIfMatch(t, h, "alice", rv, `{"soul":"second edit"}`)
	if rec.Code != http.StatusConflict {
		t.Fatalf("code = %d, want 409 — the first edit was silently overwritten", rec.Code)
	}
	if !bytes.Contains(rec.Body.Bytes(), []byte("re-read")) {
		t.Errorf("409 body should tell the caller what to do: %s", rec.Body)
	}

	// ...and the first edit must still be there.
	req := httptest.NewRequest(http.MethodGet, "/api/v1/workers/alice", nil)
	req.SetPathValue("name", "alice")
	get := httptest.NewRecorder()
	h.GetWorker(get, req)
	var resp WorkerResponse
	if err := json.Unmarshal(get.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if resp.Soul != "first edit" {
		t.Errorf("soul = %q, want the first edit preserved", resp.Soul)
	}
}

// Every existing client — agt, the Manager agent, workers — sends no If-Match.
// Their behaviour must be exactly what it was.
func TestUpdateWithoutIfMatchKeepsTheOldBehaviour(t *testing.T) {
	h, rv := newIfMatchRig(t)

	if rec := putWorkerWithIfMatch(t, h, "alice", rv, `{"soul":"first"}`); rec.Code != http.StatusOK {
		t.Fatalf("setup edit failed: %d", rec.Code)
	}
	// Stale by now, but no header: last write wins, as before.
	rec := putWorkerWithIfMatch(t, h, "alice", "", `{"soul":"second"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("code = %d, want 200 — omitting If-Match must not change behaviour", rec.Code)
	}
}

func TestHumanAndTeamUpdatesHonourIfMatchToo(t *testing.T) {
	scheme := newServerTestScheme(t)
	human := &v1beta1.Human{
		ObjectMeta: metav1.ObjectMeta{Name: "maizong", Namespace: "default"},
		Spec:       v1beta1.HumanSpec{DisplayName: "Mai", PermissionLevel: 2},
	}
	team := &v1beta1.Team{ObjectMeta: metav1.ObjectMeta{Name: "market", Namespace: "default"}}
	k8s := fake.NewClientBuilder().WithScheme(scheme).WithObjects(human, team).Build()
	h := NewResourceHandler(k8s, "default", nil, "", nil)

	humanReq := httptest.NewRequest(http.MethodPut, "/api/v1/humans/maizong",
		bytes.NewReader([]byte(`{"displayName":"Mai Z"}`)))
	humanReq.SetPathValue("name", "maizong")
	humanReq.Header.Set("If-Match", "definitely-not-the-current-version")
	rec := httptest.NewRecorder()
	h.UpdateHuman(rec, humanReq)
	if rec.Code != http.StatusConflict {
		t.Errorf("human update: code = %d, want 409", rec.Code)
	}

	teamReq := httptest.NewRequest(http.MethodPut, "/api/v1/teams/market",
		bytes.NewReader([]byte(`{"description":"x"}`)))
	teamReq.SetPathValue("name", "market")
	teamReq.Header.Set("If-Match", "definitely-not-the-current-version")
	rec = httptest.NewRecorder()
	h.UpdateTeam(rec, teamReq)
	if rec.Code != http.StatusConflict {
		t.Errorf("team update: code = %d, want 409", rec.Code)
	}
}

// A response without a resourceVersion gives a client nothing to send back, so
// the feature would be unusable even though the server side works.
func TestListAndGetExposeResourceVersion(t *testing.T) {
	scheme := newServerTestScheme(t)
	worker := &v1beta1.Worker{ObjectMeta: metav1.ObjectMeta{Name: "alice", Namespace: "default"}}
	human := &v1beta1.Human{ObjectMeta: metav1.ObjectMeta{Name: "maizong", Namespace: "default"}}
	team := &v1beta1.Team{ObjectMeta: metav1.ObjectMeta{Name: "market", Namespace: "default"}}
	k8s := fake.NewClientBuilder().WithScheme(scheme).WithObjects(worker, human, team).Build()
	h := NewResourceHandler(k8s, "default", nil, "", nil)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/workers", nil)
	rec := httptest.NewRecorder()
	h.ListWorkers(rec, req)
	var list WorkerListResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &list); err != nil {
		t.Fatal(err)
	}
	if len(list.Workers) == 0 || list.Workers[0].ResourceVersion == "" {
		t.Error("list response workers carry no resourceVersion")
	}

	hreq := httptest.NewRequest(http.MethodGet, "/api/v1/humans/maizong", nil)
	hreq.SetPathValue("name", "maizong")
	hrec := httptest.NewRecorder()
	h.GetHuman(hrec, hreq)
	var hresp HumanResponse
	if err := json.Unmarshal(hrec.Body.Bytes(), &hresp); err != nil {
		t.Fatal(err)
	}
	if hresp.ResourceVersion == "" {
		t.Error("human response carries no resourceVersion")
	}
}
