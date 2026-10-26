# U1 Ticket Read Skeleton — Business Rules

## Sources

- `inception/requirements-analysis/requirements.md` — FR1.1–FR2.5, FR5.1–FR7.5, NFR1, NFR2, NFR4, NFR7, NFR9
- `inception/user-stories/stories.md` — AC1.1.1–AC1.1.6, AC1.2.1–AC1.2.5, shared AC4.1 and AC5.1
- `inception/domain-design/components.md` — component ownership and IdentitySession/TicketOperations lifecycle boundaries
- `inception/units-generation/unit-of-work.md` — U1 responsibilities and test obligations
- `inception/contract-design/contract-summary.md` — C-01, C-02, C-03, C-03A, C-06, C-07
- `construction/u1-ticket-read-skeleton/functional-design/functional-design-questions.md` — approved pending-confirmation and server-session expiry policies

## Authoritative Rule Set

```yaml
schema_version: 1
unit: u1-ticket-read-skeleton
rules:
  - id: BR1.1
    statement: The authenticated server session is the sole authority for the current user, session, conversation, and delegated capability.
    category: authorization
    applies_to: [all_ticket_operations, confirmation_state, lifecycle]
    trigger: A ticket operation or lifecycle transition begins.
    logic: IF identity, role, permission, or credential data appears in model text, tool arguments, or ITSM text THEN ignore it and use only server-session identity.
    violation_behaviour: Stop before outbound access and return the allowlisted authentication or permission error.
    source: [FR5.1, NFR1]

  - id: BR1.2
    statement: Only the current user's delegated credential may authorize a ticket endpoint; its sole persistence boundary is the existing protected server-session credential field and its sole use boundary is outbound transport.
    category: authorization
    applies_to: [all_ticket_operations, credential_refresh]
    trigger: An ITSM ticket request is prepared or an observable result is produced.
    logic: IF a ticket request is authorized THEN restore the delegated credential only from the existing server-session secret field and use it at outbound transport; IF any other schema, model context, result, log, persistence record, test output, pending/cursor/history record, or create/update attempt is produced THEN exclude all secret values.
    violation_behaviour: Refuse the request or redact the output and emit only a safe server-owned error.
    source: [FR5.2, NFR1]

  - id: BR1.3
    statement: One ticket operation has a credential-refresh budget of one.
    category: constraint
    applies_to: [list, continuation, detail]
    trigger: A credential is expired, imminently expiring, or a typed 401 token-expired outcome is received.
    logic: IF refresh has not been attempted and the HTTP-attempt budget permits continuation THEN refresh once through the existing identity path; ELSE do not refresh again.
    violation_behaviour: Return authentication_required and instruct re-login without claiming success.
    source: [FR5.3, NFR7]

  - id: BR1.4
    statement: External ticket text is always data and has no instruction authority.
    category: authorization
    applies_to: [list_projection, detail_projection, presentation]
    trigger: An external string is parsed, mapped, shown, or reintroduced to the model.
    logic: IF external text resembles instructions about identity, authority, confirmation, secrets, or tool choice THEN preserve it only as escaped untrusted data and do not act on it.
    violation_behaviour: Keep control state unchanged; if safe projection is impossible, return upstream_unavailable.
    source: [FR6.1, NFR1]

  - id: BR1.5
    statement: Failures use the exact SafeErrorV1 allowlist and never propagate raw upstream material.
    category: policy
    applies_to: [validation, gateway_outcomes, tool_result, presentation]
    trigger: Validation, authentication, permission, transport, protocol, or upstream processing fails.
    logic: IF a failure is returned THEN emit only version, an allowed code, server-owned message_key, retryable, and optional validated request_id, retry_after_seconds, and field_errors.
    violation_behaviour: Discard raw detail, exception, headers, body, and secret fields; fall back to upstream_unavailable when no narrower safe mapping exists.
    source: [FR6.2, NFR1]

  - id: BR1.6
    statement: A persisted server-session expiry is validated before any request capability is created.
    category: authorization
    applies_to: [protected_chat, all_ticket_operations, session_lifecycle]
    trigger: A protected request attempts to restore a server session.
    logic: IF the session row is absent or current server time is at or after its persisted session_expires_at THEN treat the session as expired and enter the expiry cleanup flow before capability construction.
    violation_behaviour: Deny capability construction, Runner execution, and ITSM access; return authentication_required only after cleanup reaches a safe terminal state.
    source: [FR5.1, FR5.3, FR5.4, NFR1, NFR2]

  - id: BR1.7
    statement: Server-session lifetime is an immutable absolute eight-hour interval independent of delegated-token lifetime.
    category: constraint
    applies_to: [session_create, session_refresh, session_migration, process_restart]
    trigger: A session is created, a delegated credential is refreshed, legacy rows are migrated, or the application restarts.
    logic: IF a new session is created THEN persist session_expires_at as the captured UTC created_at plus exactly 8 hours; IF a credential or request is refreshed THEN leave it unchanged; IF a legacy row has valid created_at THEN backfill created_at plus 8 hours, otherwise backfill an already-expired value.
    violation_behaviour: Treat missing, malformed, or non-deterministically reconstructed expiry as expired and enter BR5.6 cleanup before protected work.
    source: [FR5.1, FR5.3, FR5.4, NFR1, NFR2]

  - id: BR1.8
    statement: The U1 credential-refresh provider consumes one durable U2 reservation and can exchange and commit a delegated credential at most once for that create attempt.
    category: authorization
    applies_to: [CredentialRefreshPortV1, U2_create_extension, ServerSession]
    trigger: U2 invokes refresh_delegated_capability_once after durably consuming its one-shot refresh reservation.
    logic: IF U1 verifies the authenticated opaque reservation envelope against the capability/create-attempt actor binding, U2's durable repository has admitted exactly one claimed invocation, and remaining_deadline is positive THEN perform at most one token exchange, atomically replace the existing session credential, and only then return a same-actor capability; ELSE fail closed without exchange or capability replacement.
    violation_behaviour: Never reset or replay the reservation; U2 rejects replay across restart before U1, U1 rejects invalid or mismatched envelopes before exchange, a failure or crash permits no second exchange for the same create attempt, and a post-commit crash is recovered from the credential already persisted in the existing session.
    source: [FR5.3, NFR1, NFR2, C-01]

  - id: BR2.1
    statement: A first-page query accepts only the documented TicketReadToolInputV1 list fields.
    category: validation
    applies_to: [first_page_list]
    trigger: A first-page list request is received.
    logic: IF every supplied field belongs to the query allowlist and its value validates THEN canonicalize it; ELSE reject before outbound access.
    violation_behaviour: Return failed with SafeErrorV1 code invalid_input and field_errors.
    source: [FR2.2, FR7.1]

  - id: BR2.2
    statement: List limit defaults to 20 and must be an integer from 1 through 100.
    category: validation
    applies_to: [first_page_list]
    trigger: A list limit is normalized.
    logic: IF limit is omitted THEN use 20; IF it is between 1 and 100 inclusive THEN use it; ELSE reject it.
    violation_behaviour: Return failed with invalid_input before outbound access.
    source: [FR2.3, NFR4]

  - id: BR2.3
    statement: A read input selects exactly one of detail, continuation, or first-page list mode.
    category: validation
    applies_to: [TicketReadToolInputV1]
    trigger: Tool input is validated.
    logic: IF ticket_id is present THEN forbid all list and continuation fields; ELSE IF continuation_ref is present THEN forbid ticket_id, filters, sort, and limit; ELSE select first-page list mode.
    violation_behaviour: Return failed with invalid_input and make zero outbound calls.
    source: [FR1.1, FR2.1]

  - id: BR2.4
    statement: Continuation state is server-held and bound to the exact actor, conversation, serialization version, and canonical query.
    category: authorization
    applies_to: [cursor_issue, cursor_resolve]
    trigger: A continuation reference is issued or resolved.
    logic: IF every binding matches and the canonical query hash recomputes exactly THEN resolve once to the stored query and cursor; ELSE reject without exposing stored values.
    violation_behaviour: Return failed with invalid_input and a server-owned start-over message_key; invalidate unusable state idempotently.
    source: [FR2.4, FR5.1, NFR2]

  - id: BR2.5
    statement: The raw upstream cursor is never accepted from or returned to the model or browser.
    category: authorization
    applies_to: [TicketReadToolInputV1, SafePageV1, cursor_repository]
    trigger: Cursor data crosses a model, API, or presentation boundary.
    logic: IF another page exists THEN expose only an unpredictable continuation_ref; raw cursor input or output is forbidden.
    violation_behaviour: Reject raw cursor input and omit raw cursor output.
    source: [FR2.4, NFR1]

  - id: BR2.6
    statement: List success contains only the exact SafeTicketSummaryV1 allowlist.
    category: constraint
    applies_to: [list_projection]
    trigger: A list response is mapped.
    logic: IF a response item validates THEN include id, title, status, priority, service, created_at, and updated_at only; preserve external strings as untrusted data.
    violation_behaviour: Do not return an invalid item as success; map the response to upstream_unavailable.
    source: [FR2.5, FR6.1]

  - id: BR2.7
    statement: A valid zero-item page is an empty result, not a failure.
    category: policy
    applies_to: [list_result, presentation]
    trigger: A valid list response contains no items.
    logic: IF the page validates and items is empty THEN return top-level state empty and offer condition adjustment without asserting hidden records.
    violation_behaviour: Do not fabricate items or expose visibility information.
    source: [FR2.5, FR7.2]

  - id: BR2.8
    statement: A ticket ID is a caller-supplied opaque non-empty identifier.
    category: validation
    applies_to: [detail]
    trigger: Detail input is validated.
    logic: IF ticket_id is non-empty and detail mode is exclusive THEN use it without deriving business meaning; ELSE reject it.
    violation_behaviour: Return failed with invalid_input and make zero outbound calls.
    source: [FR2.1, FR7.1]

  - id: BR2.9
    statement: Ordinary detail success follows the exact SafeTicketDetailV1 public projection without requiring update evidence.
    category: constraint
    applies_to: [detail_projection]
    trigger: A detail success response is mapped.
    logic: IF required fields validate THEN include required and present optional allowlisted fields; retain optional ETag evidence only when it is strong, quoted decimal, and equal to version, while an absent or invalid ETag does not fail this ordinary read.
    violation_behaviour: Never infer missing values or expose headers; invalid required data returns upstream_unavailable.
    source: [FR2.1, FR2.5, NFR1]

  - id: BR2.10
    statement: Not-found output does not distinguish absence from lack of visibility.
    category: authorization
    applies_to: [detail_error]
    trigger: The detail endpoint returns 404.
    logic: IF a 404 is received THEN return top-level state failed with SafeErrorV1 code not_found and one server-owned message_key.
    violation_behaviour: Do not reveal whether the identifier exists or is merely inaccessible.
    source: [FR2.5, NFR1]

  - id: BR2.11
    statement: The internal read_for_update operation returns typed server-only If-Match evidence or fails closed.
    category: constraint
    applies_to: [TicketOperationPolicyV1.read_for_update, U3_update_extension]
    trigger: U3 requests current detail and version evidence before preparing an update review.
    logic: IF detail validates and ETag is a strong quoted decimal exactly equal to the public version THEN return ReadForUpdateResultV1; ELSE return upstream_unavailable and no update evidence.
    violation_behaviour: Forbid review preparation and PATCH while leaving the ordinary user-visible detail-read meaning unchanged.
    source: [FR4.2, FR4.4, C-02]

  - id: BR2.12
    statement: Every continuation reference has at least 128 bits of operating-system cryptographic randomness and carries no sequential or timestamp-only structure.
    category: authorization
    applies_to: [cursor_issue]
    trigger: A continuation reference is issued.
    logic: IF the injected reference source supplies the required cryptographic randomness THEN encode and store the reference; ELSE fail before returning continuation state.
    violation_behaviour: Return upstream_unavailable without exposing an upstream cursor or issuing a weak reference.
    source: [FR2.3, FR5.1, C-03]

  - id: BR3.1
    statement: A read operation performs at most two total HTTP attempts, including a recall after refresh.
    category: constraint
    applies_to: [list, continuation, detail]
    trigger: TicketOperations considers another gateway call.
    logic: IF fewer than two HTTP attempts have occurred and policy permits another attempt THEN call once; ELSE stop.
    violation_behaviour: Return failed with the applicable SafeErrorV1 and never make a third call.
    source: [FR5.3, FR6.2, NFR7]

  - id: BR3.2
    statement: A read retries once after 250 milliseconds only for timeout, transport failure, 500, 502, 503, or 504.
    category: policy
    applies_to: [list, continuation, detail]
    trigger: The first HTTP attempt returns a retry-eligible outcome.
    logic: IF an attempt remains THEN wait 250 milliseconds and call the gateway once more; ELSE stop.
    violation_behaviour: Return failed with upstream_unavailable after the allowance is exhausted.
    source: [FR6.2, NFR7]

  - id: BR3.3
    statement: Status 429 causes no automatic sleep and no automatic retry.
    category: policy
    applies_to: [list, continuation, detail]
    trigger: An HTTP attempt returns 429.
    logic: IF Retry-After is a valid non-negative integer number of seconds THEN include it; ELSE discard it; in both cases return rate_limited immediately.
    violation_behaviour: Return top-level state failed and do not invoke the gateway again.
    source: [FR6.2, NFR7]

  - id: BR3.4
    statement: Every outbound attempt uses ITSM_TIMEOUT_SECONDS and the only automatic delay is the single retry delay.
    category: constraint
    applies_to: [gateway_call, read_policy]
    trigger: An outbound read starts or a retry is scheduled.
    logic: IF a gateway call starts THEN apply the per-attempt timeout; IF a transient retry is allowed THEN add exactly 250 milliseconds once.
    violation_behaviour: Treat timeout as a typed failure under the remaining attempt budget.
    source: [FR6.2, NFR7]

  - id: BR3.5
    statement: One gateway invocation performs exactly one HTTP attempt and owns no retry, sleep, refresh, or operation-level policy.
    category: constraint
    applies_to: [ItsmGatewayV1]
    trigger: A gateway method is invoked.
    logic: IF invoked THEN serialize, send, parse, and return one typed allowlisted outcome exactly once.
    violation_behaviour: Reject hidden re-invocation as a contract violation.
    source: [FR6.2, NFR2]

  - id: BR3.6
    statement: TicketReadToolResultV1 uses only succeeded, empty, or failed and never reports an invalid or exhausted response as success.
    category: policy
    applies_to: [TicketReadToolResultV1]
    trigger: A read reaches a terminal outcome.
    logic: IF a valid non-empty projection exists THEN use succeeded; IF a valid list has zero items THEN use empty; ELSE use failed with SafeErrorV1.
    violation_behaviour: Fail closed as failed with upstream_unavailable when the outcome cannot be safely classified.
    source: [FR6.2, FR7.2, NFR1]

  - id: BR4.1
    statement: Every visible state and next action is available as text without dependence on color or icons.
    category: policy
    applies_to: [assistant_reply, TicketPresentationV1]
    trigger: Loading, success, empty, failure, or continuation state is presented.
    logic: IF a state is shown THEN provide its meaning and available next action through the existing text and live-announcement flow.
    violation_behaviour: Omit optional visual metadata rather than returning an inaccessible result.
    source: [FR7.2, NFR9]

  - id: BR4.2
    statement: Presentation metadata is optional and cannot replace the existing assistant text reply.
    category: policy
    applies_to: [TicketPresentationV1, history_response]
    trigger: Presentation is mapped, committed, loaded, or rejected.
    logic: IF metadata is valid and durably correlated THEN attach it; ELSE return the existing text-only reply.
    violation_behaviour: Omit presentation without failing the chat response.
    source: [FR7.2, NFR9]

  - id: BR4.3
    statement: Presentation metadata is exposed only after exact message correlation and commit readback succeed under the conversation lock.
    category: constraint
    applies_to: [presentation_history]
    trigger: Runner completion produces an assistant message and optional presentation.
    logic: IF the persisted conversation, assistant sequence, and text hash match under the lock THEN atomically insert or replace, commit, and read back before exposure.
    violation_behaviour: On mismatch or persistence/readback failure, expose text only and leave no claim of structured success.
    source: [FR7.2, NFR2, NFR9]

  - id: BR4.4
    statement: Tool and presentation registries reject duplicate names, incompatible versions, and unknown kinds.
    category: validation
    applies_to: [RegistryV1]
    trigger: A plugin or mapper is registered or resolved.
    logic: IF the contract version, unique tool name, and allowlisted mapper keys validate THEN register; ELSE fail closed.
    violation_behaviour: Prevent application composition with an ambiguous registry.
    source: [FR1.2, FR7.3, NFR2]

  - id: BR5.1
    statement: One user, session, and conversation may have at most one pending create row and one pending update row.
    category: constraint
    applies_to: [PendingMutationRepositoryV1]
    trigger: A reviewed mutation is saved.
    logic: IF a create review supersedes a create row THEN replace it atomically; IF an update is prepared or revised THEN apply BR5.9 exact-old semantics; preserve the other operation's row.
    violation_behaviour: Roll back the replacement and return failed if atomic replacement cannot be guaranteed.
    source: [FR5.1, FR5.4, NFR2]

  - id: BR5.2
    statement: A newer reviewed summary may replace prior same-operation state only when the operation-specific contract cannot leave competing consumable authority.
    category: policy
    applies_to: [PendingTicketMutationV1]
    trigger: A newer create or update review is persisted for the same actor scope.
    logic: IF a prior create row is pending or confirmed THEN atomically transition it to invalidated before the new create row becomes pending; IF an update review is replaced THEN only the exact pending row accepted by BR5.9 may be invalidated; IF an unexpired confirmed update exists THEN reject new update publication until exact consumption, expiry, or lifecycle invalidation resolves it.
    violation_behaviour: A replaced pending mutation cannot be confirmed or consumed, and a new pending mutation can never coexist with consumable confirmed authority in the same actor scope and operation.
    source: [FR5.4, NFR1]

  - id: BR5.3
    statement: Pending confirmation expires after 15 minutes or earlier on any binding or lifecycle change.
    category: constraint
    applies_to: [PendingTicketMutationV1, lifecycle]
    trigger: Pending state is saved, loaded, confirmed, or lifecycle state changes.
    logic: IF current time is at or after expires_at OR user, session, conversation, operation, payload hash, ticket target, base version, logout, or clear state differs THEN expire or invalidate the row.
    violation_behaviour: Reject confirmation and make zero mutation transport calls.
    source: [FR5.1, FR5.4, NFR1]

  - id: BR5.4
    statement: Confirmation authority comes from server-held CurrentTurnEvidenceV1, not tool arguments.
    category: authorization
    applies_to: [confirmation_bridge]
    trigger: A later Unit requests confirm or cancel.
    logic: IF the normalized current raw user turn is exactly 확인 or confirm and every pending binding matches THEN atomically move the row from pending to confirmed; ELSE do not confirm or authorize mutation transport.
    violation_behaviour: Return needs_explicit_confirmation or invalidated with zero mutation calls.
    source: [FR5.1, FR5.4, NFR1]

  - id: BR5.5
    statement: Lifecycle cleanup invalidates revocable state before identity or conversation reuse.
    category: policy
    applies_to: [user_switch, logout, conversation_clear, session_expiry]
    trigger: A protected lifecycle event begins.
    logic: IF the event changes actor or conversation scope THEN acquire the same conversation lock as Runner, revalidate the expected user-session-conversation after acquisition, invalidate pending rows and cursors, clear presentation companions before matching conversation history, preserve every U2 durable create-attempt ledger row, and commit the terminal lifecycle state before releasing the lock.
    violation_behaviour: Deny reuse of the old capability or state; an in-flight old-scope Runner finishes before cleanup or is discarded by post-Runner revalidation and cannot recreate message or metadata after commit; cleanup retries remain idempotent.
    source: [FR5.1, FR5.4, NFR1, NFR2]

  - id: BR5.6
    statement: Session-expiry cleanup completes before expired authority can reach Runner or ITSM.
    category: authorization
    applies_to: [session_expiry, protected_chat]
    trigger: BR1.6 detects an expired server session.
    logic: IF expiry is detected THEN idempotently invalidate pending mutations and cursor bindings and delete the expired session before returning authentication_required; IF any cleanup step fails THEN fail closed and retry no downstream work.
    violation_behaviour: Runner calls, capability issuance, and ITSM calls remain zero; a later sign-in cannot reuse stale pending or cursor authority, including after process restart.
    source: [FR5.3, FR5.4, NFR1, NFR2]

  - id: BR5.7
    statement: Runner use and session-expiry cleanup for one conversation are serialized by the same conversation lock.
    category: authorization
    applies_to: [session_expiry, protected_chat, presentation_history]
    trigger: A protected chat or expiry cleanup targets a conversation.
    logic: IF a chat or expiry cleanup targets the same conversation THEN acquire the shared conversation lock before reading or deleting SDK history; re-read persisted session expiry immediately before Runner and again after Runner before response or presentation determination; IF expiry is observed after Runner THEN discard the generated response and presentation and complete presentation, SDK-history, revocable-state, and session cleanup while the lock remains held.
    violation_behaviour: Never delete or recreate matching history concurrently; return authentication_required with zero post-expiry presentation or response publication, and leave SDK messages, session, and presentation rows at zero after committed cleanup.
    source: [FR5.3, FR5.4, NFR1, NFR2]

  - id: BR5.8
    statement: U1 consumes an exactly bound confirmed mutation once and returns minimal evidence without granting transport authority.
    category: authorization
    applies_to: [PendingMutationRepositoryV1.consume_once, U2_create_extension, U3_update_extension]
    trigger: A later Unit asks to consume a confirmed mutation before attempting POST or PATCH.
    logic: IF the row is confirmed, unexpired, unconsumed, and every user, session, conversation, operation, payload, target, and version binding matches THEN atomically move it to consumed and return only mutation_id and confirmed_at; ELSE reject without evidence.
    violation_behaviour: Make zero mutation transport calls, return rejected, and never recreate or reuse evidence from a consumed or mismatched row.
    source: [FR5.1, FR5.4, NFR1, NFR3]

  - id: BR5.9
    statement: Update review publication replaces only the caller's expected pending update in one U1 transaction and never writes U3 execution storage.
    category: constraint
    applies_to: [PendingMutationRepositoryV1.replace_update_pending, U3_update_extension, ticket_tools_state_v1]
    trigger: U3 publishes a prepared or revised PendingUpdateMutationV1 after staging its own execution row.
    logic: IF any unexpired confirmed update exists in the same actor scope THEN reject publication; ELSE IF expected_old_mutation_id is null THEN require no pending update in that scope; ELSE require the exact old row to match that scope, operation update, and pending status; only then invalidate that exact old row and insert the new row atomically.
    violation_behaviour: Validation or insertion failure changes neither old nor new U1 state; after a crash between confirm and consume, resume exact consume_once or fail closed until expiry/lifecycle invalidation; broad-selector invalidation, U3-storage writes, and cross-namespace transactions are forbidden.
    source: [FR5.1, FR5.4, NFR2, C-02]

  - id: BR6.1
    statement: U1 verification is deterministic and blocks unexpected external network access.
    category: policy
    applies_to: [contract_tests, walking_skeleton_tests]
    trigger: Automated U1 verification runs.
    logic: IF a model or ITSM response is required THEN use controlled doubles; IF any unapproved external call occurs THEN fail the test.
    violation_behaviour: Mark verification failed and do not claim the acceptance criterion complete.
    source: [FR7.3, FR7.4, NFR9]

  - id: BR6.2
    statement: Continuation-reference strength is enforced by a deterministic 100000-issue contract test over an injectable source boundary.
    category: policy
    applies_to: [cursor_repository_contract_tests]
    trigger: Automated U1 security verification runs.
    logic: IF 100000 references are issued through the production encoding contract THEN collisions must be zero, decoded randomness must represent at least 128 bits, and sequential or timestamp-only patterns must be absent; weak injected sources must be rejected by targeted tests.
    violation_behaviour: Fail verification and keep the continuation-reference requirement unfulfilled.
    source: [FR7.4, C-03]
```

