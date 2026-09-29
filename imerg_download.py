"""Download raw GPM IMERG files from NASA GES DISC with earthaccess.

IMERG is published as one collection per latency and temporal frequency, e.g.
``GPM_3IMERGDF`` for Final daily, and GES DISC lays each one out as
``GPM_L3/<short name>.<version>/<year>/<month or day of year>/<file>``: daily
files sit under a month directory, half hourly files under a day of year one,
and monthly files directly under the year. The files are found through a CMR
search rather than by walking that tree, which also yields each file's size.
The local tree is rebuilt as
``<download>/<version>/raw/<latency>/<frequency>/<subdirectory>/<file>`` with
the version written as ``v_07`` rather than ``V07``, and the subdirectory a year
(``1998``) for daily and monthly files and a year and month (``1998/01``) for
half hourly ones, so that no directory holds more than a couple of thousand.

Which files are fetched is driven by the config: the ``version``, the list of
``products`` (``<latency>/<frequency>``, e.g. ``final/daily``) and
``year_range`` (``null`` for the whole record). earthaccess handles the login
and the search; the files are then streamed over its authenticated HTTPS session
by ``n_processes`` worker processes, each with its own session and its own log
file.

Downloads are restartable: a file whose local copy already matches the size CMR
reports is skipped, transfers land on a ``.part`` file that is only renamed into
place once complete, and stray ``.part`` files are swept before and after a run
and on Ctrl-C. The closing verification pass compares every expected file
against the CMR size, so a rerun picks up exactly the files it lists.
"""

import logging
import argparse
import os
import re
import time
import socket
import multiprocessing
from datetime import datetime
from multiprocessing import Pool

from utils.path_utils import (
    load_config,
)
from utils.log_utils import (
    setup_logging,
)

import earthaccess

# CMR short name of each IMERG collection, by (latency, frequency); the version
# is searched separately. There is no early or late monthly product.
SHORT_NAMES = {
    ('early', 'half_hourly'): 'GPM_3IMERGHHE',
    ('late', 'half_hourly'): 'GPM_3IMERGHHL',
    ('final', 'half_hourly'): 'GPM_3IMERGHH',
    ('early', 'daily'): 'GPM_3IMERGDE',
    ('late', 'daily'): 'GPM_3IMERGDL',
    ('final', 'daily'): 'GPM_3IMERGDF',
    ('final', 'monthly'): 'GPM_3IMERGM',
}
# local subdirectory under each product, as a strftime format on the file's
# start date; a year of half hourly files is 17,520 of them, so those are split
# by month too, which caps a directory at 1,488
SUBDIR_FORMAT = {
    'half_hourly': '%Y/%m',
    'daily': '%Y',
    'monthly': '%Y',
}
# what the filename says about the product, used to catch a search that
# returned something other than what was asked for
FILENAME_KIND = {'half_hourly': 'HHR', 'daily': 'DAY', 'monthly': 'MO'}
FILENAME_LATENCY = {'early': 'E', 'late': 'L', 'final': None}
# '3B-HHR-E.MS.MRG.3IMERG.19980101-S000000-E002959.0000.V07B.HDF5' (early half
# hourly, index is the minute of the day), '3B-DAY.MS.MRG.3IMERG.20250930-
# S000000-E235959.V07B.nc4' (final daily, no index), '3B-MO.MS.MRG.3IMERG.
# 19980101-S000000-E235959.01.V07B.HDF5' (monthly, index is the month). The
# release letter after the version varies across the record, so it is matched
# rather than assumed.
FILENAME_RE = re.compile(
    r'^3B-(?P<kind>HHR|DAY|MO)'
    r'(?:-(?P<latency>[EL]))?'          # E early, L late, absent for final
    r'\.MS\.MRG\.3IMERG\.'
    r'(?P<date>\d{8})-S(?P<start>\d{6})-E(?P<end>\d{6})'
    r'(?:\.(?P<index>\d{2,4}))?'        # half hourly and monthly only
    r'\.(?P<release>V\d{2}[A-Z]?)'
    r'\.(?P<extension>nc4|HDF5)$'
)
# 'V07' -> '07', which is both the CMR collection version and, as 'v_07', the
# local directory name
VERSION_RE = re.compile(r'^[Vv](?P<number>\d+)$')
# processName keeps the workers apart in the shared console stream
LOG_FORMAT = '%(asctime)s [%(levelname)s] %(processName)s %(name)s: %(message)s'

LOG = logging.getLogger(__name__)

