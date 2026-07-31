# Evidence-Driven Pull Request Review

A serious systems review is an engineering workstream. Its unit of analysis is
the behavior of the system, not the lines in the diff, and its result is more
than an approval state. A complete review establishes what the change enables,
which contracts make it correct, where those contracts are upheld or violated,
and the shortest credible path to landing.

This method is especially useful for changes involving asynchronous work,
resource ownership, memory management, concurrency, native APIs, security
boundaries, or performance-critical paths. It applies equally to human- and
agent-authored changes. The code and its evidence receive the review, not the
author.

Technical force and constructive collaboration reinforce one another. Authors
can act decisively when findings identify exact mechanisms and consequences,
and a reviewer can request substantial changes without turning the review into
a judgment of the people who did the work.

## Review Contract

The reviewer owns every claim they publish. A plausible concern is a candidate,
not a finding. A passing test is evidence, not proof. Existing implementation
shape is evidence, not authority. Review prose is working memory, not a source
of truth.

Several principles follow from that contract:

| Principle | Consequence |
| --- | --- |
| Review an exact snapshot. | Every observation names the reviewed head and merge base. A moving branch cannot silently change the evidence underneath the review. |
| Start from a production witness. | Architecture is judged against a real caller, deployment, or user-visible behavior rather than a synthetic test convenience. |
| Trace information boundaries. | A component can validate, infer, or own only facts that actually reach it. Hidden reconstruction usually signals duplicate state or a missing contract. |
| Give each resource one terminal owner. | Success, rejection, cancellation, prerequisite failure, shutdown, retry, and reuse all end in an explicit ownership transition. |
| Treat performance as architecture. | Global locks, linear scans, allocations, cache misses, and duplicate bookkeeping are accounted for where they are paid. |
| Falsify before publishing. | The reviewer actively tries to prove each candidate concern wrong and records rejected candidates so they do not reappear later. |
| Attribute fairly. | History separates regressions introduced by the change from defects discovered while reviewing it. Both receive owners, but only the former become author obligations. |
| Make the landing path executable. | A bounded correction experiment can prove that a finding has a practical resolution and that the feature remains viable. |

Requesting changes is a technical state, not a condemnation. When a patch
violates a memory-safety, ownership, synchronization, or public API invariant,
the respectful response is to say so plainly and provide enough evidence to
make the repair unambiguous.

## Start With A Review Charter

The first artifact is a short private charter. It prevents the review from
expanding opportunistically, records the basis for later attribution, and makes
the landing bar explicit before implementation details influence judgment.

```text
Reviewed head:
Merge base:
Objective:
Owned review surface:
Production callers used as evidence:
Explicit non-goals:
Production witness:
Declared product direction:
Strategic capabilities to preserve:
Trust and deployment model:
Landing bar:
High-risk questions:
```

The owned surface and caller evidence are intentionally distinct. A runtime
review may inspect a compatibility layer deeply enough to understand the
operations it requires without accepting responsibility for every compatibility
API in the same patch. This keeps scope bounded while preserving the end-to-end
trace.

The production witness anchors the review but does not define the entire product
ceiling. Declared strategic capabilities remain constraints even when the first
caller does not exercise them yet. A current staged-copy path, for example,
cannot by itself invalidate a deliberate zero-copy contract. The review
distinguishes an unproven aspiration from an explicit product requirement, then
tests proposed simplifications against both the current witness and the
capabilities the system has committed to preserve.

The high-risk questions should expose the design pressure rather than predict
the answer. Useful questions include:

- Which layer is authoritative for each piece of persistent state?
- What information does every operation receive directly?
- Which cleanup steps can fail, and who retains enough state to retry?
- What happens after successful submission if a prerequisite later fails?
- Which costs occur on allocation, submission, reuse, completion, and teardown?
- Which behavior is required by the real caller, and which machinery exists
  only to approximate a stronger contract?
- Which future-facing capabilities are explicit requirements, and which are
  unsupported speculation?
- What trust boundary and deployment model govern validation and security?
- What smaller implementation would preserve the same product capability?

If the PR head changes during the review, the existing findings remain scoped
to the recorded snapshot. The new head receives a deliberate reconciliation:
which mechanisms remain, which were corrected, and which evidence must be
rerun.

## Map The System Before Judging The Diff

A file inventory is useful, but a behavioral inventory is the real starting
point. The change is mapped across public API, ownership, synchronization,
failure, performance, compatibility, and test surfaces.

