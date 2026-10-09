# devclean

Reclaim disk space on a macOS dev machine without losing work.

It cleans git worktrees, Flutter `build/` folders, iOS simulators and old
runtimes, Xcode DerivedData and device support, Docker leftovers, Android
emulators and system images, `node_modules`, and package manager caches.

It is a dry run by default. Nothing is deleted without a review: `--apply`
walks you through each category (all, item by item, or skip), or you write a
plan file, edit it, and apply only what is left in it.

```
$ devclean
== worktrees: 5.4 GB in 4 item(s), 38 kept
== builds: 7.0 GB in 2 item(s), 5 kept
== simulators: 0 B in 0 item(s), 2 kept
== runtimes: 0 B in 0 item(s), 1 kept
== derived-data: 40.9 GB in 54 item(s)
== device-support: 14.3 GB in 3 item(s)
== docker: 185.8 MB in 4 item(s)

Reclaimable: 67.8 GB. Free now: 114.0 GB.
Dry run. Review with --apply (asks per category), or --plan-out FILE.
```

One Python file, standard library only (Python 3.11+).

## Install

```bash
git clone https://github.com/irvine5k/devclean.git
ln -s "$PWD/devclean/devclean.py" ~/.local/bin/devclean
```

## Use

```bash
devclean                      # dry run of the default categories
devclean -v                   # also show what is kept, and why
devclean all                  # dry run of every category
devclean xcode caches         # pick categories or aliases
devclean --json               # the plan as JSON, for scripts
devclean --list               # categories and aliases
```

## Review, then delete

Interactive: `devclean --apply` shows each category and asks
`[a]ll / [r]eview each / [s]kip`. With `r` it asks about every item.

With a plan file, for a careful pass or for a script:

```bash
devclean all --plan-out plan.tsv          # deletes nothing
$EDITOR plan.tsv                          # delete the lines you want to keep
devclean --apply --plan plan.tsv          # asks once, then deletes what is left
devclean --apply --plan plan.tsv --yes    # same, no question (cron, CI, agents)
```

Each item in the plan is checked again at apply time. If a worktree got new
changes since the review, or a process started using a folder, it is skipped.
`--yes` without `--plan` is refused, so nothing is deleted unattended without
a reviewed plan.

## What it cleans

| Category | Default | What |
|---|---|---|
| `worktrees` | yes | git worktrees with no work in them (`git worktree remove`; the branch stays) |
| `builds` | yes | Flutter `build/` folders |
| `simulators` | yes | unavailable simulators, ones whose name matches a pattern, and data-heavy ones (erased, not deleted) |
| `runtimes` | yes | simulator runtimes older than the newest major version per platform |
| `derived-data` | yes | Xcode DerivedData |
| `device-support` | yes | Xcode device support for older OS versions, per device model |
| `docker` | yes | stopped containers, dangling images, build cache, unused networks |
| `docker-volumes` | no | unused anonymous volumes (named volumes stay) |
| `android-avds` | no | emulators that are not running |
| `android-images` | no | system images no remaining emulator uses |
| `node-modules` | no | `node_modules` in projects untouched for 30 days |
| `cache-npm`, `cache-gradle`, `cache-pods`, `cache-pub`, `cache-brew` | no | package manager caches |

Aliases: `xcode`, `android`, `caches`, `all`.

## What it never touches

A worktree stays when any of these is true:

- it has uncommitted changes;
- it has untracked files that are not identical copies of files in the main checkout;
- it is a detached HEAD with commits that no branch, remote or tag contains;
- it is locked;
- a running process has its working directory inside it;
- its git state changed in the last 24 hours (an idle Claude Code or Conductor
  session runs no process, but it still uses the folder).

A Flutter `build/` folder stays when its project is in use, its worktree is
locked, or it was written in the last 2 hours.

"In use" means a running process has its working directory inside the folder,
or names the folder on its command line (a test runner started elsewhere).

Also kept: booted simulators, runtimes a
booted simulator uses, DerivedData while Xcode or `xcodebuild` runs, running
emulators, and the Gradle cache while a Gradle daemon runs.

Every deletion is logged to `~/.local/state/devclean/devclean.log`.

## Config

`~/.config/devclean/config.toml`, every key optional:

```toml
roots = ["~/projects"]            # where to look for repos and projects
sim_delete_pattern = 'QA-\d+'     # delete (not erase) simulators named like this
sim_erase_min_mb = 500            # erase shut-down simulators holding this much data
runtime_keep_majors = 1           # newest major versions to keep per platform
device_support_keep = 1           # newest OS versions to keep per device model
node_modules_days = 30
docker_all_images = false         # true: also remove unused, not only dangling, images
worktree_min_idle_hours = 24
build_min_idle_hours = 2
max_depth = 4
```

## Test

```bash
python3 -m unittest -v test_devclean
```

The tests build a real git repo with one worktree per case above (clean,
dirty, locked, unique untracked file, duplicate untracked file, detached on a
branch commit, detached orphan commit, in use, missing) and check that only
the right ones go. They also cover the review: a line removed from the plan
stays, an item that changed after the review stays, `--yes` without a plan is
refused, and the interactive skip and item-by-item paths.

## License

MIT
