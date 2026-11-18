# AST-00 Baseline Recovery Report

Date: 2026-11-18

## Outcome

Git metadata is usable again and points at the recoverable local baseline commit `f8b80e1de13a89c45bd100cae57d80a7f5ea6538`. The working tree was not restored from that commit: files that are empty in the worktree may have been intentionally emptied, and the available evidence cannot distinguish that from damage. Those files remain unchanged and are reported by Git as modifications. This is a metadata repair, not a source restoration or a claim that the project is runnable.

## Preservation and rollback

A full copy of the repository, including `.git`, was made before the metadata repair:

- Backup: `/home/zuohang/workspace/astero-ast00-backups/ast00-20261118-133721/repository`
- File checksum manifest: `/home/zuohang/workspace/astero-ast00-backups/ast00-20261118-133721/SHA256SUMS`
- Snapshot extracted from `f8b80e1`: `/home/zuohang/workspace/astero-ast00-backups/ast00-20261118-133721/f8b80e1-snapshot`
- Snapshot checksum manifest: `/home/zuohang/workspace/astero-ast00-backups/ast00-20261118-133721/SNAPSHOT-SHA256SUMS`

`sha256sum -c` passed for every file in the repository backup. Before repair, both `.git/index` and `.git/refs/heads/main` were zero bytes (SHA-256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`). Their original copies remain in the backup. The repaired `.git/index` SHA-256 is `bd9d75a6ee5758ca0c180d0cff17ebdb93ca5dc34193addccff8b6d58a3f06ea`; the repaired local branch ref SHA-256 is `f7d14f41537e594f61ae6fefabb698149fdb772373d5a7fcaa41f8cb48e08ab7`.

## Snapshot comparison

The commit contains 15 tracked files. Each had nonzero content in the commit but is zero bytes in the pre-repair worktree: `.gitignore`, `README.md`, `astero/__init__.py`, `astero/__main__.py`, `astero/core.py`, `configs/astero.example.json`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `docs/HARDWARE_DECISIONS_2026-11-18.md`, `docs/PRODUCT.md`, `docs/VALIDATION.md`, `docs/VOICE_BRIDGE.md`, `pyproject.toml`, `systemd/astero.service`, and `tests/test_core.py`. Since intentional deletion/emptying cannot be ruled out, none were restored.

`docs/ECHO_DOT_2_INSTRUCTIONS.md`, `docs/LOCAL_VOICE_PATH.md`, and `docs/AGENT_DEVELOPMENT_TASKS.md` are preserved as nonempty worktree documents absent from this commit. `docs/VOICE_BRIDGE.md` is nonempty in the worktree but differs from the tracked baseline, so it was preserved too. Other files added by concurrent tasks are also left untouched.

The configured `origin` is reachable. `git ls-remote origin refs/heads/main` returned `d799efcdc4a177cb3f0e94322f14c7bab4e8b2c0`, which differs from local baseline `f8b80e1de13a89c45bd100cae57d80a7f5ea6538`. The remote commit is not present in the local object database, so its content-level diff was not evaluated. No fetch was performed. Git currently marks `origin/main` as `[gone]` because the local remote-tracking ref is missing/unusable; the live remote branch was verified separately with `ls-remote`.

## Verification

- `git rev-parse HEAD` resolves to `f8b80e1de13a89c45bd100cae57d80a7f5ea6538`.
- `git status --short --branch` runs and reports the preserved worktree differences.
- `git show f8b80e1:README.md` reads the snapshot source successfully.
- `python3 -m unittest discover -s tests -v` ran 6 tests and passed. These were concurrent AST-02 PCM tests (`tests/test_pcm.py`); the pre-existing `tests/test_core.py` was empty, so this does not validate the historical baseline test suite.

## Remaining limitation

The empty source/config/test files remain unchanged. There is no safe basis to claim intentional deletion or corruption, so a runnable source baseline has not been restored. The backup and Git history preserve the data needed for a later reviewed recovery decision.
