---
project: <workspace-slug>
title: <Workspace Title> — the cross-system design
status: draft
created: <YYYY-MM-DD>
origin: <who asked, why>
audience: the next AI agent or engineer picking this up
priority: P0
---

# <Workspace Title> — the cross-system design

**One line:** <what this system does, for whom, in one sentence.>

This document covers **only what crosses a system boundary**: the shared identity, the
authorization model, the agent surface, hosting and data protection, and the way the applications
talk to each other. Everything else lives in each repository's own `PROJECT.md`.

> **This file is a skeleton.** The sections below are the questions a family of applications has to
> answer *once*, together; the placeholders are where this workspace's answers go. Delete the
> guidance prose when you fill a section, and keep the decisions register honest — it is the reason
> the document exists.

---

## 1. The system at a glance

| Repo | One line | Tier |
|---|---|---|
| `<repo>` | <what it does> | P0 |
| `<repo>` | <what it does> | P1 |

Two kinds of member exist, and the distinction matters for how work is scheduled:

| Kind | What it is | What it implies |
|---|---|---|
| **Application** | a running service with a UI and/or API, registered in `apps.json` with a port | has a golden path and a `demo.md` |
| **Library** | a shared module consumed by applications, or a tool with no service | its own issues; consumed by many |

The registry — `apps.json` in `<meta-repo>` — is the machine-readable version of this table. If
the two disagree, the registry wins and this table is fixed.

## 2. The workspace pattern

A workspace is a **container of sibling clones plus one meta repository**:

```
<workspace>/
├── <meta-repo>/          the registry, the factory tooling, these documents
├── <repo>/               one application or library
├── <repo>-issue-<iid>/   a worker's git worktree (never a repository of its own)
└── shared/
    ├── <ui>/             the shared UI library
    └── <go>/             the shared backend module
```

- The workspace root is **not** a git repository. It may carry a shared `opencode.json` (the model
  pin every worktree inherits) and nothing else that belongs to a repository.
- Every member is its own repository with its own issues, its own `AGENTS.md` (copied from the
  meta's and specialised), and its own gate.
- Shared code is a member too: applications consume it through committed paths (a `replace`
  directive, a local package) and never fork it.

## 3. The keystone — one identity per thing that matters

Every family has one entity that everything else references, and getting it wrong is the most
expensive mistake in the document. For this workspace it is **<the keystone: e.g. one global ID per
person, per club, per device>**.

Write down, in this section:

- **What the keystone entity is**, in one sentence.
- **Which repository owns it** (there is exactly one).
- **How everything else references it** (an opaque ID, never a name; never a copy of the
  attributes).
- **What is derived from it** (projections, public views, search copies) and the rule that a
  derived copy is never authoritative.

> **One copy, many projections.** A second writable copy of the keystone is the defect this section
> exists to forbid; a projection is allowed only if it can be rebuilt from the keystone at any
> time.

## 4. Identity and authorization — decide once

- **Who is calling, and are they allowed to.** One identity provider and one policy decision point
  for the whole family; applications assert facts and do not decide access.
- State the split explicitly: <the identity service authenticates>; <the policy layer decides>;
  the application **asserts facts, it does not decide access**.
- Name where the policy lives (files in the policy repository, versioned with it), how it is
  tested, and what a policy change costs.
- State whether authorization is P0 or deferred. **The default in this factory is P0** — a family
  that cannot be demoed without authorization is a family whose authorization cannot be bolted on
  later. If a workspace defers it, that is a decision with a date and a reason, recorded in §7.

## 5. The agent surface

If agents operate the system (through an API, a hub, or tool calls), write down:

- **The single input** where an agent — or a human — touches the whole system.
- **What an agent may do without a human**, and what always needs one. Publishing, spending money,
  and touching another party's environment are the classic "always needs one".
- **The audit boundary**: what an agent action records, and where it is answerable afterwards.
- **The prompt-injection boundary**: which content an agent may treat as instructions, and which it
  must treat as data. Untrusted text is data, always.

## 6. Hosting, residency and data protection

- **Where the system runs** and under which jurisdiction; one row per environment.
- **What data classes exist** (e.g. personal, sensitive, media, contracts) and which registry
  entries carry them.
- **Who is a processor, who is a controller**, and what that means for each data class.
- **The sub-processor list** and where it is maintained (a document, not a database).
- **Retention and erasure** per data class, including the carve-outs and why they exist.
- **What the audit trail can and cannot evidence** — a named account, not a claim.

A workspace that handles other people's data does not get to defer this section.

## 7. Decisions — settled and open

Every cross-system decision gets a number, a date, the reasoning, and a status. **A decision is not
a preference**: it is written down so the next agent does not re-derive it, and so a reversal is
visible rather than silent.

### Settled

| # | Decision | Date | Why |
|---|---|---|---|
| 1 | <decision> | <date> | <reason> |

### Open — the grilling agenda

| # | Question | What it changes |
|---|---|---|
| 1 | <question> | <what it changes once answered> |

Open decisions are worked as issues, not at the coordinator's desk (see `AGENTS.md` § *Roles*).
When one is answered, it moves up and the issue is closed with the reasoning linked.

## 8. How the applications talk to each other

- **The registry is the contract for plumbing** (ids, ports, URLs); the API contracts are the
  contract for data.
- State the direction of every call. A family with mutual runtime dependencies has a start order
  problem; a family where calls flow one way does not.
- State what happens when a dependency is down: which surface degrades, which refuses, and which
  silently fails. Silent is never an acceptable answer.

## 9. The customer is a tenant, not a constant

This section is a hard rule in this factory, not a suggestion:

- The product is for the market in general; the first customer is the **design partner and seed
  data, not a requirement**.
- **No customer's name, logo, colours, legal entity, locales or editorial voice may appear in code,
  in a default, or in a seed that ships to another customer.** One deployment per customer,
  configured from data.
- A customer's house style is per-customer data seeded from a session with them, never a built-in
  default — otherwise the second customer silently inherits the first customer's voice.
- The default in a fresh checkout is **obviously synthetic**.
- `AGENTS.md` and this document may name the design partner as evidence; **code may not**.

## 10. Delivery, support and the boundary

- **What ships** (containers, versions), **how a release is cut**, and **how it is rolled back**.
- **Who is on the hook for support**, and what the response expectations are.
- **The processor boundary**: what the workspace operator may see, and what it explicitly may not.

## 11. Glossary

| Term | Means |
|---|---|
| <term> | <definition> |

Keep this list short and use the terms consistently — the glossary is what stops two repositories
inventing two names for one thing.
