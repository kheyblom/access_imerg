We need to build a pipeline or downloading all available IMERG data. We will work through each task one-at-a-time together.

# 1. init CLAUDE.md for the codebase [COMPLETED]

# 2. init a git repo [COMPLETED]
Initalize a github repo and connect to the remote (see general notes).

# 3. Plan and build the codebase [COMPLETED]
I want you to build the codebase for this pipeline. Design the codebase to be consistent with each of the other access codebases (see general notes).
Download the data using the earthaccess python package.
The environment is already created.
See the search notebook for an example of searching the data.
Make sure that the version, product (in this case: [early, late, final]), and time frequency are configurable - I will eventually want to download all of them.
At first, set up the scripts for downloading version 07 final daily files. We will configure for other products in the future.

Initial build (done):
- `imerg_download.py`: earthaccess handles login and the CMR search, and a pool of workers streams each file to `.part` and renames it once its size is checked. Also skips files already downloaded, sweeps partials, retries, and ends with a verification pass. Supports `--dry-run`.
- configs: `config/config_download.yaml` (V07 final daily, full record) and `config/config_download_tiny.yaml` (2025 only, separate scratch dir). A product is `<latency>/<frequency>`, e.g. `final/daily`.
- `utils/` (copied from siblings), `run_download.sh` (detached login-node run, `--status`), `job_download.pbs` (Casper fallback), `README.md`.
- dry runs pass: full = 10,135 files / 291 GB, tiny = 273 files / 7.9 GB, all release V07B.

# 4. Test the pipeline [COMPLETED]
Run the tiny config end-to-end before any full download (~7.9 GB, login node, no core-hours):
- `./run_download.sh config/config_download_tiny.yaml`; check `--status`, the per-worker logs, a clean verification pass and `done :-)`.
- rerun it and confirm every file is skipped.
- interrupt a run with `kill -INT`, confirm `.part` files are swept, and confirm a rerun resumes and verifies.
- fix anything found, and commit.

Results (2026-09-29):
- first run: every file got 403 `EULA Acceptance Failure`, because the GES DISC app was not approved on the Earthdata account (now approved). Fixed: a one-byte access check before any transfer, no retries on 401/403.
- `kill -INT` was ignored (a background job from a non-interactive shell inherits SIGINT as ignored). Fixed: SIGINT/SIGTERM handlers. The retest was clean: the partial was swept, the pid file removed, and no processes were left.
- resume: 26 skipped, 247 downloaded, verified 273/273 (7.92 GB), `done :-)`. About 58 MB/s with 2 workers.
- rerun: all 273 skipped, verified.
- files are valid (read with h5py and netCDF4). Found an environment problem: `xarray.open_dataset` segfaults in this venv (h5py and netCDF4 HDF5 conflict). Not a download issue; needs fixing before any processing work.

# 5. Full download of V07 final daily [COMPLETED]
Download the full V07 final daily record (10,135 files, ~291 GB) into `/glade/derecho/scratch/kheyblom/data/imerg/`.
- needs my approval before starting.
- default route: `./run_download.sh` (detached, `nice -n 19` on a login node). Fallback: `qsub -A "$PBS_ACCOUNT" job_download.pbs` on Casper.
- monitor with `./run_download.sh --status`; rerun if verification reports missing files.
- done when verification reports all files present and complete.

Results (2026-09-29):
- before the run: fixed the Python environment. `xarray.open_dataset` segfaulted because numpy crashes on Python 3.14.3 (every numpy release tried). The venv is now Python 3.13, the same as the siblings, and xarray reads the files.
- run: login node derecho6, 4 workers, 09:53–10:38 (~45 min, ~130 MB/s). 3 transient 502/503 errors, all recovered on retry.
- verified 10,135/10,135 files (291.45 GB), `done :-)`. On disk: every year 1998–2024 complete (365/366 files), 2025 to 09-30 (273), no `.part` files.
- follow-up: workers now reset their inherited signal handlers (on 3.13 the pool forks). Tested with interrupted scratch runs under SIGINT and SIGTERM.


# General notes:
- the git remote is: https://github.com/kheyblom/access_imerg.git
- ensure to always use proper git version controlling as changes are made
- use and update a CLAUDE.md that will take this information and effectively and efficiently handle this project
- use information learned in each of the directories in: @/glade/u/home/kheyblom/work/data_access and add to this project's CLAUDE.md. Each directory contains a codebase for accessing data to be downloaded into our data lake. @/glade/u/home/kheyblom/work/data_access/access_smap will likely be the most useful as is also uses the earthacess package.
- it is always important to minimize compute costs on derecho. we have a finite allocation and we need to managing our usage.
- keep notes on your current working state. you may lose connection to the HPC system or I may need to start new sessions, so I need you to be able to easily pick up where you left off.
- additional tasks may come up that need to occur between the above tasks. this task list can be flexible, but if substantial changes are need, I need to approve them.
- mark tasks as completed when they are completed