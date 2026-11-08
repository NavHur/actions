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
  owners: 8 and 16), or changes a file it cannot measure. Excesses in
  untouched functions never fail it. It runs fleet's `measure_complexity.py`
  at `fleet-ref` with that commit's pinned analyzers. Check out with
  `fetch-depth: 0` first.

  ```yaml
  - uses: actions/checkout@v4
    with:
      fetch-depth: 0
  - uses: NavHur/actions/complexity@main
  ```

- `ci-cache`: runner-local CI state for self-hosted runners. Inputs `command`
  (`passed-check`, `passed-record`, `godot-restore`, `godot-save`) and `key`.

`file-size/file_size.py` is a copy of the one Pedro's host tooling uses; that
repository's CI fails when the two differ, so change both together.