# per worker HTTPS session and settings, filled in by init_worker; sessions
# cannot be shared between processes, so each worker keeps its own here
_WORKER_STATE = {}


def parse_version(version):
    """Return the CMR collection version for a configured IMERG version.

    Args:
        version (str): Version as written in the config, e.g. 'V07'.

    Returns:
        str: The number as CMR writes it, e.g. '07'.

    Raises:
        ValueError: If the version is not a 'V' followed by a number.
    """
    match = VERSION_RE.match(version)
    if match is None:
        raise ValueError(f"cannot parse version {version!r}, expected e.g. 'V07'")
    return match.group('number')


def format_version(version):
    """Rewrite an IMERG version for use as a directory name.

    Args:
        version (str): Version as written in the config, e.g. 'V07'.

    Returns:
        str: e.g. 'v_07'.
    """
    return f'v_{parse_version(version)}'


def parse_product(product):
    """Split a configured product into its latency, frequency and collection.

    Args:
        product (str): Product as written in the config, e.g. 'final/daily'.

    Returns:
        tuple: (latency, frequency, short_name), e.g.
            ('final', 'daily', 'GPM_3IMERGDF').

    Raises:
        ValueError: If the product is not '<latency>/<frequency>' or names a
            combination IMERG does not publish, such as early monthly.
    """
    parts = product.lower().split('/')
    if len(parts) != 2 or tuple(parts) not in SHORT_NAMES:
        known = ', '.join(sorted(f'{a}/{b}' for a, b in SHORT_NAMES))
        raise ValueError(f'unknown product {product!r}, expected one of: {known}')
    latency, frequency = parts
    return latency, frequency, SHORT_NAMES[(latency, frequency)]


def format_product(product):
    """Rewrite a configured product for use as a local directory path.

    Args:
        product (str): Product as written in the config, e.g. 'final/daily'.

    Returns:
        str: The lowercased path components, e.g. 'final/daily'.
    """
    return os.path.join(*(part.lower() for part in product.split('/')))


def download_root(settings):
    """Root of the local tree for the configured version.

    Args:
        settings (dict): The loaded configuration.

    Returns:
        str: e.g. '<download>/v_07/raw'.
    """
    return os.path.join(
        settings['directories']['download'], format_version(settings['version']), 'raw'
    )


def pid_file(settings):
    """Path of the file recording which host and pid are running the download.

    Args:
        settings (dict): The loaded configuration.

    Returns:
        str: e.g. '<logs>/imerg_download.pid'.
    """
    stem, _ = os.path.splitext(settings['log_file'])
    return os.path.join(settings['directories']['logs'], f'{stem}.pid')


def write_pid_file(settings):
    """Record the host and pid of this run.

    A download started on one login node is invisible from the others -- GLADE
    is shared but process tables are not -- so the host has to be written down
    alongside the pid or a later session cannot find the run to check or stop it.

    Args:
        settings (dict): The loaded configuration.

    Returns:
        str: The path written.
    """
    path = pid_file(settings)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(f'{socket.gethostname()} {os.getpid()}\n')
    return path


def login():
    """Log in to NASA Earthdata with the credentials in ~/.netrc.

    Returns:
        earthaccess.Auth: The authenticated session state.

    Raises:
        RuntimeError: If the login did not succeed.
    """
    auth = earthaccess.login(strategy='netrc')
    if not auth or not auth.authenticated:
        raise RuntimeError('earthdata login failed, check ~/.netrc')
    return auth


def in_year_range(year, year_range):
    """Whether a year falls inside the configured range.

    Args:
        year (int): The year encoded in a filename.
        year_range (list | None): Inclusive [first, last], or None for no limit.

    Returns:
        bool: True if the file should be downloaded.
    """
    if year_range is None:
        return True
    first, last = year_range
    return first <= year <= last


def granule_size_bytes(granule):
    """Exact size of a granule in bytes, from its CMR metadata.

    GES DISC reports the size in MB meaning MiB, to enough digits that
    rounding recovers the byte count exactly; it matches the HTTP
    Content-Length. Anything that cannot be turned into bytes is an error
    rather than a guess, since every skip and verification relies on it.

    Args:
        granule (earthaccess.DataGranule): A search result.

    Returns:
        int: The size in bytes.

    Raises:
        ValueError: If the metadata carries no usable size.
    """
    scale = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3}
    info = granule['umm'].get('DataGranule', {}).get('ArchiveAndDistributionInformation', [])
    total = 0
    for entry in info:
        if 'SizeInBytes' in entry:
            total += int(entry['SizeInBytes'])
        elif 'Size' in entry and entry.get('SizeUnit') in scale:
            total += round(float(entry['Size']) * scale[entry['SizeUnit']])
        else:
            raise ValueError(f'no usable size in {entry}')
    if not total:
        raise ValueError('granule metadata carries no size')
    return total


