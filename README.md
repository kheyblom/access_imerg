# access_imerg

Download raw GPM IMERG precipitation files from NASA GES DISC onto GLADE, with
[earthaccess](https://earthaccess.readthedocs.io). It is structured like `access_mswep`
and `access_gleam`: one script driven by one YAML config, a thin `utils/` layer,
per-worker log files, and a run that is safe to repeat.

```
imerg_download.py                 the downloader
earthaccess_search.ipynb          scratch notebook for searching collections and granules
config/config_download.yaml       V07 Final daily, full record
config/config_download_tiny.yaml  same, limited to 2025, for a smoke test
utils/                            config loading, logging
run_download.sh                   start the download on a login node
job_download.pbs                  fallback batch job (Casper)
```

## One time setup

earthaccess logs in with `strategy='netrc'`, so it needs an Earthdata Login entry in
`~/.netrc` (mode 600):

```
machine urs.earthdata.nasa.gov login <user> password <password>
```

GES DISC data also needs the *NASA GESDISC DATA ARCHIVE* application approved under
your Earthdata profile. Then install the environment with `uv sync`.

## Running

```bash
# what would be downloaded, no transfers
uv run python imerg_download.py --config config/config_download.yaml --dry-run

# one year into a separate directory, as a smoke test
./run_download.sh config/config_download_tiny.yaml

# the full record
./run_download.sh
```

`run_download.sh` starts the download under `setsid`, in a session of its own, so it
survives logout and anything that kills the starting shell's process group (`nohup`
alone only blocks SIGHUP, which is not enough). It runs at `nice -n 19` so it yields to
anything interactive on the shared node.

Once enumeration finishes, the run records its host and pid in
`logs/<log_file stem>.pid`, and it deletes that file when it ends. The host matters:
Derecho's login nodes share GLADE but not their process tables, so a run started on
`derecho1` is invisible from `derecho4`.

```bash
./run_download.sh --status              # host, pid, whether it is alive
tail -n 40 logs/imerg_download.log      # main log: totals, verification, failures
ls logs/                                 # one log per worker, one line per file
```

Stopping is always safe. `kill -INT <pid>` sweeps partial files on the way out, and a
rerun picks up exactly what is missing. A second run over the same download directory
is refused while the first is alive. Logs append across runs, so use the pid file, not
a `grep` for `done :-)`, to tell whether something is in progress.

`job_download.pbs` is a fallback for the case where a login-node run keeps getting
killed. Submit it from the repo with `qsub -A "$PBS_ACCOUNT" job_download.pbs`. It
targets Casper, because Derecho compute nodes have no outbound internet.

## Products

A product in the config is `<latency>/<frequency>`:

| latency \ frequency | `half_hourly` | `daily` | `monthly` |
|---|---|---|---|
| `early` | `GPM_3IMERGHHE` | `GPM_3IMERGDE` | n/a |
| `late`  | `GPM_3IMERGHHL` | `GPM_3IMERGDL` | n/a |
| `final` | `GPM_3IMERGHH`  | `GPM_3IMERGDF` | `GPM_3IMERGM` |

`version: V07` selects collection version `07`. Filenames carry a sub-release on top of
it (`V07B`), and the run logs which releases it found. If a period is listed under two
releases, only the newer one is kept. Every product starts on 1998-01-01. The Final
daily record, to 2025-09-30, is 10,135 files and 291 GB.

Downloading another product means listing it in `products`, or giving it its own
config and `log_file`. Half-hourly is about 17,500 files per year per product.

## How it works

One CMR search per product (`earthaccess.search_data`) returns every granule with its
HTTPS link and size. GES DISC gives the size in MiB to enough digits that rounding
recovers the exact byte count, which matches the HTTP `Content-Length`. So the total
volume is known before anything transfers, and no per-file request is needed.

The local tree is
`<download>/v_07/raw/<latency>/<frequency>/<subdirectory>/<file>`. The subdirectory is
the year for daily and monthly files, and the year and month for half-hourly ones:

```
<download>/v_07/raw/final/daily/1998/3B-DAY.MS.MRG.3IMERG.19980101-S000000-E235959.V07B.nc4
<download>/v_07/raw/final/half_hourly/1998/01/3B-HHR.MS.MRG.3IMERG.19980101-S000000-E002959.0000.V07B.HDF5
```

This keeps every directory under about 1,500 files, below the 2,000–3,000 that GLADE's
parallel filesystems are comfortable with.

`n_processes` workers each log in once and stream their files over earthaccess's
authenticated `requests` session. Each file is written to `<file>.part`, checked
against the expected byte count, and only then renamed into place. A failed file is
retried `retries` times with backoff before it is reported. Files whose local size
already matches are skipped, stray `.part` files in the run's directories are swept
before and after every run and on Ctrl-C, and the run ends with a verification pass
against the CMR sizes that exits non-zero if anything is missing.