| Surface | Review pressure |
| --- | --- |
| Public API | Vocabulary, preconditions, ownership transfer, retry semantics, unsupported behavior, and observable completion. |
| Persistent state | Authoritative source, lifetime, memory cost, lookup complexity, and consistency with lower layers. |
| Synchronization | Lock domains, lock ordering, queue dependencies, host waits, callbacks, and completion notification. |
| Failure | Partial initialization, accepted-but-not-executed work, cancellation, teardown, status propagation, and retry ownership. |
| Performance | Path classification, lock contention, allocation frequency, scan complexity, cache behavior, and native call placement. |
| Trust and security | External input boundaries, declared deployment model, authority, capability exposure, and failure containment. |
| Compatibility | Canonical behavior, zero-size and boundary cases, capability truthfulness, and version constraints. |
| Tests | Production fidelity, missing terminal states, sanitizer visibility, hardware coverage, and accidental assumptions. |

### Establish The Baseline

The merge base answers what the PR introduced. Current main answers whether the
surrounding system shifted after the branch diverged. Relevant predecessor
implementations and native dependencies answer what semantics already exist
below the proposed layer.

History is part of correctness analysis. It distinguishes a new regression from
an inherited defect, reveals why an unusual shape exists, and shows whether a
seemingly new abstraction duplicates one that was recently removed or moved.

### Trace A Real Caller

One real caller is traced end to end before declarations or abstractions are
judged. The trace follows:

1. User-visible operation and expected result.
2. Public API validation and ownership transfer.
3. Intermediate wrappers and queued operations.
4. Backend or native operation.
5. Completion, failure propagation, and cleanup.
6. Reuse or destruction of every retained object.

The reverse trace is equally important: start at a new registry, mutex, mapping,
callback, or state field and ask which caller requires it. If every caller
already passes the exact object and range needed by the native API, a global
lookup structure needs a stronger justification than defensive convenience.

The real caller remains an anchor rather than a ceiling. A proposed deletion or
localization also checks the declared product direction, known deployment
models, and deliberate strategy slots. Current tests cannot silently erase a
required zero-copy, heterogeneous-device, cross-process, or queue-ordered
capability merely because the first vertical slice has not reached it.

### Perform A Boundary-Information Check

For every actor, record the facts it receives and the facts it reconstructs.
This catches two common architecture failures:

- validation placed in a layer that does not possess the information required
  to perform it correctly; and
- duplicate truth introduced because an implementation did not notice that a
  lower layer or caller already owns the state.

Existing API vocabulary and native contracts constrain the solution. New nouns,
registries, flags, and status paths require genuinely new semantics rather than
a desire to make local reasoning easier.

### Establish The Trust Boundary

Security review starts from the product's actual authority and deployment
model. Public parsers, protocol decoders, handles crossing processes, and
untrusted API inputs validate at their boundary. Internal state produced by a
verified runtime pipeline is not treated as hostile merely because several
components can reach it.

The threat model is neither minimized nor inflated. A trusted local-network
execution service that accepts arbitrary GPU programs already grants a
different authority than an internet-facing multi-tenant service. Findings
describe the boundary the product claims, the capability exposed across it, and
the consequence of violation. They do not punish the viable deployment model
for failing to satisfy a different product's threat model.

## Trace Lifetimes Through Every Terminal State

Asynchronous code is reviewed as a state machine even when the implementation
is not written as one. The critical question is not merely whether the success
callback runs. It is who owns the operation before and after every possible
terminal event.

A useful ownership table has this shape:

| State | Current owner | May the effect run? | Cleanup owner | Observable completion |
| --- | --- | --- | --- | --- |
| Created | Caller | No | Caller | None |
| Submission rejected | Caller | No | Caller | Returned status |
| Submission accepted | Queue or operation | Not yet | Queue or operation | Pending signal |
| Prerequisites satisfied | Queue or operation | Yes | Queue or continuation | Callback or device completion |
| Prerequisite failed | Queue or operation | No | Queue or operation | Failed signal |
| Cancelled or shutdown | Queue or operation | No | Queue or terminal teardown | Failure or shutdown completion |
| Effect completed | Result owner | Already ran | Result owner | Completed signal |
| Reused or released | New owner or allocator | No | New owner or allocator | API-specific |