def search_granules(settings, short_name):
    """Search CMR for every granule of one collection in the configured years.

    Args:
        settings (dict): The loaded configuration.
        short_name (str): Collection short name, e.g. 'GPM_3IMERGDF'.

    Returns:
        list: The earthaccess.DataGranule results.
    """
    query = {'short_name': short_name, 'version': parse_version(settings['version'])}
    if settings['year_range'] is not None:
        first, last = settings['year_range']
        query['temporal'] = (f'{first}-01-01T00:00:00', f'{last}-12-31T23:59:59')
    LOG.info(f'searching CMR for {query}')
    return earthaccess.search_data(**query)


def newest_release(files, product):
    """Collapse granules that cover the same period, keeping the newest release.

    A period is normally published once, but a reprocessing can leave an older
    release (V07A) listed next to its replacement (V07B). Both would land on
    disk under different names and read as two copies of the same data, so only
    the higher release letter is kept and the other is logged.

    Args:
        files (list): (url, name, match, size) tuples for one product, where
            match is the FILENAME_RE match on name.
        product (str): The product being listed, for the log message.

    Returns:
        list: The same tuples with superseded releases removed.
    """
    newest = {}
    for item in files:
        match = item[2]
        period = (match.group('date'), match.group('start'), match.group('index'))
        previous = newest.get(period)
        if previous is None:
            newest[period] = item
            continue
        older, newer = sorted(
            (previous, item), key=lambda entry: entry[2].group('release')
        )
        newest[period] = newer
        LOG.warning(
            f'{product}: {older[1]} is superseded by {newer[1]}, keeping the latter'
        )
    return list(newest.values())


def list_files(settings, product):
    """List the files to download for one product.

    Args:
        settings (dict): The loaded configuration.
        product (str): A product such as 'final/daily'.

    Returns:
        list: (url, local_path, size) triples, filtered by year and with
            superseded releases dropped.
    """
    latency, frequency, short_name = parse_product(product)

    files = []
    for granule in search_granules(settings, short_name):
        links = granule.data_links()
        if not links:
            LOG.warning(f'skipping {granule["umm"].get("GranuleUR")}: no download link')
            continue
        url = links[0]
        name = url.rsplit('/', 1)[-1]
        match = FILENAME_RE.match(name)
        if match is None:
            LOG.warning(f'skipping {url}: cannot parse filename')
            continue
        # a search should only return its own collection, but a mismatch here
        # would file data under the wrong product without anyone noticing
        if (
            match.group('kind') != FILENAME_KIND[frequency]
            or match.group('latency') != FILENAME_LATENCY[latency]
        ):
            LOG.warning(f'skipping {url}: filename does not match {product}')
            continue
        if not in_year_range(int(match.group('date')[:4]), settings['year_range']):
            continue
        # size is kept so downloads can be skipped and verified later
        files.append((url, name, match, granule_size_bytes(granule)))

    root = os.path.join(download_root(settings), format_product(product))
    listing = []
    for url, name, match, size in newest_release(files, product):
        start = datetime.strptime(match.group('date'), '%Y%m%d')
        local_path = os.path.join(root, start.strftime(SUBDIR_FORMAT[frequency]), name)
        listing.append((url, local_path, size))
    return sorted(listing, key=lambda item: item[1])


def format_size(n_bytes):
    """Human readable file size.

    Args:
        n_bytes (int): Size in bytes.

    Returns:
        str: The size in MB, or GB once it passes 1024 MB.
    """
    mb = n_bytes / 1024**2
    return f'{mb:.1f} MB' if mb < 1024 else f'{mb / 1024:.2f} GB'


def worker_log_file(settings, worker=None):
    """Path of the per-process log file, for the current worker unless one is named.

    Args:
        settings (dict): The loaded configuration.
        worker (str, optional): Worker name to build a path for. Defaults to the
            name of the calling process.

    Returns:
        str: e.g. '<logs>/imerg_download_forkserverpoolworker-1.log'.
    """
    stem, extension = os.path.splitext(settings['log_file'])
    worker = worker or multiprocessing.current_process().name.lower()
    return os.path.join(settings['directories']['logs'], f'{stem}_{worker}{extension}')


