# Pipelines — Dagger v1

Read this before writing or changing a pipeline. The supported flavor is **Dagger v1 with a thin
CI wrapper, measured on GitLab**: **every pipeline is a Dagger module written in Go**, `.gitlab-ci.yml`
is a thin wrapper whose only job is to call it, and no `script:` block does real work.

**Why:** the pipeline and the workstation must run *the same code*. A `script:` block is duplicated
logic that drifts, and it can only be tested by spending CI quota. **GitLab quota is scarce — run the
pipeline locally before pushing.** Never push to find out whether a command was right.

The reference is a pipeline repository kept in the workspace as a checkout — **read its
`ci/README.md` end to end before you write a pipeline.** It runs on **Dagger v1**
(`engineVersion = "v1.0.0-beta.14"`) and its layout is **not** the v0 one; everything below is a trap
that cost real time there.

A copy is in the workspace so you do not stop on a permission prompt:

```
<workspace>/.reference/<reference-pipeline>/
```

## The v1 layout

```
dagger.toml                 [modules.ci] source = ".dagger/modules/ci"
dagger.lock                 pinned module versions
.gitlab-ci.yml              the wrapper: one job per exported function
.dockerignore               exclusions for the IMAGE (different from .gitignore)
Dockerfile
.docker/modules/ci/         <- note the leading dot; this is not ci/
  dagger-module.toml        name, engineVersion, [runtime] source = "go"
  main.go                   exported funcs: Lint, Test, Build
  checks.go  errors.go
  go.mod  go.sum            GENERATED WITH PINS — do not hand-edit
  dagger.gen.go  internal/  GENERATED, and COMMITTED in v1
```

```sh
dagger call ci lint --repo .      # the module name is lowercase
```

`.gitlab-ci.yml` reduces to a `docker:*-cli` image with a `docker:dind` service, the Dagger CLI
installed, and one job per function: `dagger call ci lint --repo .`

## Six things that will bite you

**1. `go.mod` carries generated pins — `go mod tidy` breaks it.**
A plain `go mod tidy` pulls `otel v1.47.0`, which conflicts with the `otel/log v0.16.0` replaces and
produces an import cycle. **Do not hand-edit it and do not tidy it.** `dagger generate` produces the
working version.

**2. In v1 the generated bindings are COMMITTED.** `dagger.gen.go` and `internal/` are in git — that is
the opposite of v0, where they were gitignored, and it is what makes `go build ./...` work in the module
without codegen. **They are also `linguist-generated`, so do not read or edit them.** After changing an
exported signature: `dagger generate -y`, and if `main.go` no longer compiles against the old bindings,
**delete `dagger.gen.go` and `internal/` first** — the generator then reports `could not load before
this run's changes; loads with them applied` and produces correct bindings anyway.

**3. `--ws` is advertised by the v1 CLI and then rejected.** It accepts the flag, the workspace arrives
nil, and the first call panics. **Take the repository as a `*dagger.Directory` argument.** Never
`dag.CurrentModule().Source()`.

**4. `.gitignore` filters the upload, but only inside the engine.** `repo()` applies `.gitignore` rules
after the directory is uploaded whole. A clean CI checkout is a few MB; a workstation used as a
`DATA_ROOT` is gigabytes and the call is slow there. Delete the data directories before invoking, or
accept the wait — and note `.dockerignore` is a *different* list: exclusions about the image rather than
the upload.

**5. CI has no Docker socket, so the engine needs one provisioned.** Jobs run in `docker:*-cli` with a
`docker:dind` service and `DOCKER_HOST` pointing at it over TLS, which means **`privileged = true` on the
runner**. Without a driver the CLI falls back to `image://` and fails with `driver for scheme "image" was
not available`. This is the one requirement not verifiable from the repository.

**6. Engine contention masquerades as an OOM.** `buildctl dial-stdio has exited with exit status 137`
looks like a memory problem and is usually another `dagger call` holding the engine. **With a fleet of
workers this will happen** — check for other callers before believing it:
```sh
ps -eo pid,etimes,args | grep 'dagger call' | grep -v grep
docker rm -f $(docker ps -aq --filter name=dagger-engine)   # stale engines from older CLI versions
```

## Publishing, and which credential is which

**The registry password goes in a file, never on a command-line argument** — it is written to
`/tmp/registry-password`, `chmod 600`, and passed to the module as a *path*. **Outside the workspace, so
`--repo .` never uploads it.**

