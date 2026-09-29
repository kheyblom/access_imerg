# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Download raw GPM IMERG precipitation files from NASA GES DISC onto GLADE with the
`earthaccess` package, for the shared data lake. This is one of a family of sibling
downloaders in `../` (`access_smap`, `access_mswep`, `access_gleam`) and must stay
consistent with them in layout, config shape, logging and style (see *Sibling conventions*).

Task list and status live in [TASKS.md](TASKS.md). Mark tasks complete there as they finish.
Keep the *Current working state* section below up to date at the end of every working
step, so a new session can resume without re-deriving anything.

## Current working state

- Task 1 (this CLAUDE.md) and task 2 (git repo, `origin` connected) are done.
- Task 3 (initial build) is complete. The approved plan is in
  `~/.claude/plans/see-tasks-md-i-atomic-adleman.md`. These exist: `imerg_download.py`,
  `config/config_download.yaml` (V07 final daily, full record) and
  `config/config_download_tiny.yaml` (2025 only, into `data/imerg_tiny/`), `utils/`,
  `run_download.sh`, `job_download.pbs` and `README.md`.
- Verified so far: dry runs only. Full: 10,135 files, 291.45 GB. Tiny: 273 files,
  7.92 GB. All are release V07B, with no skipped names.
- Task 4 is in progress. The first tiny run (2026-09-29) got 403 `EULA Acceptance Failure`
  on every GET, because the GES DISC application is not approved on the Earthdata
  account. HEAD requests succeed via CloudFront, so a HEAD check does not detect this.
  The user must approve it at
  https://urs.earthdata.nasa.gov/approve_app?client_id=e2WVk8Pw6weeLUKZYOxvTQ. Nothing was
  written to disk. Fixes made: a one-byte `check_access` before the pool (it also runs on
  dry runs), no retries on 401/403, and SIGINT/SIGTERM handlers (`kill -INT` was ignored,
  because background jobs from a non-interactive shell inherit SIGINT as ignored). The
  handlers were tested detached. Once access is approved, rerun the tiny test.
- Task 4 checklist: the tiny download with
  `./run_download.sh config/config_download_tiny.yaml`, checking verification and
  `done :-)`, a rerun to confirm all files are skipped, and a `kill -INT` resume test.
  After that comes task 5 (the full V07 final daily download). Each needs the user's
  go-ahead.
- Nothing has been downloaded yet.

Design choices already settled with the user:
- Transfer is our own per-file `Pool` loop (gleam pattern) over
  `earthaccess.get_requests_https_session()`, not `earthaccess.download()`.
- Time selection is `year_range` only.
- Products are configured as `<latency>/<frequency>` (for example `final/daily`) and
  mapped to short names in `SHORT_NAMES`. Local subdirectories are the year, or
  year/month for half-hourly (`SUBDIR_FORMAT`).
- Exact byte sizes come from CMR: `Size` is in MiB, and `round(Size * 1024**2)` equals
  the Content-Length.

## Environment and commands

- Python 3.14 environment managed by `uv`, already built in `.venv/`. Run everything as
  `uv run python <script> --config config/<file>.yaml` (or `.venv/bin/python`).
  `uv sync` rebuilds it from `uv.lock`.
- `pyyaml` (needed by `utils/path_utils.py`) is now an explicit dependency.
- There is no build, lint or test tooling in this repo or its siblings. Verify with a
  `--dry-run` (enumerate and size the files, download nothing) and a "tiny" config
  (for example one month into a separate scratch directory) before any full run.
- Earthdata auth is `earthaccess.login(strategy='netrc')`, which uses `~/.netrc`. It
  exists already. Never read or print it.
- earthaccess 0.19 API notes: `DataCollection.summary` is an attribute, not a method.
  `DataGranule.size()` is still a method but emits a FutureWarning about becoming an
  attribute in 1.0.

## IMERG products (V07, all cloud hosted at GES DISC)

| latency \ frequency | half-hourly | daily | monthly |
|---|---|---|---|
| early | `GPM_3IMERGHHE` C2723758340-GES_DISC | `GPM_3IMERGDE` C2723754850-GES_DISC | n/a |
| late  | `GPM_3IMERGHHL` C2723754845-GES_DISC | `GPM_3IMERGDL` C2723754859-GES_DISC | n/a |
| final | `GPM_3IMERGHH`  C2723754847-GES_DISC | `GPM_3IMERGDF` C2723754864-GES_DISC | `GPM_3IMERGM` C2723754851-GES_DISC |