def init_worker(settings):
    """Give each worker process its own log file and Earthdata session.

    Runs once per worker at pool startup, so each worker logs in once and
    reuses its session, and the token it carries, for every file it handles.

    Args:
        settings (dict): The loaded configuration.
    """
    setup_logging(worker_log_file(settings), fmt=LOG_FORMAT)
    login()
    _WORKER_STATE['session'] = earthaccess.get_requests_https_session()
    _WORKER_STATE['retries'] = settings['retries']
    LOG.info('worker ready, logged in to earthdata')


def fetch(url, tmp_path):
    """Stream one URL to a local file over the worker's session.

    Args:
        url (str): The granule's HTTPS link.
        tmp_path (str): Where to write it.

    Returns:
        int: The number of bytes written.

    Raises:
        requests.HTTPError: If the server answered with an error status.
    """
    written = 0
    # connect timeout, then the longest wait allowed between two chunks
    with _WORKER_STATE['session'].get(url, stream=True, timeout=(30, 300)) as response:
        response.raise_for_status()
        with open(tmp_path, 'wb') as handle:
            for chunk in response.iter_content(chunk_size=1024**2):
                handle.write(chunk)
                written += len(chunk)
    return written


def download_file(job):
    """Download a single file, skipping it if a complete local copy already exists.

    Args:
        job (tuple): (index, total, url, local_path, size); index and total are
            only used to tag the log lines.

    Returns:
        bool: True if the file is in place afterwards, False if every attempt
            failed.
    """
    index, total, url, local_path, size = job
    tag = f'[{index}/{total}]'
    name = os.path.basename(local_path)

    # name and size both match CMR, so this file is already done
    if os.path.exists(local_path) and os.path.getsize(local_path) == size:
        LOG.info(f'{tag} skipping {name}: already downloaded')
        return True

    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    # transfer to a temporary name so an interrupted run never leaves a file
    # that looks complete to the skip check above
    tmp_path = local_path + '.part'
    attempts = _WORKER_STATE['retries']
    for attempt in range(1, attempts + 1):
        LOG.info(
            f'{tag} starting {url} ({format_size(size)}) -> {local_path}'
            + (f' (attempt {attempt}/{attempts})' if attempt > 1 else '')
        )
        start = time.monotonic()
        try:
            written = fetch(url, tmp_path)
            # a dropped connection can end the stream early without an error
            if written != size:
                raise IOError(f'got {written} bytes, CMR lists {size}')
            os.replace(tmp_path, local_path)
        except Exception as error:
            # keep going with the other files; failures are reported at the end
            LOG.error(f'{tag} failed to download {name}: {error}')
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            if attempt < attempts:
                time.sleep(2**attempt)
            continue
        elapsed = time.monotonic() - start
        rate = size / 1024**2 / elapsed if elapsed else 0
        LOG.info(
            f'{tag} finished {name} ({format_size(size)}) '
            f'in {elapsed:.1f} s ({rate:.1f} MB/s)'
        )
        return True
    return False


def cleanup_partial_files(directories):
    """Remove leftover *.part files from a run that was killed mid-transfer.

    Only the directories this run writes to are looked at, one listing each,
    rather than a walk of the whole download tree. Note this removes every
    *.part in them, so two runs must not share a download directory.

    Args:
        directories (iterable): Local directories the run downloads into.

    Returns:
        int: Number of files removed.
    """
    removed = 0
    for directory in sorted(directories):
        if not os.path.isdir(directory):
            continue
        for filename in os.listdir(directory):
            if not filename.endswith('.part'):
                continue
            path = os.path.join(directory, filename)
            try:
                os.remove(path)
            except OSError as error:
                LOG.error(f'could not remove partial download {path}: {error}')
                continue
            LOG.warning(f'removed partial download {path}')
            removed += 1
    LOG.info(f'cleaned up {removed} partial downloads')
    return removed


def verify_downloads(jobs):
    """Check every expected file is on disk with the remote size, and log the results.

    Args:
        jobs (list): The (index, total, url, local_path, size) tuples that were
            handed to the pool.

    Returns:
        bool: True if every file is present and complete.
    """
    LOG.info(f'verifying {len(jobs)} downloaded files')
    missing = []
    incomplete = []
    verified_bytes = 0
    for _, _, url, local_path, size in jobs:
        if not os.path.exists(local_path):
            missing.append((url, local_path))
            continue
        local_size = os.path.getsize(local_path)
        if local_size != size:
            incomplete.append((url, local_path, local_size, size))
            continue
        verified_bytes += size

    # list the problem files first, then the summary line
    for url, local_path in missing:
        LOG.error(f'missing: {local_path} (remote {url})')
    for url, local_path, local_size, size in incomplete:
        LOG.error(
            f'size mismatch: {local_path} is {format_size(local_size)}, '
            f'remote {url} is {format_size(size)}'
        )

    n_ok = len(jobs) - len(missing) - len(incomplete)
    LOG.info(
        f'verified {n_ok}/{len(jobs)} files ({format_size(verified_bytes)}), '
        f'{len(missing)} missing, {len(incomplete)} incomplete'
    )
    return not missing and not incomplete