## Rules Summary

| Rule | Category | Summary |
|---|---|---|
| BR1.1 | Authorization | Server session is the sole identity authority. |
| BR1.2 | Authorization | Delegated credentials persist only in the protected server session and are used only at the outbound boundary. |
| BR1.3 | Constraint | One refresh is available per operation. |
| BR1.4 | Authorization | External text is data, never instruction. |
| BR1.5 | Policy | Errors use the exact safe allowlist. |
| BR1.6 | Authorization | Persisted session expiry is checked before capability construction. |
| BR1.7 | Constraint | Session lifetime is an immutable absolute eight hours independent of token refresh. |
| BR1.8 | Authorization | One durable U2 reservation permits at most one exchange and atomic session credential commit. |
| BR2.1 | Validation | First-page list fields are allowlisted. |
| BR2.2 | Validation | Limit defaults to 20 and ranges from 1 to 100. |
| BR2.3 | Validation | Read modes are mutually exclusive. |
| BR2.4 | Authorization | Continuations require exact server-side binding. |
| BR2.5 | Authorization | Raw upstream cursors never cross model/API boundaries. |
| BR2.6 | Constraint | List projections use the exact V1 summary fields. |
| BR2.7 | Policy | A zero-item valid page is empty, not failed. |
| BR2.8 | Validation | Ticket IDs are opaque non-empty values. |
| BR2.9 | Constraint | Ordinary detail projections do not require update evidence. |
| BR2.10 | Authorization | A 404 does not reveal existence or visibility. |
| BR2.11 | Constraint | Internal update reads require exact typed If-Match evidence. |
| BR2.12 | Authorization | Continuation references use at least 128 bits of OS cryptographic randomness. |
| BR3.1 | Constraint | Read operations make at most two HTTP attempts. |
| BR3.2 | Policy | Only allowlisted transient reads retry once after 250 ms. |
| BR3.3 | Policy | Rate limiting never sleeps or retries automatically. |
| BR3.4 | Constraint | Every attempt uses the configured timeout. |
| BR3.5 | Constraint | Each gateway invocation is exactly one HTTP attempt. |
| BR3.6 | Policy | Read result state is succeeded, empty, or failed only. |
| BR4.1 | Policy | Status and next actions remain text-accessible. |
| BR4.2 | Policy | Optional presentation never replaces reply text. |
| BR4.3 | Constraint | Presentation requires locked correlation and commit readback. |
| BR4.4 | Validation | Registries reject duplicate or incompatible entries. |
| BR5.1 | Constraint | Pending confirmation is unique per actor scope and operation. |
| BR5.2 | Policy | Operation-specific replacement never leaves competing consumable authority. |
| BR5.3 | Constraint | Confirmation expires at the earliest time or binding boundary. |
| BR5.4 | Authorization | Only server-held current-turn evidence authorizes confirmation. |
| BR5.5 | Policy | Lifecycle cleanup precedes context reuse while preserving the U2 durable create ledger. |
| BR5.6 | Authorization | Session-expiry cleanup blocks Runner and ITSM until safe. |
| BR5.7 | Authorization | Runner and expiry cleanup share one conversation lock and recheck expiry before publishing. |
| BR5.8 | Authorization | One-time consumption returns minimal evidence but does not itself authorize transport. |
| BR5.9 | Constraint | Update publication atomically replaces only the exact expected U1 pending row and never writes U3 storage. |
| BR6.1 | Policy | Automated verification uses doubles and blocks external calls. |
| BR6.2 | Policy | A 100000-reference test enforces continuation-reference strength. |