(Checked with `earthaccess.search_datasets(short_name=..., cloud_hosted=True)` on
2026-09-28. Only V07 is returned.) Monthly exists only as Final.

`GPM_3IMERGDF`: 10,135 granules through 2025-09-30, about 31 MB each, so roughly 300 GB.
Granule URLs look like
`https://data.gesdisc.earthdata.nasa.gov/data/GPM_L3/GPM_3IMERGDF.07/2025/09/3B-DAY.MS.MRG.3IMERG.20250930-S000000-E235959.V07B.nc4`.
The collection version is `07`, but filenames carry a sub-release (`V07B`). Don't
assume the letter is the same across the record or across products.

`earthaccess_search.ipynb` is the scratch notebook for searching collections and
granules.

## Sibling conventions to follow

Read `../access_mswep` (the most complete sibling, with a README and run and PBS
scripts) and `../access_gleam` for patterns. `../access_smap/earthaccess_download.py` is
the only other earthaccess user, but it is a minimal single-call script.

Repo layout:
- `<dataset>_download.py` (one entry-point script)
- `<dataset>_search.ipynb`
- `config/config_download*.yaml` (one YAML per run variant, including a `_tiny` smoke-test one)
- `utils/log_utils.py` and `utils/path_utils.py`. Copy these from a sibling (identical
  across repos; mswep's `setup_logging` takes a `fmt` arg).
- `logs/`, `run_download.sh`, `job_download.pbs`, `README.md`

Config shape: `directories.download`, `directories.logs` (`./logs`), `log_file`,
`n_processes` and `version`, plus dataset-specific selectors such as `products` and
`year_range` (`null` means the whole record). `run_download.sh` extracts `log_file` and
`logs:` with `sed`, so keep those as top-level and simple scalar lines.

Script shape:
- A module docstring explains the remote layout, the local layout and restart behaviour.
- Google-style docstrings (`Args:`, `Returns:`, `Raises:`).
- Module-level compiled regexes carry a comment showing an example match.
- `LOG = logging.getLogger(__name__)`.
- `main(settings, dry_run=False)` plus `argparse` with `--config` (required) and
  `--dry-run`.
- The final log line is `done :-)`.

Local tree: `<download>/<version>/raw/<product...>/<file>`. The version is formatted for
directories (`V2.8` becomes `v_2_8`, `v4.3a` becomes `v_4_3_a`), and product path parts
are lowercased. Use year subdirectories so that no directory exceeds about 2,000–3,000
files. Download roots are under `/glade/derecho/scratch/kheyblom/data/<dataset>/`.

Robustness:
- Build the full file list and total size up front and log it before any transfer.
- Skip files whose local size already matches.
- Write to a `.part`/`.partial` file and rename it into place only on completion.
- Sweep stray partials before and after a run and on Ctrl-C.
- Finish with a verification pass against the remote sizes that exits non-zero if
  anything is missing. Rerunning must pick up exactly those files.
- Parallel workers each log to `<log stem>_<processname>.log`, using `LOG_FORMAT` with
  `%(processName)s`.

Running (mswep pattern):
- `run_download.sh [config]` starts the run with
  `setsid nohup nice -n 19 uv run python ...`, fully detached from the shell.
- `--status` reads `logs/<stem>.pid` (host and pid). Login nodes share GLADE but not
  process tables.
- The script refuses a second concurrent run over the same directory.
- `job_download.pbs` is the fallback and targets **Casper**. Derecho compute nodes have
  no outbound internet.

## Compute and allocation

The allocation is finite, so minimize core-hours. A download is network bound. The
sibling practice is a detached, `nice -n 19` run on a login node, with the Casper PBS
job as a fallback. That conflicts with the global rule that large downloads belong on
batch nodes, so **ask the user before starting any full download**, whichever route is
used. Always show any `qsub`/`qcmd` command and wait for approval.
`qsub -A "$PBS_ACCOUNT"` needs a login shell (`bash -lc`) for the variable to be set.
Limit parallelism to what GES DISC tolerates, and prefer a few workers.

## Git

- `origin` fetches from `https://github.com/kheyblom/access_imerg.git` but pushes over
  SSH (`git@github.com:kheyblom/access_imerg.git`), because HTTPS has no credential
  helper on this system while the GitHub SSH key works. Branch `main`.
- Commit each logical change as it is made. Use short imperative subjects, as in the
  siblings ("Add MSWEP download pipeline").
- The `.gitignore` (shared with siblings) excludes `*.log`, `*.out`, `nohup*`, `.venv`,
  `draft*` and `shards/`. Track `pyproject.toml`, `uv.lock` and the search notebook,
  like the siblings do.
