package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/matrix"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/service"
)

// LocalEnrollment (S16 — Management Console local-worker onboarding) is a
// read-only disclosure endpoint: it must return the worker's already-
// provisioned bundle when present, and a clear non-200 (never a 200 with an
// empty token) when the worker has not finished its first reconcile.

// fakeUserIDMatrix satisfies matrix.Client via embedding (nil for every
// method but UserID, which is all LocalEnrollmentBundle exercises); calling
// any other method panics, which is the point — this test has no business
// touching them.
type fakeUserIDMatrix struct {
	matrix.Client
}

func (fakeUserIDMatrix) UserID(localpart string) string {
	return "@" + localpart + ":localhost"
}

type fakeCredStore map[string]*service.WorkerCredentials

func (f fakeCredStore) Load(_ context.Context, workerName string) (*service.WorkerCredentials, error) {
	return f[workerName], nil
}
func (f fakeCredStore) Save(_ context.Context, workerName string, creds *service.WorkerCredentials) error {
	f[workerName] = creds
	return nil
}
func (f fakeCredStore) Delete(_ context.Context, workerName string) error {
	delete(f, workerName)
	return nil
}
func (f fakeCredStore) List(context.Context) ([]string, error) {
	names := make([]string, 0, len(f))
	for k := range f {
		names = append(names, k)
	}
	return names, nil
}

func newLocalEnrollmentHandler(creds map[string]*service.WorkerCredentials) *CredentialsHandler {
	prov := service.NewProvisioner(service.ProvisionerConfig{
		Matrix: fakeUserIDMatrix{},
		Creds:  fakeCredStore(creds),
	})
	return NewCredentialsHandler(nil, prov)
}

func TestLocalEnrollmentReturnsTheProvisionedBundle(t *testing.T) {
	h := newLocalEnrollmentHandler(map[string]*service.WorkerCredentials{
		"alice": {MatrixToken: "tok", MinIOPassword: "sekret", GatewayKey: "gwkey"},
	})

	req := httptest.NewRequest(http.MethodPost, "/api/v1/workers/alice/local-enrollment", nil)
	req.SetPathValue("name", "alice")
	rec := httptest.NewRecorder()

	h.LocalEnrollment(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var got service.LocalEnrollmentBundle
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if got.MatrixToken != "tok" || got.MinIOSecretKey != "sekret" || got.GatewayKey != "gwkey" {
		t.Errorf("bundle = %+v, missing expected fields", got)
	}
	if got.MinIOAccessKey != "alice" {
		t.Errorf("MinIOAccessKey = %q, want alice", got.MinIOAccessKey)
	}
}

func TestLocalEnrollmentOnAWorkerStillReconcilingIsNotA200(t *testing.T) {
	h := newLocalEnrollmentHandler(nil)

	req := httptest.NewRequest(http.MethodPost, "/api/v1/workers/brand-new/local-enrollment", nil)
	req.SetPathValue("name", "brand-new")
	rec := httptest.NewRecorder()

	h.LocalEnrollment(rec, req)

	if rec.Code == http.StatusOK {
		t.Fatalf("status = 200 with body %s, want a non-200 (worker has no credentials yet)", rec.Body.String())
	}
}

func TestLocalEnrollmentRequiresAWorkerName(t *testing.T) {
	h := newLocalEnrollmentHandler(nil)

	req := httptest.NewRequest(http.MethodPost, "/api/v1/workers//local-enrollment", nil)
	rec := httptest.NewRecorder()

	h.LocalEnrollment(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400 for a missing worker name", rec.Code)
	}
}
