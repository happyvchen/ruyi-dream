# GitHub Admin Settings Checklist

This repository already contains CI, PR template, and CODEOWNERS. The remaining collaboration controls must be enabled by a repository admin because the current automation account only has `WRITE` permission.

## Required Repository Settings

Enable automatically delete head branches:

```bash
gh repo edit happyvchen/ruyi-dream --delete-branch-on-merge
```

Protect `main` with these rules:

- Require a pull request before merging.
- Require at least 1 approval.
- Dismiss stale pull request approvals when new commits are pushed.
- Require review from Code Owners.
- Require conversation resolution before merging.
- Require CI status checks:
  - `Backend smoke`
  - `Frontend build`
  - `Compose config`
  - `Docker image smoke`
- Do not allow force pushes.
- Do not allow branch deletion.

## Last Automation Attempt

On 2026-05-11, Codex authenticated as a GitHub user with `WRITE` permission. Attempts to update repository settings returned `HTTP 404`, which is the expected GitHub API behavior for a private repository when the token cannot administer repository settings.