This table exposes callback-only cleanup immediately. A queue may accept a host
operation and later skip its callback because a prerequisite failed. Submission
success therefore cannot prove callback execution. State needed only for the
effect may be caller-owned, while internal adapter state still requires a
terminal owner that covers both invocation and cancellation.

The same reasoning applies to device memory:

- logical release can precede completion of queued device accesses;
- an allocator may mark a range free while a death frontier still prevents
  physical reuse;
- unmap, physical release, and virtual-address release may have distinct
  retryable ownership;
- a void teardown callback cannot report a failure after discarding the only
  handles required to recover; and
- reuse transfers ownership only when both dependency and bookkeeping
  transitions are linearizable.

Queue ordering is proven by explicit dependencies, not submission order or
host-side FIFO assumptions. Tests model those dependencies directly and use
the normal completion primitive rather than sleeps, polling races, or arbitrary
deadlines.

## Account For Cost Where It Is Paid

Every edited path is classified as a hot path, cold path, compatibility path,
shutdown path, or error path. That classification determines which costs are
acceptable.

For new persistent machinery, the review records:

- allocation count and lifetime;
- lock domain and contention scope;
- lookup and removal complexity;
- cache footprint;
- native calls made while locks are held;
- duplication of lower-layer state; and
- the workload dimension that makes the cost grow.

A global lock plus an O(process-wide live objects) scan on every allocation is a
different architectural choice from a context-scoped pending list drained at
completion. A host allocation on every map operation is a different choice from
storing required ownership on the reservation and physical-handle objects the
caller already provides.

Some costs are demonstrably unnecessary without a benchmark. If a lower layer
already owns authoritative maps and performs the same validation, and the real
caller already carries exact handles, deleting a second registry removes known
locks, scans, allocations, and consistency states while preserving behavior.
That is an architectural simplification, not a speculative optimization.

Other performance concerns remain hypotheses until a production witness and
measurement exist. The review can record scaling pressure without promoting it
to a landing blocker when no violated contract or demonstrated workload
supports that severity.

## Turn Suspicions Into Findings

A candidate finding moves through an evidence loop:

1. **Observation:** the exact code, state, or behavior that raised concern.
2. **Contract:** the API, ownership, synchronization, compatibility, or
   performance invariant that governs it.
3. **Mechanism:** the concrete sequence of transitions that violates the
   contract.
4. **Consequence:** corruption, use-after-free, leak, hang, false capability,
   wrong result, unbounded cost, or lost diagnostic.
5. **Evidence:** source trace, history, native implementation, targeted probe,
   sanitizer result, hardware behavior, or focused test.
6. **Falsification:** alternate explanations and valid paths that would make the
   concern a non-finding.
7. **Correction:** the smallest mechanism that restores the contract.
8. **Disposition:** correction supplied, author action required, independent
   mainline issue, follow-on, or falsified candidate.

The status of each claim remains visible:

| Status | Meaning |
| --- | --- |
| Observed | Directly present in code, history, or execution output. |
| Inferred | Follows from observed facts but has not yet been exercised. |
| Uncertain | Missing evidence can materially change the conclusion. |
| Confirmed | The mechanism and consequence survived falsification. |
| Corrected | A bounded implementation restores the invariant and is verified. |
| Falsified | Evidence disproved the candidate or showed it does not belong to the PR. |

### Build The Smallest Decisive Probe

The best reproducer isolates the disputed contract without recreating the whole
application. Examples include:

- release one allocation with an unresolved device frontier, trim the pool, and
  inspect whether physical storage was reclaimed;
- request a required memory property from a generic allocator and compare the
  reported capability with the actual allocation;
- enqueue an owned host operation behind a failed prerequisite and verify that
  state is reclaimed without invoking the effect;
- compare a compatibility API boundary case against its canonical runtime; or
- exercise native virtual-memory transitions directly to determine which layer
  already validates ownership and overlap.

The probe runs against the exact reviewed head and, when practical, against the
correction. Sanitizers remain enabled for correctness experiments. Suppression
sets are audited so they cannot hide leaks in the subject under test.

### Falsification Protects The Author

The reviewer tries to disprove each candidate before publishing it. This is not
hesitation; it is how a large review avoids becoming busywork.

Useful falsification questions include:

- Is this behavior introduced by the PR or merely adjacent to it?
- Does another path retain or reclaim the object?
- Can the supposedly skipped callback still run under the documented contract?
- Are two counters actually unrelated, or are they intentionally assigned the
  same timeline value?
