# Overnight Task Runner

This repository contains the reusable overnight task runner engine and local dashboard.

## Current scope

- Read tasks from a local backlog file.
- Register multiple target repositories and assign each task to one repository.
- Process each task independently in its own reusable git worktree and task branch.
- Run Codex locally to implement each task inside its worktree.
- Create an incremental git commit after each executor attempt that produces changes.
- Run task-specific tests with up to 2 retries when a validation command is configured.
- Persist task logs and run summaries under `<repo-root>/automation/runs/`.
- Keep task worktrees outside the repository by default so you can inspect and resume them after a run.
- Push task branches and create draft GitHub pull requests automatically when validation passes.
- Bootstrap each task branch from the selected base branch the first time, then keep reusing that task branch for later retries.

## Runtime state

- Runtime state is intentionally kept in the target repository, not in `Automator`.
- Default paths are:
- backlog: `<repo-root>/automation/backlog/tasks.json`
- attachments: `<repo-root>/automation/backlog/attachments/`
- runs: `<repo-root>/automation/runs/`
- worktrees: `/tmp/kiubo-automation-worktrees/<repo-name>`
- The tracked sample backlog in this repo is `backlog/tasks.example.json`.

Backlog supports a repository registry:

```json
{
  "defaults": {
    "repository_id": "default",
    "base_branch": "main",
    "working_directory": ".",
    "retry_limit": 2,
    "executor": { "type": "codex" }
  },
  "repositories": [
    { "id": "default", "path": "/absolute/path/to/repo-a" },
    { "id": "api", "path": "/absolute/path/to/repo-b" }
  ],
  "tasks": [
    {
      "id": "example-task",
      "title": "Example task",
      "repository_id": "api"
    }
  ]
}
```

When a run starts, branch/clean checks are validated per repository used by the selected tasks.

## Folder structure

```text
Automator/
  backlog/
    tasks.example.json
  overnight_runner/
    backlog.py
    git_ops.py
    models.py
    runner.py
  local_dashboard/
  tests/
    test_pipeline.py
  main.py
  dashboard.py
  README.md
```

## Prerequisites

Set a GitHub token before running the pipeline:

```bash
export GITHUB_TOKEN=your_token_here
```

Use a fine-grained personal access token with repository access and at least:

- `Contents: Read and write`
- `Pull requests: Read and write`

The runner also expects the local `codex` CLI to be installed and available in `PATH`.

If you launch the local dashboard, export `GITHUB_TOKEN` before starting it. If you change the token later, restart `python3 dashboard.py` so new runs inherit the updated environment.

Attachment diagnostics are enabled by default in log mode. Control strictness with:

```bash
export CODEX_ATTACHMENT_GUARD=log   # default: record attachment anomalies in events.jsonl
export CODEX_ATTACHMENT_GUARD=fail  # fail the attempt if anomalies are detected
export CODEX_ATTACHMENT_GUARD=off   # disable attachment guard checks
```

## Run the runner

```bash
python3 main.py --repo-root /absolute/path/to/target-repo
```

Runs always keep their worktrees and always try to publish a pull request when validation passes.

Optional flags:

- `--task-id <id>` to run one task.
- `--max-tasks <n>` to limit the batch size.
- `--worktrees-root <path>` to control where reusable task worktrees are created.

## Run the local dashboard

```bash
python3 dashboard.py --repo-root /absolute/path/to/target-repo
```

Then open:

```text
http://127.0.0.1:8765
```

The dashboard lets you:

- create, edit, and delete backlog tasks
- register repositories (`id` + absolute `path`) with validation
- attach reference files and photos to each backlog task
- choose which repository each task runs in
- move reviewed tasks into a `Done` section without losing their history
- launch all tasks or only selected tasks
- inspect the reusable branch and worktree assigned to each task id
- monitor live run state by polling the run logs
- inspect runner stdout/stderr plus per-task event and test logs
- follow the pipeline stages from `Backlog` through `Review`

Tasks moved to `Done` stay in the target backlog file, but new runs skip them until you move them back into the active backlog.

`test_command` is optional. If you leave it empty, the runner will skip validation for that task and record that tests were skipped.