def main(settings, dry_run=False):

    os.makedirs(download_root(settings), exist_ok=True)
    os.makedirs(settings['directories']['logs'], exist_ok=True)
    log_file = os.path.join(settings['directories']['logs'], settings['log_file'])
    setup_logging(log_file, fmt=LOG_FORMAT)

    years = settings['year_range'] or 'all'
    LOG.info(
        f'downloading IMERG {settings["version"]} data '
        f'({settings["products"]}, years: {years}) from GES DISC'
    )
    LOG.info(f'running as pid {os.getpid()} on {socket.gethostname()}')
    login()

    # build the full file list up front, one search per product, so the total
    # volume is known before any transfer starts
    per_product = {}
    for product in settings['products']:
        product_files = list_files(settings, product)
        releases = sorted(
            {FILENAME_RE.match(os.path.basename(path)).group('release')
             for _, path, _ in product_files}
        )
        LOG.info(f'found {len(product_files)} {product} files (releases: {releases})')
        per_product[product] = product_files

    jobs = [job for product_files in per_product.values() for job in product_files]
    total_bytes = sum(size for _, _, size in jobs)
    LOG.info(f'found {len(jobs)} files in total ({format_size(total_bytes)})')

    if dry_run:
        for product, product_files in per_product.items():
            product_bytes = sum(size for _, _, size in product_files)
            LOG.info(
                f'{product}: {len(product_files)} files '
                f'({format_size(product_bytes)})'
            )
            for url, local_path, size in product_files[:3]:
                LOG.info(f'  {url} ({format_size(size)}) -> {local_path}')
            if len(product_files) > 3:
                LOG.info(f'  ... and {len(product_files) - 3} more')
        LOG.info('dry run, nothing downloaded')
        return

    if not jobs:
        LOG.warning('no files matched the configuration, nothing to download')
        return

    # number the jobs so each log line says which file of how many it is
    jobs = [(index, len(jobs), *job) for index, job in enumerate(jobs, start=1)]
    directories = {os.path.dirname(local_path) for _, _, _, local_path, _ in jobs}

    # clear debris from any previous run before the workers start writing
    cleanup_partial_files(directories)
    LOG.info(f'wrote {write_pid_file(settings)}')

    n_processes = min(settings['n_processes'], len(jobs)) or 1
    LOG.info(
        f'starting {n_processes} worker processes, '
        f'each logging to {worker_log_file(settings, worker="<worker>")}'
    )
    try:
        # chunksize=1 hands out one file at a time, so a worker that draws a
        # slow file does not hold up a whole pre-assigned block
        with Pool(processes=n_processes, initializer=init_worker, initargs=(settings,)) as pool:
            results = pool.map(download_file, jobs, chunksize=1)
    except BaseException as error:
        # covers ctrl-c too; the pool has terminated and joined its workers by
        # the time this runs, so nothing is still writing to a .part file
        LOG.error(f'download interrupted ({type(error).__name__}), cleaning up before exiting')
        cleanup_partial_files(directories)
        raise
    finally:
        # a stale pid file would point at a process that is gone, or worse at a
        # pid the system has since reused
        if os.path.exists(pid_file(settings)):
            os.remove(pid_file(settings))
    cleanup_partial_files(directories)

    for (index, _, url, _, _), ok in zip(jobs, results):
        if not ok:
            LOG.error(f'[{index}/{len(jobs)}] did not download: {url}')
    LOG.info(f'downloaded {results.count(True)} files, {results.count(False)} failed')

    # final check against the CMR sizes, in the main log; a rerun picks up
    # exactly the files reported here
    if not verify_downloads(jobs):
        LOG.error('download incomplete, rerun to retry the files listed above')
        raise SystemExit(1)

    LOG.info('all files downloaded and verified')
    LOG.info('done :-)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='download raw gpm imerg files from ges disc via earthaccess.'
    )
    parser.add_argument(
        '--config',
        type=str,
        required=True,
        help='Path to YAML configuration file.',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='List what would be downloaded and exit.',
    )
    args = parser.parse_args()
    settings = load_config(args.config)
    main(settings, dry_run=args.dry_run)
