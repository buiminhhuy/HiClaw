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

func TestManagementRoleAllowedSurfaces(t *testing.T) {
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
		// Profile edits (S4). The controller cannot distinguish a display-name
		// change from a privilege change, so the narrowing lives in the console;
		// see TestManagementHumanUpdateIsTheOnlyWriteAllowed for the boundary
		// this role still keeps.
		{Action: ActionUpdate, ResourceKind: "human", ResourceName: "huybui1"},
		// S5: admin user management. Gated on Scope.Admin in the console, not
		// here — see TestManagementHumanCreateAndDeleteAreAllowed.
		{Action: ActionCreate, ResourceKind: "human"},
		{Action: ActionDelete, ResourceKind: "human", ResourceName: "huybui1"},
		// S6: admin-gated worker CRUD and lifecycle. Same shape, same gate —
		// see TestManagementWorkerWritesAreAllowedButTeamAndProjectAreNot.
		{Action: ActionCreate, ResourceKind: "worker"},
		{Action: ActionUpdate, ResourceKind: "worker", ResourceName: "alice"},
		{Action: ActionDelete, ResourceKind: "worker", ResourceName: "alice"},
		{Action: ActionWake, ResourceKind: "worker", ResourceName: "alice"},
		{Action: ActionSleep, ResourceKind: "worker", ResourceName: "alice"},
		// S7: admin-gated Team CRUD. Create is a two-step console operation
		// (Leader Worker, then Team); the controller's own
		// validateTeamWorkerMembers still enforces the ordering constraint
		// regardless of this grant — see
		// TestManagementTeamWritesAreAllowedButProjectIsNot.
		{Action: ActionCreate, ResourceKind: "team"},
		{Action: ActionUpdate, ResourceKind: "team", ResourceName: "research"},
		{Action: ActionDelete, ResourceKind: "team", ResourceName: "research"},
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
		// Writes on project remain denied — no console screen needs them.
		{Action: ActionCreate, ResourceKind: "project"},
		{Action: ActionUpdate, ResourceKind: "project", ResourceName: "p-1"},
		// Worker *behaviour*, as opposed to its CRUD/lifecycle above: these act
		// on what the worker is allowed to do, not on the resource itself, and
		// stay denied.
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

// Exactly three kinds get writes, and it is worth stating as its own test: the
// next person to need "just one more" has to come here and add it
// deliberately.
func TestManagementWritesAreAllowedOnlyOnHumanWorkerAndTeam(t *testing.T) {
	az := NewAuthorizer()
	caller := managementCaller()

	for _, kind := range []string{"human", "worker", "team"} {
		for _, a := range []Action{ActionUpdate, ActionCreate, ActionDelete} {
			if err := az.Authorize(caller, AuthzRequest{Action: a, ResourceKind: kind, ResourceName: "x"}); err != nil {
				t.Errorf("%s %s should be allowed: %v", a, kind, err)
			}
		}
	}
	for _, a := range []Action{ActionUpdate, ActionCreate, ActionDelete} {
		if err := az.Authorize(caller, AuthzRequest{Action: a, ResourceKind: "project"}); err == nil {
			t.Errorf("%s project must still be denied", a)
		}
	}
}

// The worker grant is CRUD *and* lifecycle (wake/sleep), because S6 needs
// both; it is not worker-behaviour actions like approval or workspace writes,
// which stay denied regardless of resource kind.
func TestManagementWorkerWritesAreAllowedButTeamAndProjectAreNot(t *testing.T) {
	az := NewAuthorizer()
	caller := managementCaller()

	for _, a := range []Action{ActionCreate, ActionUpdate, ActionDelete, ActionWake, ActionSleep} {
		if err := az.Authorize(caller, AuthzRequest{Action: a, ResourceKind: "worker", ResourceName: "alice"}); err != nil {
			t.Errorf("worker %s should be allowed: %v", a, err)
		}
	}
	for _, a := range []Action{ActionWorkerApproval, ActionWorkspaceFilesWrite} {
		if err := az.Authorize(caller, AuthzRequest{Action: a, ResourceKind: "worker", ResourceName: "alice"}); err == nil {
			t.Errorf("worker %s must still be denied", a)
		}
	}
}

// The team grant is CRUD only — there is no team-lifecycle or team-behaviour
// action to keep denied the way worker has ActionWorkerApproval, but project
// stays a useful negative control since it sits right next to team in the
// authorizer's switch.
func TestManagementTeamWritesAreAllowedButProjectIsNot(t *testing.T) {
	az := NewAuthorizer()
	caller := managementCaller()

	for _, a := range []Action{ActionCreate, ActionUpdate, ActionDelete} {
		if err := az.Authorize(caller, AuthzRequest{Action: a, ResourceKind: "team", ResourceName: "research"}); err != nil {
			t.Errorf("team %s should be allowed: %v", a, err)
		}
	}
	for _, a := range []Action{ActionCreate, ActionUpdate} {
		if err := az.Authorize(caller, AuthzRequest{Action: a, ResourceKind: "project"}); err == nil {
			t.Errorf("project %s must still be denied", a)
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