- Does the canonical implementation define the surprising behavior?
- Can valid input reach the claimed failure?
- Is the test modeling production ordering, or manufacturing an impossible
  state?

Rejected candidates stay in the private review record with the evidence that
closed them. That prevents repeated investigation and demonstrates that the
public findings are the residue of scrutiny, not an inventory of suspicions.

## Use Correction Experiments As Review Evidence

Architecture findings become far more actionable when a bounded correction
proves the alternative. The correction is built on the exact reviewed head and
targets the hardest ownership boundary first.

A useful correction experiment:

- changes one invariant cluster;
- includes its declarations, implementation, integration, and focused tests;
- preserves the product feature;
- makes ownership and failure behavior simpler;
- avoids speculative adjacent infrastructure; and
- records what remains unresolved.

Commits follow dependency order. A generic lifetime contract lands before its
caller adaptation; a backend ownership simplification lands before
compatibility tests that depend on it. Each commit tells one design story and
can be reviewed independently.

The correction branch is an implementation handoff, not a competing claim of
authorship. The original author can cherry-pick it, adapt it, or reproduce the
same invariants. Its purpose is to demonstrate that the requested changes are
finite and compatible with the feature.

One rejected architecture does not select its replacement. A correction
experiment still satisfies every established caller, performance, ownership,
trust, and strategic-capability requirement. When no candidate has crossed that
bar, the honest output is a bounded experiment or a required invariant, not a
prematurely declared design.

Before publication, the branch is curated. Corrections for defects that predate
the PR move to an independent mainline path. Experimental commits, mixed
ownership, and obsolete approaches are removed so the branch never assigns the
author work that belongs elsewhere.

## Track A Long Review Continuously

A long review benefits from three private artifacts with different jobs.

### Review Overview

The overview records:

- exact head and merge base;
- objective and scope;
- current disposition;
- confirmed blockers;
- corrections already proven;
- remaining author actions;
- independent issues discovered during review; and
- the next concrete investigation or integration step.

This is the recovery point after interruption. It changes whenever the review's
direction or disposition changes.

### Evidence Notebook

The notebook holds the high-volume engineering work:

```text
Review position
Hard questions
Diff and state inventory
Production caller trace
Native or predecessor ownership trace
Queue ordering and teardown trace
State, synchronization, and cost analysis
Candidate findings
Falsified candidates
Confirmed findings
Correction experiments
Verification
Final disposition
```

Raw commands and environment details belong here rather than in public review
prose. The notebook records mechanisms and evidence, not a transcript.

### Issue Graph

The issue graph carries execution state. One umbrella item owns the review.
Confirmed findings, correction experiments, and remaining repairs become
separate items with explicit dependencies. A landing gate depends on every
required correction instead of relying on a prose checklist.

High-frequency updates preserve:

- the exact observed mechanism;
- severity and reachability;
- whether the PR introduced it;
- correction commit or required design;
- verification status; and
- blocking relationships.

Suspicions stay in the notebook until confirmed. Falsified candidates close
with their evidence. A corrected issue closes only after its implementation and
tests have a durable home. A newly discovered inherited defect receives its own
mainline owner rather than disappearing or remaining attached to the PR.

Major transitions are integration checkpoints: after initial mapping, after
confirming a P0, after each correction cluster, before composing public
feedback, and before posting. At each checkpoint, the overview, issue graph,
branch state, and actual diff must agree.

## Assign Severity And Disposition Fairly

Severity follows consequence and reachability, not diff size or reviewer
discomfort.

- **P0:** reachable memory corruption, use-after-free, release of device-live
  storage, security-boundary failure, deadlock, or nonterminating valid work.
- **P1:** deterministic resource leaks, broken ownership or retry contracts,
  false public capabilities, wrong API behavior, or severe unbounded scaling on
  a production path.
- **P2:** accounting errors, incomplete diagnostics, meaningful coverage gaps,
  or contained policy problems that do not invalidate core correctness.

Context can move an issue between levels. A coverage gap is not automatically a
blocker, but missing coverage for a brand-new native ownership implementation
raises the uncertainty of every associated contract.

Every confirmed issue receives one disposition:

