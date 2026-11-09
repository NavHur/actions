# actions

Composite GitHub Actions shared by NavHur's repositories.

- `file-size`: fail when a change adds a file over 20,000 bytes (about 5,000
  tokens) or grows one already over. Check out with `fetch-depth: 0` first.

  ```yaml
  - uses: actions/checkout@v4
    with:
      fetch-depth: 0
  - uses: NavHur/actions/file-size@main
  ```

- `complexity`: fail when a change leaves a function it touches over Pedro's
  complexity limits (NavHur repositories: cognitive 6, cyclomatic 18; other
  owners: 8 and 16), or when an analyzer is missing or fails. Excesses in
  untouched functions never fail it, and files that don't parse are listed
  without failing it. Check out with `fetch-depth: 0` first.

  ```yaml
  - uses: actions/checkout@v4
    with:
      fetch-depth: 0
  - uses: NavHur/actions/complexity@main
  ```

- `checks`: `file-size` and `complexity` in one job, so a repository bills
  one rounded-up runner minute per push instead of two. Both checks run even
  when the first fails, each failure gets an annotation titled with its
  check's name, and the job fails when either check did. The complexity
  analyzers have Linux builds for x86_64 only, so keep this job on an x64
  runner. It replaces a repository's separate `file-size` and `complexity`
  jobs; rename any required status checks to the new job when you switch.

  ```yaml
  jobs:
    checks:
      name: File size and complexity
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4
          with:
            fetch-depth: 0
        - uses: NavHur/actions/checks@main
  ```

- `ci-cache`: runner-local CI state for self-hosted runners. Inputs `command`
  (`passed-check`, `passed-record`, `godot-restore`, `godot-save`) and `key`.

`file-size/file_size.py`, `complexity/binaries.py`, `complexity/skill/` and the
pins in `complexity/binaries.json` are copies of NavHur/fleet's, written by its
`lib/actions-copy/actions_copies.py`. Fleet's CI fails when they differ, so
change them there and rewrite the copies here. `pbp-complexity` releases are
mirrored here from fleet's, so public and private CI can download them.