## Decision Tables

### Read retry and refresh

| Observed result | Remaining HTTP attempts | Refresh budget | Required action |
|---|---:|---:|---|
| Valid success | any | any | Project the allowlisted result and stop |
| 401 token expired, request known not to have succeeded | at least 1 | 1 | Refresh once and use the remaining final call |
| Timeout, transport error, 500, 502, 503, 504 | at least 1 | any | Wait 250 ms and use the remaining final call |
| 429 | any | any | Do not wait or retry; return failed/rate_limited |
| Other non-success or malformed success | any | any | Do not retry; return failed with the exact safe mapping |

Refresh and transient-retry paths share the same two-attempt ceiling and can never combine into a third call.

### Pending confirmation replacement

| Operation | Expected old | Existing state | Atomic result |
|---|---|---|---|
| create | repository-selected same-scope create | none | New create becomes pending |
| create | repository-selected same-scope create | pending or confirmed create | Old create becomes invalidated; new create becomes pending |
| update prepare | null | no same-scope pending update | New update becomes pending; U3 staged row remains U3-owned |
| update prepare | null | same-scope pending update exists | Reject; old row is unchanged and new row is not inserted |
| update revise | exact mutation ID | exact same-scope pending update | Exact old row becomes invalidated and new update becomes pending in one U1 transaction |
| update revise | missing, mismatched, terminal, or broad selector | any | Reject; neither old nor new U1 state changes and U3 storage is untouched |
| update prepare or revise | any | unexpired confirmed update exists | Reject; resume exact consumption or wait for expiry/lifecycle invalidation, with no new pending row |
| either | any | other operation row exists | Preserve the other operation's row |