| Where | Credential | Notes |
|---|---|---|
| **CI publishes** | `CI_JOB_TOKEN` (`gitlab-ci-token`) | Free tier, registry write to its own project, creates the repository on first push. **No deploy token needed.** |
| **Workstation push** | **deploy token** (`read_registry` + `write_registry`) | Free, scoped to one project, not tied to a person |
| Fine-grained PAT | — | **Cannot push container images.** Container Repository offers Read and Delete only; there is no Create or Write. |

A deploy token named `gitlab-deploy-token` is also exposed to that project's CI jobs as
`CI_DEPLOY_USER` / `CI_DEPLOY_PASSWORD`; one named anything else is not.

The value never goes in a repository, an issue, a commit, a log or a brief. Local copies live in
`<workspace>/.secrets/` (mode `0600`, outside every repo); `.secrets/README.md` is the committed
template and carries variable **names** only. **A token pasted into a conversation has been
disclosed — rotate it.**

## Large gitignored artifacts travel as generic packages

For anything too big to commit — a production snapshot, a fixture corpus — pack it, hash the content,
upload it as a **GitLab generic package**, and commit the version. `ci/publish-snapshot.sh` does this:
the package version is the archive's **content hash**, recorded in the committed `ci/snapshot-version`,
so the build job reads a file rather than a CI variable and a change shows up as a one-line diff.

Make the hash meaningful: build the tarball deterministically (`--sort=name`, fixed mtime, uid/gid 0,
`gzip -n`), or every run mints a new version for unchanged content. Generic packages rather than the
container registry because they need no privileged runner, the version is a free-form string, and
`CI_JOB_TOKEN` can read them.

## The gate itself does not change

`GOPROXY=off` is not optional inside the module: `go vet`, `go test -count=1`, `gofmt -l`, plus frontend
`typecheck`/`test`/`lint` where a `web/` exists. A Dagger function that shells out to `go` *without*
`GOPROXY=off` is a gate that only fails when the network does.

**Assert it.** The module holds these checks rather than the Makefile targets:
`TestTheOfflineGateIsWhatCIPasses` fails if a CI step is not a `dagger call`,
`TestEveryFunctionThePipelineCallsIsExported` catches a renamed function, and the module's syntax tree is
read so the `GOPROXY=off` claim is checked inside the function rather than in a Makefile the pipeline no
longer runs.

**Two guards that ask git, not the disk** — because a required file that is on disk but *ignored* is
invisible to `git status` and passes every check that reads the working tree. That is how
`ci/dagger.json` shipped once and broke a fresh clone:
`TestEveryFileTheModuleNeedsIsCommittedAndNotMerelyPresent` (`git ls-files`) and
`TestNoIgnoreRuleSwallowsAFileTheModuleNeeds` (`git check-ignore --no-index` — without `--no-index` git
skips tracked files and the test cannot fire). **Run the gate from a fresh clone, not from the worktree.**

Per the family conventions, docker-compose is the floor: the module must be able to **start, upgrade and
back up** one app as one compose file on one host, not merely lint and test.

This is a workspace-wide convention: the pipeline is owned across repositories, and adding an
application does not license a second pipeline shape.

## The Dagger reference lives inside the workspace

The reference pipeline repository's `ci/` is the working implementation the family conventions point at,
but that repository sits **outside this workspace**, so an agent reading it stops on a permission prompt
every single time — and a worker that stops is a worker that does not read the traps.

A copy, without the generated files, is at:

```
<workspace>/.reference/<reference-pipeline>/
  README.md                     <- read END TO END; the design notes are the value
  main.go  checks.go  errors.go  dagger.json
  go.mod  go.sum                <- PINNED. copy verbatim, never regenerate
  gitlab-ci.yml.reference       <- the 124-line wrapper, for shape only
```

`dagger.gen.go` and `internal/` are deliberately absent: they are generated and gitignored, so a fresh
clone regenerates them — which is exactly why the `go.mod`/`go.sum` pin matters.

The workspace root is allow-listed in `~/.config/opencode/opencode.json` for `read` and
`external_directory`, because worktrees have to be siblings inside the workspace (apps consume shared
code by relative `replace`) and every worker legitimately reads the reference for decisions.
Workers blocked on this within a minute of dispatch; approving each interactively would mean
approving it again for every worker that follows.

**When a worker blocks on a permission prompt, approve it — do not tell it to work around the file.**
Working around it means the traps go unread, and the traps are the reason for pointing at the reference.

> **A GitHub-native pipeline is a different flavor.**
> It is not yet extracted.
