# Contributing

## Branches

`main` is the released line. Other installs pull it: the maintainer's work deployment and anyone running junto-memory. Keep it deployable.

- **Do new work on a branch:** `feat/<topic>` for features, `fix/<topic>` for fixes, `docs/<topic>` for documentation.
- **Push the branch early and often.** Pushing is a safety net, not a release.
- **Merge to `main` only when the work is proven:** tests and lint pass, and the change has run clean on a live deployment. For the maintainers, that deployment is the home box.
- **Merge through a pull request.** `main` is protected: it needs a pull request and green CI (lint, test, docker).
- **Exception:** a small, urgent fix to something broken in production may be pushed straight to `main` by a maintainer, noted afterwards.

## Before you push

```bash
ruff check src/
PYTHONPATH=src python -m pytest tests -q
```

CI runs the same lint and tests, plus a Docker build.

## Reviews

Adapted from [orbit-spec](https://github.com/las-sal/orbit-spec) by Sal, which measured these on its own history.

- **Keep a pull request small.** If it changes more than 3 tools or areas (messaging, specs, auth, facets, …), split it. On orbit, changes touching more than 5 capabilities took 12–17 review rounds each; one touching 2 converged in a week.
- **Review risky changes twice, blind.** Wire shapes, auth and data migrations get a fresh-context Claude review and a Copilot review on the PR. Neither sees the other's findings until both are in.
- **Walk the findings one at a time.** Before fixing a finding, re-check it against the current branch. If it no longer holds, say why and close it; don't fix it a second time.
- **Severity means consequence.** *Critical*: a user path breaks or data is lost, and the finding includes a reproduction. *Warning*: the code could diverge from what the spec intends. *Suggestion*: everything else. Suggestions never block a merge.

## Wire shapes

Other projects depend on the exact shapes junto-memory returns, so treat them as fixed:
- Don't rename fields or change casing in tool responses.
- Before changing a tool signature or wire protocol, document the existing one first.
- Spec changes that cross components go through the spec owner.

## License

MIT. By contributing, you agree your contributions are licensed under the same terms.