| Disposition | Public treatment |
| --- | --- |
| Corrected in handoff | Explain the mechanism and link the correction. |
| Remaining landing blocker | State the required invariant and bounded repair. |
| Follow-on | Explain why current behavior remains correct and what future pressure motivates the work. |
| Independent mainline defect | Exclude it from author obligations and state that it has a separate owner. |
| Falsified candidate | Keep it out of the public finding list. |

History establishes attribution, but inherited code is not ignored. The fair
shape is separate ownership: this PR repairs what it introduces, while the
reviewer or another mainline owner carries independently discovered defects.

## Write A Constructive Changes-Requested Review

The public review is a reviewer-facing design and landing document. It is not a
session log, command transcript, or dump of every concern considered.

A strong structure is:

1. **Concrete appreciation.** Name what the contribution enables and what the
   real caller taught the project.
2. **Scope and snapshot.** State the reviewed head and owned technical surface.
3. **Outcome.** Say that changes are required and summarize the blocking
   mechanisms without euphemism.
4. **Corrections already implemented.** Link executable fixes and explain what
   each proves.
5. **Remaining required changes.** Give each blocker a contract, mechanism,
   consequence, and required invariant.
6. **Landing shape.** Put the work in dependency order and show that it is
   bounded.
7. **Forward-looking close.** Explain why completing the work strengthens the
   feature and shared system.
8. **Scope correction.** Identify any inherited issue that was deliberately
   removed from the author's action list.

Each public finding answers five questions:

```text
What contract applies?
What exact code violates it?
What sequence produces the failure?
What user or system consequence follows?
What invariant must the correction restore?
```

Exact source links point at the reviewed commit, so line drift cannot make the
review ambiguous. Referenced correction branches are pushed before the review
is posted.

The tone stays positive by being useful, not by softening severity. The review
criticizes mechanisms, state models, and contracts. It never speculates about
author competence or intent. A large review can explicitly say that the domain
is unforgiving and the contribution supplied valuable pressure, while still
marking a P0 as a P0.

A reviewer may privately feel alarm, frustration, or disbelief. Public prose
translates that energy into contract, mechanism, consequence, evidence, and
action. This keeps the full technical force while giving the author something
they can repair rather than an emotional burden they must first defuse.

Structural findings usually read better as one coherent review than as dozens
of disconnected line comments. Localized mistakes still benefit from inline
comments. The public response contains only confirmed issues that affect
landing; raw exploration, private paths, tool quirks, and rejected candidates
remain private.

## End-To-End Review Procedure

| Phase | Work | Exit gate |
| --- | --- | --- |
| 1. Freeze | Fetch the PR, record exact head and merge base, inspect tree state. | Every later source reference can be reproduced. |
| 2. Charter | Define objective, owned surface, caller evidence, non-goals, witness, and landing bar. | Scope is broad enough for correctness and narrow enough to finish. |
| 3. Map | Inventory API, state, synchronization, failure, performance, compatibility, and tests. | Every new persistent object and operation family has an owner hypothesis. |
| 4. Trace | Follow one real caller through native execution, completion, cleanup, and reuse. | Boundary information and authoritative state are known. |
| 5. Stress | Enumerate rejection, delayed execution, prerequisite failure, cancellation, retry, shutdown, and concurrent reuse. | Every resource has one terminal owner in every reachable state, or a candidate finding exists. |
| 6. Measure | Classify path costs and identify locks, scans, allocations, and duplicate truth. | Performance concerns distinguish demonstrated cost from unmeasured pressure. |
| 7. Prove | Build focused reproducers, inspect canonical behavior and history, and try to falsify every candidate. | Public findings have mechanism, consequence, and evidence. |
| 8. Correct | Implement bounded vertical corrections for the hardest findings when feasible. | The proposed landing path is executable and preserves the feature. |
| 9. Classify | Separate corrected blockers, remaining blockers, follow-ons, inherited defects, and non-findings. | The issue graph and overview agree with the code and correction branch. |
| 10. Compose | Write the public review from value through landing shape. | Every request is actionable, linked, and attributable. |
| 11. Audit | Reconfirm PR head, branch availability, public links, and absence of private artifacts. | The response can be posted without qualification. |
| 12. Follow Through | Reconcile revisions, verify fixes, and carry independent issues to mainline. | Changes-requested state is cleared only when the governing contracts hold. |

## Reusable Templates

### Private Review Record

