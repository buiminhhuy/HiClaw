package service

import (
	"context"
	"testing"
)

// LocalEnrollmentBundle backs S16 (Management Console local-worker
// onboarding): it must be a pure read of what a normal reconcile already
// persisted, never a mint/refresh — see auth.ActionLocalEnroll.

func TestLocalEnrollmentBundleReturnsProvisionedCredentials(t *testing.T) {
	creds := fakeCredentialStore{
		"laptop-worker": {
			MatrixToken:   "worker-matrix-token",
			MinIOPassword: "worker-minio-secret",
			GatewayKey:    "worker-gateway-key",
		},
	}
	p := NewProvisioner(ProvisionerConfig{
		Matrix: newFakeTeamMatrix(),
		Creds:  creds,
	})

	bundle, err := p.LocalEnrollmentBundle(context.Background(), "laptop-worker")
	if err != nil {
		t.Fatalf("LocalEnrollmentBundle: %v", err)
	}
	if bundle.MatrixUserID != "@laptop-worker:localhost" {
		t.Errorf("MatrixUserID = %q, want @laptop-worker:localhost", bundle.MatrixUserID)
	}
	if bundle.MatrixToken != "worker-matrix-token" {
		t.Errorf("MatrixToken = %q, want worker-matrix-token", bundle.MatrixToken)
	}
	if bundle.MinIOAccessKey != "laptop-worker" {
		t.Errorf("MinIOAccessKey = %q, want laptop-worker (the worker name)", bundle.MinIOAccessKey)
	}
	if bundle.MinIOSecretKey != "worker-minio-secret" {
		t.Errorf("MinIOSecretKey = %q, want worker-minio-secret", bundle.MinIOSecretKey)
	}
	if bundle.GatewayKey != "worker-gateway-key" {
		t.Errorf("GatewayKey = %q, want worker-gateway-key", bundle.GatewayKey)
	}
}

// A worker whose first reconcile has not finished yet has no Secret at all
// (fakeCredentialStore.Load returns nil, nil for an unknown key — see below).
// This must surface as an error, not as an empty-but-200 bundle: the console
// would otherwise render a startup command with a blank Matrix token.
func TestLocalEnrollmentBundleRejectsAWorkerStillReconciling(t *testing.T) {
	p := NewProvisioner(ProvisionerConfig{
		Matrix: newFakeTeamMatrix(),
		Creds:  fakeCredentialStore{},
	})

	if _, err := p.LocalEnrollmentBundle(context.Background(), "not-yet-ready"); err == nil {
		t.Fatal("LocalEnrollmentBundle: want error for a worker with no provisioned credentials, got nil")
	}
}

// Reconcile writes the Secret before it has a Matrix token in one narrow
// window (creds.MatrixToken populated only after ensureMatrixToken runs).
// Treat "found but tokenless" the same as "not found" rather than handing the
// console a bundle it cannot actually use to log in.
func TestLocalEnrollmentBundleRejectsCredentialsWithoutAMatrixToken(t *testing.T) {
	p := NewProvisioner(ProvisionerConfig{
		Matrix: newFakeTeamMatrix(),
		Creds: fakeCredentialStore{
			"mid-reconcile": {
				MinIOPassword: "minio-secret",
			},
		},
	})

	if _, err := p.LocalEnrollmentBundle(context.Background(), "mid-reconcile"); err == nil {
		t.Fatal("LocalEnrollmentBundle: want error for credentials with an empty Matrix token, got nil")
	}
}
