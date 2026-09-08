# Worktrees and concurrent sessions

Use one Git worktree per concurrent line of work. Each directory has its own
checkout of the contract, source files, and local ConstraintLoop state. Linked
and detached worktrees are supported, including contracts below the Git root.

## What is isolated

For Git projects, state lives under
`.constraintloop/state/checkouts/<scope>/` in the selected project. The scope
includes the resolved project path, worktree Git directory, and full branch
reference. Detached checkouts use the HEAD commit instead of a branch.

Evidence, session goals and retries, waivers, advisory acknowledgments, loop
journals, challenge work, and supervisor leases all use this directory. Setting
`CONSTRAINTLOOP_CACHE_DIR` relocates storage while preserving project and checkout
separation. Symlink aliases of the same project resolve to the same scope.
Projects outside Git continue using `.constraintloop/state/` directly.

Evidence digests also include HEAD, the contract, and watched file bytes. An
empty commit therefore makes earlier evidence stale even when watched files
are identical. Retry budgets persist across commits on the same branch. Switching
branches selects that branch's state; switching back resumes its budgets and
reuses evidence only if its inputs still match. A checkout change detected during
evaluation or supervision prevents completion and requires another run.

This does not make concurrent branch switching in one directory safe. Sessions
there still share files and commands; use separate worktrees for independent tasks.

## Set up a line of work

Create a worktree from the desired starting branch or commit:

```bash
git worktree add -b task/worktree-fix ../project-worktree-fix HEAD
```

Bootstrap dependencies and local environment files in the new directory using
the project's normal setup process. Git does not copy ignored virtual environments,
secrets, or local hook settings. Then install hooks with the ConstraintLoop
executable intended for that worktree:

```bash
constraintloop setup --adapter all --project ../project-worktree-fix
constraintloop doctor --deep --project ../project-worktree-fix
constraintloop explain --json --project ../project-worktree-fix
```

If the contract is in a subproject, pass that directory, for example
`--project ../project-worktree-fix/packages/atool`, to each command. Generated hooks
retain this subproject path relative to the active worktree root. Start the coding
session in that worktree. Inspect `state_directory` in `explain --json` to verify
that sessions intended to be independent have different scopes.

## Containers and integration tests

Git worktrees isolate files; they do not isolate the Docker daemon, host ports,
databases, or test cleanup. ConstraintLoop does not allocate these resources.

Configure the consuming project's container setup with distinct Compose project
names, host ports, and named resources where required. TestContainers suites need
their own resource and cleanup isolation; a Compose project name alone does not
provide it. Until the project's integration runner supports concurrent execution,
serialize those test runs across worktrees. `doctor --deep` checks declared local
prerequisites and Docker availability, but cannot certify arbitrary test code's
container isolation.

## Existing state after upgrading

Earlier unscoped Git state remains on disk but is not imported into any checkout
scope, because its originating branch is unknown. Run the gates again. Start a
fresh challenge cycle if the contract uses challenges. `doctor` shows the active
state directory; no manual cache deletion or contract weakening is needed.