```markdown
# Review: <PR and subsystem>

Status:
Reviewed head:
Merge base:
Objective:
Owned surface:
Caller evidence:
Non-goals:
Production witness:
Declared product direction:
Strategic capabilities to preserve:
Trust and deployment model:
Landing bar:

## Hard Questions

## Diff And State Inventory

## Caller And Boundary Trace

## Ownership And Failure Trace

## State, Synchronization, And Cost

## Candidate Findings

## Falsified Candidates

## Confirmed Findings

## Correction Experiments

## Verification

## Disposition
```

### Finding Record

```markdown
Title:
Severity:
Status:
Introduced by this PR:
Exact symbols or callsites:
Path classification:
Trust boundary:
Strategic constraints:

Contract:
Observed mechanism:
Reachable sequence:
Consequence:
Evidence:
Falsification attempted:
Required invariant:
Correction or handoff:
Verification:
Public disposition:
```

### Public Review Response

```markdown
<Concrete appreciation and product value.>

<Reviewed head, scope, and caller framing.>

## Review outcome

<Direct changes-requested result and concise blocker summary.>

## Corrections already implemented

1. **<Finding and invariant>.** <Mechanism, correction link, and evidence.>

## Remaining changes required

1. **<Finding and invariant>.** <Mechanism, consequence, and bounded repair.>

## Landing shape

<Dependency-ordered path from current branch to a correct landing.>

<Forward-looking close explaining why completion is valuable.>

<Independent issue attribution, when applicable.>
```

## Case Study: Virtual Memory And Async Ownership

[PR 231](https://github.com/ROCm/hrx-system/pull/231) motivated this guide. It
added an end-to-end HRX/HIP virtual-memory and memory-pool path. The review
owned HAL and AMDGPU correctness while treating HRX/HIP as the production
caller.

The investigation started from the caller's slab object. It already retained
the virtual reservation, physical handle, mapped length, and mapping state.
Tracing downward showed that ROCr already owned authoritative reservation,
mapping, overlap, and handle-reference tables under its native memory lock.
That evidence falsified the need for a second allocator-global AMDGPU registry
and exposed its global mutex, linear scans, per-map allocation, and
false-success release path as removable complexity.

The async lifetime trace then found a separate P0. TLSF considered a slab
logically idle when its allocation count reached zero, but the coalesced free
range could still carry an unresolved death frontier for queued GPU work.
Trimming could therefore return physical storage while the device still owned
it. A focused reproducer made the transition visible, and a correction reused
the pool's nonblocking completion predicate without adding persistent state or
host waits.

Following accepted host calls through failed prerequisites found another
ownership gap. Backends correctly skipped the effect callback, but a staged
device-to-host operation had made callback execution its only cleanup path. The
correction separated effect from destruction: an optional retained resource
owned internal state until either callback return or terminal cancellation.

Smaller probes established false uncached-memory capability reporting, incorrect
zero-size HIP allocation behavior, a retained graph pool leak, mixed-granularity
VMM rejection, and missing physical-allocation statistics. Candidate concerns
that did not survive history, canonical implementation comparison, or caller
tracing were recorded as non-findings and omitted from the public response.

The correction branch was built in dependency order on the exact PR head:
frontier-safe TLSF trimming, registry-free AMDGPU VMM, generic host-call
terminal ownership, staged-copy adoption, and HIP pool lifetime fixes. Before
publication it was curated to remove fixes for inherited callback adapters. A
separate queue conversion defect found during the review was assigned to
mainline instead of being charged to the PR author.

The final changes-requested response began by explaining why the feature and
its concrete caller were valuable. It stated the P0 and remaining ownership
failures directly, linked the correction branch, bounded the unresolved work,
and closed with the stronger system those repairs would produce. The result was
structurally demanding feedback that remained collaborative because every
request had evidence, a reason, and a viable path forward.

## Completion Standard

A review is complete when:

- the exact reviewed snapshot and baseline are recorded;
- a real caller has been traced through its hardest ownership boundary;
- all persistent state, synchronization, and terminal cleanup have named
  owners;
- every public finding survived an explicit falsification attempt;
- severity follows a reachable consequence;
- correction experiments prove the difficult parts of the landing path where
  practical;
- inherited defects have independent owners rather than disappearing or
  becoming author obligations;
- the tracking artifacts, code, tests, and public disposition agree; and
- the author can read the response, understand why each change matters, and
  begin the repair without reverse-engineering the reviewer's intent.
