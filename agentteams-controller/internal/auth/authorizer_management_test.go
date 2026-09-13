package auth

import "testing"

// The Management Console workload identity. These tests are the containment
// argument for it: the role exists because the Matrix-token path only resolves
// L2 humans, and the cost of giving the console an identity of its own is that
// something has to prove the identity stays small.

func managementCaller() *CallerIdentity {
	return &CallerIdentity{
		Role:                    RoleManagement,
		Username:                "management",
		ServiceAccountNamespace: "agentic",
		ServiceAccountName:      "agentteams-management",
	}
}

func TestManagementRoleIsReadOnly(t *testing.T) {
	az := NewAuthorizer()
	caller := managementCaller()

	for _, req := range []AuthzRequest{
		{Action: ActionList, ResourceKind: "worker"},
		{Action: ActionGet, ResourceKind: "worker", ResourceName: "alice"},
		{Action: ActionList, ResourceKind: "team"},
		{Action: ActionGet, ResourceKind: "team", ResourceName: "research"},
		{Action: ActionList, ResourceKind: "human"},
		{Action: ActionGet, ResourceKind: "human", ResourceName: "huybui1"},
		{Action: ActionList, ResourceKind: "project"},
		{Action: ActionGet, ResourceKind: "project", ResourceName: "p-1"},
		{Action: ActionGet, ResourceKind: "status"},
	} {
		if err := az.Authorize(caller, req); err != nil {
			t.Errorf("management should be allowed %s %s: %v", req.Action, req.ResourceKind, err)
		}
	}
}

// The whole point of not making this RoleAdmin. Every entry here is something
// the console could plausibly be asked to do "just this once" later; each one
// has to be added deliberately, and this test is what makes that deliberate.
func TestManagementRoleDeniedOnPrivilegedSurfaces(t *testing.T) {
	az := NewAuthorizer()
	caller := managementCaller()

	denied := []AuthzRequest{
		// Credential issuance: the console holding these means compromising the
		// console mints worker identities.
		{Action: ActionSTS, ResourceKind: "credentials"},
		{Action: ActionRefreshMatrixToken, ResourceKind: "credentials"},
		// Gateway consumer binding.
		{Action: ActionGateway, ResourceKind: "gateway"},
		// Writes of every shape, on every kind it can read.
		{Action: ActionCreate, ResourceKind: "worker"},
		{Action: ActionUpdate, ResourceKind: "worker", ResourceName: "alice"},
		{Action: ActionDelete, ResourceKind: "worker", ResourceName: "alice"},
		{Action: ActionCreate, ResourceKind: "team"},
		{Action: ActionUpdate, ResourceKind: "team", ResourceName: "research"},
		{Action: ActionDelete, ResourceKind: "team", ResourceName: "research"},
		{Action: ActionCreate, ResourceKind: "human"},
		{Action: ActionUpdate, ResourceKind: "human", ResourceName: "huybui1"},
		{Action: ActionDelete, ResourceKind: "human", ResourceName: "huybui1"},
		{Action: ActionCreate, ResourceKind: "project"},
		{Action: ActionUpdate, ResourceKind: "project", ResourceName: "p-1"},
		// Worker lifecycle and behaviour.
		{Action: ActionWake, ResourceKind: "worker", ResourceName: "alice"},
		{Action: ActionSleep, ResourceKind: "worker", ResourceName: "alice"},
		{Action: ActionWorkerApproval, ResourceKind: "worker", ResourceName: "alice"},
		{Action: ActionWorkspaceFilesWrite, ResourceKind: "worker", ResourceName: "alice"},
		// Kinds it has no business touching at all.
		{Action: ActionGet, ResourceKind: "manager"},
		{Action: ActionUpdate, ResourceKind: "manager"},
	}
	for _, req := range denied {
		if err := az.Authorize(caller, req); err == nil {
			t.Errorf("management must NOT be allowed %s %s", req.Action, req.ResourceKind)
		}
	}
}

// Adding a role must not move the L2 boundary. checkHumanWorkerUpdate in the
// resource handler only runs when caller.Role == RoleHuman, so the field policy
// is untouched by construction — this covers the authorizer half.
func TestManagementRoleDoesNotWeakenL2Policy(t *testing.T) {
	az := NewAuthorizer()
	human := &CallerIdentity{Role: RoleHuman, Username: "maizong", Teams: []string{"market-team"}}

	mustDeny := []AuthzRequest{
		{Action: ActionCreate, ResourceKind: "worker", ResourceTeam: "market-team"},
		{Action: ActionDelete, ResourceKind: "worker", ResourceTeam: "market-team"},
		{Action: ActionWake, ResourceKind: "worker", ResourceTeam: "market-team"},
		{Action: ActionUpdate, ResourceKind: "team", ResourceTeam: "market-team"},
		{Action: ActionSTS, ResourceKind: "credentials"},
	}
	for _, req := range mustDeny {
		if err := az.Authorize(human, req); err == nil {
			t.Errorf("L2 human must still be denied %s %s", req.Action, req.ResourceKind)
		}
	}
	// ...and what they could do before, they still can.
	if err := az.Authorize(human, AuthzRequest{Action: ActionUpdate, ResourceKind: "worker", ResourceTeam: "market-team"}); err != nil {
		t.Errorf("L2 human lost worker update: %v", err)
	}
}

func TestParseSAUsernameRecognisesTheManagementServiceAccount(t *testing.T) {
	id, err := DefaultResourcePrefix.ParseSAUsername("system:serviceaccount:agentic:agentteams-management")
	if err != nil {
		t.Fatalf("management SA not recognised: %v", err)
	}
	if id.Role != RoleManagement {
		t.Errorf("role = %q, want %q", id.Role, RoleManagement)
	}
	if id.ServiceAccountName != "agentteams-management" || id.ServiceAccountNamespace != "agentic" {
		t.Errorf("SA identity not carried through: %+v", id)
	}
}

// "agentteams-management" starts with neither the worker prefix nor the manager
// name, but the two manager cases sit next to it in the switch and an exact
// match is one edit away from a prefix match.
func TestManagementSAIsNotMistakenForManagerOrAdmin(t *testing.T) {
	for username, wantRole := range map[string]string{
		"system:serviceaccount:agentic:agentteams-management": RoleManagement,
		"system:serviceaccount:agentic:agentteams-manager":    RoleManager,
		"system:serviceaccount:agentic:agentteams-admin":      RoleAdmin,
	} {
		id, err := DefaultResourcePrefix.ParseSAUsername(username)
		if err != nil {
			t.Fatalf("%s: %v", username, err)
		}
		if id.Role != wantRole {
			t.Errorf("%s resolved to role %q, want %q", username, id.Role, wantRole)
		}
	}
}
