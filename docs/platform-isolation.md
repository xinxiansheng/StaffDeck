# Platform private execution boundary

This opt-in integration preserves the platform human identity when a server API key is shared across users. It does not add a new role system or change ordinary StaffDeck sessions into platform sessions.

## Provisioning

A StaffDeck deployment administrator creates a dedicated API Client with `metadata.platform_tenant_id` set to the trusted platform tenant. Both Client and credential must explicitly include `platform:delegate` and the required private scopes:

`sessions:read`, `sessions:write`, `runs:create`, `runs:read`, `runs:cancel`, `jobs:read`, `jobs:cancel`, `memories:read`, `memories:write`.

An ordinary wildcard key cannot opt in. Ordinary members can manage only their own clients/keys and cannot create delegation grants. A configured delegation credential requires a valid subject on every request; missing context never falls back to the Client owner. Keep management and runtime credentials separate and use TLS or a controlled service network. Alternative identity providers must implement verified delegation before enabling this path.

`X-Platform-Subject` is base64url UTF-8 JSON containing exactly `tenant_id`, `user_id`, `employee_id`, `agent_id`. The trusted server constructs it after authenticating and authorizing its user. It is not a browser or model field. The delegated actor is a deterministic, password-disabled member scoped to the given Agent, never the administrator who provisioned the key.

## Guarantees and constraints

- Owner checks cover private sessions, details/reset, artifacts, uploads, native memories, jobs/runs/events and handoffs. External session IDs and idempotency keys are scoped to the subject.
- Queued runs retain the admitted actor and credential. Execution and capability invocation re-read live grants; revocation or lost scope stops further calls. Replacing a key with a new credential ID does not transfer old jobs; `:rotate` on the same credential retains that identity.
- A platform turn can use only the employee's `platform_<normalized-employee-id>` MCP gateway. Native knowledge, general skills, arbitrary business capabilities and mismatched tool arguments cannot bypass the platform's own resource authorization.
- Direct filesystem tools enforce the task workspace boundary. Commands require an OS isolation backend and deny network access; failed isolation does not fall back to unrestricted execution. Use the repository's locked SRT package, or a supported Bubblewrap installation, and verify the actual target OS.
- Platform handoffs stay with the original human, even when an SOP selects a different default handler. They use web notification only and accept both pending and assigned replies. Resume keeps the original session identity.
- Historical service-owner sessions/memories are not silently reassigned. The privileged deployment/DB/host operator remains a trusted boundary; the delegation key must never be exposed to end users.
- Group/IM execution is not accepted by this integration delivery. Do not infer production acceptance from local tests.

## Verification

`backend/tests/test_platform_subject_isolation.py` uses real HTTP routes and SQLite, with synthetic principals. `backend/tests_harness/test_platform_isolation_engine.py` uses the real Node engine and MCP with a deterministic local model. It proves private memory recall, own workspace read/write and command execution, denial of other owners' files/native business access/network, and result ownership. Set `HARNESS_V3_ROOT` to a built engine; set `STAFFDECK_SRT_RUNTIME` to the repository's verified runtime bundle to require the positive command control. No real credentials or user data are needed.

The integration also ran public API, memory, filesystem, command, sandbox, artifact, capability, turn ownership and handoff regression suites. Tests on other target OSes and the deployed platform gateway remain deployment checks.
