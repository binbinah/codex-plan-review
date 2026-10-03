# codex-plan-review

- This repository implements a native Codex Plan review plugin. Keep unrelated Codex configuration unchanged.
- Use Python 3.11+ standard-library code for the hook runtime. Hooks must work without a package installation.
- Preserve provider and model configuration while isolating reviewer tools and disabling recursive hooks.
- A technical review never grants permissions. Preserve the host's normal Plan-to-execution transition.
- Keep ordinary sessions and read-only investigation usable. Test failure, timeout, concurrent review, and resume behavior.
- Run `python3 -m unittest discover -s tests -v` and the skill validator before committing.
- Live tests must use isolated temporary workspaces and report actual hook and worker events separately from mocks.
- Never commit credentials, local provider configuration, session transcripts, or runtime state.
- User-facing documentation and commit messages use Simplified Chinese. Code comments and identifiers use English.
