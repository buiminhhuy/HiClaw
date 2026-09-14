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
		// S12 scheduler: dispatch is create-project (+ replan for a pinned
		// Worker target) and pause/resume/replan/cancel/complete, all
		// ActionUpdate — see TestManagementProjectWritesAreNowAllowedForTheScheduler.
		{Action: ActionCreate, ResourceKind: "project"},
		{Action: ActionUpdate, ResourceKind: "project", ResourceName: "p-1"},
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
		// see TestManagementWorkerWritesAreAllowedButBehaviourActionsAreNot.
		{Action: ActionCreate, ResourceKind: "worker"},
		{Action: ActionUpdate, ResourceKind: "worker", ResourceName: "alice"},
		{Action: ActionDelete, ResourceKind: "worker", ResourceName: "alice"},
		{Action: ActionWake, ResourceKind: "worker", ResourceName: "alice"},
		{Action: ActionSleep, ResourceKind: "worker", ResourceName: "alice"},
		// S7: admin-gated Team CRUD. Create is a two-step console operation
		// (Leader Worker, then Team); the controller's own
		// validateTeamWorkerMembers still enforces the ordering constraint
		// regardless of this grant — see TestManagementTeamWritesAreAllowed.
		{Action: ActionCreate, ResourceKind: "team"},
		{Action: ActionUpdate, ResourceKind: "team", ResourceName: "research"},
		{Action: ActionDelete, ResourceKind: "team", ResourceName: "research"},
		// S16: local worker onboarding reads one named worker's already-
		// provisioned bootstrap bundle — see TestManagementLocalEnrollIsNotCredentialIssuance.
		{Action: ActionLocalEnroll, ResourceKind: "worker", ResourceName: "alice"},
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
		// Project create/update is no longer in this list — see
		// TestManagementProjectWritesAreNowAllowedForTheScheduler for why, and
		// TestManagementRoleAllowedSurfaces for where it now lives.
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

// Four kinds get writes, and it is worth stating as its own test: the next
// person to need "just one more" has to come here and add it deliberately.
// Project is the odd one out — Create/Update only, no Delete, because that is
// the entire write surface the Project API exposes (see
// TestManagementProjectWritesAreNowAllowedForTheScheduler for why it is here
// at all, and for the same reason ActionDelete on "project" is asserted
// denied below rather than omitted: there is no such route to grant, so this
// pins that a future project-delete capability still has to be added here
// deliberately rather than falling out of some other change).
func TestManagementWritesAreAllowedOnlyOnHumanWorkerTeamAndProject(t *testing.T) {
	az := NewAuthorizer()
	caller := managementCaller()

	for _, kind := range []string{"human", "worker", "team"} {
		for _, a := range []Action{ActionUpdate, ActionCreate, ActionDelete} {
			if err := az.Authorize(caller, AuthzRequest{Action: a, ResourceKind: kind, ResourceName: "x"}); err != nil {
				t.Errorf("%s %s should be allowed: %v", a, kind, err)
			}
		}
	}
	for _, a := range []Action{ActionUpdate, ActionCreate} {
		if err := az.Authorize(caller, AuthzRequest{Action: a, ResourceKind: "project", ResourceName: "p-1"}); err != nil {
			t.Errorf("%s project should be allowed: %v", a, err)
		}
	}
	if err := az.Authorize(caller, AuthzRequest{Action: ActionDelete, ResourceKind: "project", ResourceName: "p-1"}); err == nil {
		t.Error("ActionDelete project must still be denied — there is no such route to grant")
	}
}

// The worker grant is CRUD *and* lifecycle (wake/sleep), because S6 needs
// both; it is not worker-behaviour actions like approval or workspace writes,
// which stay denied regardless of resource kind.
func TestManagementWorkerWritesAreAllowedButBehaviourActionsAreNot(t *testing.T) {
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
// action to keep denied the way worker has ActionWorkerApproval.
func TestManagementTeamWritesAreAllowed(t *testing.T) {
	az := NewAuthorizer()
	caller := managementCaller()

	for _, a := range []Action{ActionCreate, ActionUpdate, ActionDelete} {
		if err := az.Authorize(caller, AuthzRequest{Action: a, ResourceKind: "team", ResourceName: "research"}); err != nil {
			t.Errorf("team %s should be allowed: %v", a, err)
		}
	}
}

// Phase 5 (S12 scheduler): dispatch is create-project, plus replan/pause/
// resume/cancel/complete, all ActionUpdate — see the "project" case comment
// in authorizer.go for why this grant is deliberately not team-scoped here
// the way RoleTeamLeader's identical case is (RoleManagement has no
// caller.Team to scope against), and for why that makes it the console's job
// to check the schedule owner's own grant on the target Team before ever
// building the request. This used to be a denied surface (see git history /
// ADR-0020 in the management console repo for why it no longer is); the
// negative control that mattered — a resource kind still gets nothing until a
// concrete screen needs it — moved to
// TestManagementWritesAreAllowedOnlyOnHumanWorkerTeamAndProject's ActionDelete
// assertion, which is the one piece of "project" that still has nothing to
// grant.
func TestManagementProjectWritesAreNowAllowedForTheScheduler(t *testing.T) {
	az := NewAuthorizer()
	caller := managementCaller()

	for _, a := range []Action{ActionCreate, ActionUpdate, ActionGet, ActionList} {
		if err := az.Authorize(caller, AuthzRequest{Action: a, ResourceKind: "project", ResourceName: "p-1"}); err != nil {
			t.Errorf("project %s should be allowed: %v", a, err)
		}
	}
	if err := az.Authorize(caller, AuthzRequest{Action: ActionDelete, ResourceKind: "project", ResourceName: "p-1"}); err == nil {
		t.Error("project ActionDelete must still be denied — there is no such route")
	}
}

// ActionLocalEnroll (S16) is a read of what reconcile already persisted, not
// credential issuance. This test is the line between the two: management
// gets the former on "worker" but must still be denied the latter on
// "credentials" — one grant existing must never imply the other.
func TestManagementLocalEnrollIsNotCredentialIssuance(t *testing.T) {
	az := NewAuthorizer()
	caller := managementCaller()

	if err := az.Authorize(caller, AuthzRequest{Action: ActionLocalEnroll, ResourceKind: "worker", ResourceName: "alice"}); err != nil {
		t.Errorf("local-enroll on worker should be allowed: %v", err)
	}
	for _, req := range []AuthzRequest{
		{Action: ActionLocalEnroll, ResourceKind: "credentials"},
		{Action: ActionSTS, ResourceKind: "credentials"},
		{Action: ActionRefreshMatrixToken, ResourceKind: "credentials"},
	} {
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
