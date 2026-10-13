# -*- coding: utf-8 -*-
"""Getting files: the small ones (JSON, checksums) at once, the data files one after another with progress.

A repository that is a folder is read in place: nothing is copied unless the user asks for a copy — except where
a GeoPackage cannot be opened where it is (a share without file locking, such as a WSL or some network folders): the
first file says so, and from then on files are copied into the cache like downloads. A repository behind a URL is
downloaded into the cache, each file under the path it has in the repository, written as `.part` and renamed only
when its SHA-256 is the one the repository states. A file already in the cache with the stated checksum is not
fetched again."""
import hashlib
import json
import os
import shutil

from qgis.PyQt.QtCore import QEventLoop, QObject, QTimer, QUrl, QCoreApplication, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.core import QgsBlockingNetworkRequest, QgsFileDownloader, QgsMessageLog, QgsNetworkAccessManager, QgsVectorLayer

from .compat import NET_NO_ERROR, INFO, WARNING, exec_
from .repository import RepositoryError, is_url, join, human_size

LOG_TAG = "Terrae"
CHUNK = 4 * 1024 * 1024


def log(message, level=INFO):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


class QgisHttpReader(object):
    """Reads the small files of a repository behind a URL, through QGIS's network manager (proxy, authentication)."""

    def __init__(self, root):
        self.root = root

    def read(self, rel):
        url = join(self.root, rel)
        request = QgsBlockingNetworkRequest()
        code = request.get(QNetworkRequest(QUrl(url)), True)
        if code != NET_NO_ERROR:
            raise RepositoryError("Cannot read {}: {}".format(url, request.errorMessage() or "network error"))
        return bytes(request.reply().content())


class CachedHttpReader(QgisHttpReader):
    """The same, keeping a copy of each small file: when the repository cannot be reached (no connection), the copy
    is what is read, so the files already downloaded stay usable offline."""

    def __init__(self, root, folder):
        super().__init__(root)
        self.folder = os.path.join(folder, "meta", hashlib.sha1(root.encode("utf-8")).hexdigest()[:16])
        self.offline = False

    def read(self, rel):
        copy = os.path.join(self.folder, *rel.split("/"))
        try:
            raw = super().read(rel)
        except RepositoryError:
            if os.path.isfile(copy):
                self.offline = True
                with open(copy, "rb") as f:
                    return f.read()
            raise
        try:
            os.makedirs(os.path.dirname(copy), exist_ok=True)
            with open(copy, "wb") as f:
                f.write(raw)
        except OSError:
            pass
        return raw


def final_url(url, hops=5, timeout_ms=30000):
    """The address a file is really served from, following redirections by hand (a HEAD request each).

    A host that answers with a redirection to its CDN sends a short text with it; QgsFileDownloader can write that text
    into the file among the data. Given the final address, it meets no redirection. On any trouble the address is
    returned as it was, and the size and checksum checks judge the download."""
    try:
        current = QUrl(url)
        for _hop in range(hops):
            request = QNetworkRequest(current)
            request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute, QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
            reply = QgsNetworkAccessManager.instance().head(request)
            loop = QEventLoop()
            reply.finished.connect(loop.quit)
            QTimer.singleShot(timeout_ms, loop.quit)
            if not reply.isFinished():
                exec_(loop)
            done = reply.isFinished()
            target = reply.attribute(QNetworkRequest.Attribute.RedirectionTargetAttribute) if done else None
            if not done:
                reply.abort()
            reply.deleteLater()
            if target is None or (isinstance(target, QUrl) and target.isEmpty()) or target == "":
                return current
            current = current.resolved(target if isinstance(target, QUrl) else QUrl(str(target)))
        return current
    except Exception as e:
        log("Could not follow redirections of {}: {}".format(url, e), WARNING)
        return QUrl(url)


def sha256_of(path, pulse=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                break
            h.update(block)
            if pulse:
                pulse()
    return h.hexdigest()


class Cache(object):
    """The downloaded files, under the paths they have in the repository, with what was verified."""

    def __init__(self, root):
        self.root = root
        self._index = None

    def path(self, repo, asset):
        return os.path.join(self.root, repo.version, *(asset.tree + asset.rel).split("/"))

    def _index_path(self):
        return os.path.join(self.root, "verified.json")

    def index(self):
        if self._index is None:
            try:
                with open(self._index_path(), "r", encoding="utf-8") as f:
                    self._index = json.load(f)
            except (OSError, ValueError):
                self._index = {}
        return self._index

    def mark(self, path, sha256):
        self.index()[os.path.relpath(path, self.root).replace(os.sep, "/")] = {"sha256": sha256, "bytes": os.path.getsize(path)}
        os.makedirs(self.root, exist_ok=True)
        tmp = self._index_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._index, f)
        os.replace(tmp, self._index_path())

    def has(self, repo, asset, expected_sha=None):
        """True if the file is here, whole, and (when the repository states a checksum) the one that was verified."""
        path = self.path(repo, asset)
        if not os.path.isfile(path):
            return False
        entry = self.index().get(os.path.relpath(path, self.root).replace(os.sep, "/"))
        if entry is None or entry.get("bytes") != os.path.getsize(path):
            return False
        return expected_sha is None or entry.get("sha256") == expected_sha

    def size(self):
        total = 0
        for folder, _dirs, files in os.walk(self.root):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(folder, name))
                except OSError:
                    pass
        return total

    def clear(self):
        if os.path.isdir(self.root):
            shutil.rmtree(self.root, ignore_errors=True)
        self._index = None


class Fetcher(QObject):
    """Brings a list of files to this computer, one after another.

    progress(text, done bytes, total bytes) · finished({asset: local path}) · failed(message) · canceled()"""
    progress = pyqtSignal(str, float, float)
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)
    canceled = pyqtSignal()

    def __init__(self, repo, cache, verify=True, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.cache = cache
        self.verify = verify
        self._queue = []
        self._results = {}
        self._downloader = None
        self._outcome = None
        self._current = None
        self._done_bytes = 0.0
        self._total_bytes = 0.0
        self._stop = False
        self.busy = False
        self.remote = is_url(repo.location)
        self.in_place = None           # a folder repository: can its GeoPackages be opened where they are? (None: not tried yet)

    @property
    def through_cache(self):
        """True when files reach QGIS through the cache: a URL, or a folder that cannot be read in place."""
        return self.remote or self.in_place is False

    # ---- what a request costs ------------------------------------------------------------------------------------
    def local_path(self, asset):
        """Where the file is, or will be, on this computer."""
        if not self.through_cache:
            return self.repo.address(asset)
        return self.cache.path(self.repo, asset)

    def is_here(self, asset):
        if not self.through_cache:
            return os.path.isfile(self.repo.address(asset))
        return self.cache.has(self.repo, asset)

    def estimate(self, assets):
        """(files to download, their bytes or None if a size is unknown, files already here)."""
        todo, total, here, unknown = 0, 0, 0, False
        seen = set()
        for a in assets:
            if (a.tree, a.rel) in seen:
                continue
            seen.add((a.tree, a.rel))
            if self.is_here(a):
                here += 1
            else:
                todo += 1
                if a.bytes is None:
                    unknown = True
                else:
                    total += a.bytes
        return todo, (None if unknown else total), here

    # ---- fetching ------------------------------------------------------------------------------------------------
    def fetch(self, assets):
        if self.busy:
            self.failed.emit("A download is already running.")
            return
        self.busy = True
        self._stop = False
        self._results = {}
        self._queue = []
        seen = set()
        for a in assets:
            if (a.tree, a.rel) not in seen:
                seen.add((a.tree, a.rel))
                self._queue.append(a)
        self._total_bytes = float(sum((a.bytes or 0) for a in self._queue if not self.is_here(a)))
        self._done_bytes = 0.0
        self._next()

    def cancel(self):
        self._stop = True
        if self._downloader is not None:
            try:
                self._downloader.cancelDownload()       # it then exits, and _after_download ends the job
            except RuntimeError:                         # the downloader has already gone
                self._downloader = None
                self._finish_canceled()

    def _finish_canceled(self):
        self._cleanup_part()
        self.busy = False
        self._downloader = None
        self.canceled.emit()

    def _fail(self, message):
        self._cleanup_part()
        self.busy = False
        self._downloader = None
        log(message, WARNING)
        self.failed.emit(message)

    def _cleanup_part(self):
        if self._current is not None:
            part = self.local_path(self._current) + ".part"
            if os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass

    def _next(self):
        if self._stop:
            self._finish_canceled()
            return
        if not self._queue:
            self.busy = False
            self._current = None
            self.finished.emit(self._results)
            return
        asset = self._queue.pop(0)
        self._current = asset
        target = self.local_path(asset)
        name = os.path.basename(asset.rel)
        if not self.remote:
            source = self.repo.address(asset)
            if not os.path.isfile(source):
                self._fail("The repository has no file {} (expected at {}).".format(asset.tree + asset.rel, source))
                return
            if self.in_place is None and asset.format == "gpkg":
                self.in_place = QgsVectorLayer("{}|layername={}".format(source, asset.table), "probe", "ogr").isValid()
                if not self.in_place:
                    log("GeoPackages cannot be opened in place in {} (the folder does not support file locking): "
                        "files are copied into the cache first.".format(self.repo.location), WARNING)
                    target = self.local_path(asset)
            if not self.through_cache:
                self._results[asset] = source
                self._next()
                return
        expected = None
        if self.verify:
            try:
                expected = self.repo.checksum(asset)
            except RepositoryError as e:
                self._fail(str(e))
                return
        if self.cache.has(self.repo, asset, expected):
            self._results[asset] = target
            self._next()
            return
        os.makedirs(os.path.dirname(target), exist_ok=True)
        part = target + ".part"
        if os.path.exists(part):
            os.remove(part)
        self._expected = expected
        self._received = 0.0
        if not self.remote:
            self._copy_in(asset, part)
            return
        self.progress.emit("Downloading {}".format(name), self._done_bytes, self._total_bytes)
        # The downloader deletes itself when it has finished, and must not lose its Python reference while it is
        # still emitting: its signals only record what happened, and the work is done once it has exited.
        self._outcome = None
        dl = QgsFileDownloader(final_url(self.repo.address(asset)), part, "", True)
        dl.downloadProgress.connect(self._on_progress)
        dl.downloadCompleted.connect(lambda *_a: self._record("completed"))
        dl.downloadError.connect(lambda messages: self._record("error", messages))
        dl.downloadCanceled.connect(lambda: self._record("canceled"))
        dl.downloadExited.connect(lambda: QTimer.singleShot(0, self._after_download))
        self._downloader = dl
        dl.startDownload()

    def _copy_in(self, asset, part):
        """Brings a file of a folder repository into the cache (the folder cannot be read in place)."""
        name = os.path.basename(asset.rel)
        total = float(asset.bytes or os.path.getsize(self.repo.address(asset)))
        try:
            with open(self.repo.address(asset), "rb") as src, open(part, "wb") as dst:
                while True:
                    block = src.read(CHUNK)
                    if not block:
                        break
                    dst.write(block)
                    self._received += len(block)
                    self.progress.emit("Copying {} ({} of {})".format(name, human_size(self._received), human_size(total)),
                                       self._done_bytes + self._received, max(self._total_bytes, self._done_bytes + self._received))
                    QCoreApplication.processEvents()
                    if self._stop:
                        break
        except OSError as e:
            self._fail("Could not copy {}: {}".format(name, e.strerror or e))
            return
        if self._stop:
            self._finish_canceled()
            return
        self._on_completed()

    def _record(self, what, detail=None):
        if self._outcome is None or what == "error":
            self._outcome = (what, detail)

    def _after_download(self):
        outcome, self._outcome = self._outcome, None
        self._downloader = None
        if self._stop or (outcome is not None and outcome[0] == "canceled"):
            self._finish_canceled()
        elif outcome is None:
            self._fail("The download of {} ended without saying why. Try again.".format(os.path.basename(self._current.rel) if self._current else "the file"))
        elif outcome[0] == "error":
            self._on_error(outcome[1])
        else:
            self._on_completed()

    def _on_progress(self, received, total):
        self._received = float(received)
        name = os.path.basename(self._current.rel) if self._current else ""
        self.progress.emit("Downloading {} ({} of {})".format(name, human_size(received), human_size(total if total > 0 else self._current.bytes)),
                           self._done_bytes + self._received, max(self._total_bytes, self._done_bytes + self._received))

    def _on_error(self, messages):
        asset = self._current
        text = "; ".join(str(m) for m in messages) if isinstance(messages, (list, tuple)) else str(messages)
        self._fail("Could not download {}: {}".format(self.repo.address(asset) if asset else "the file", text))

    def _on_completed(self, *_args):
        asset = self._current
        target = self.local_path(asset)
        part = target + ".part"
        if not os.path.isfile(part) or os.path.getsize(part) == 0:
            self._fail("The download of {} left no file.".format(os.path.basename(asset.rel)))
            return
        if asset.bytes is not None and os.path.getsize(part) != asset.bytes:
            self._fail("{} is {} long and the repository says {}: the download is incomplete or the repository has changed. Try again."
                       .format(os.path.basename(asset.rel), human_size(os.path.getsize(part)), human_size(asset.bytes)))
            return
        digest = None
        if self.verify:
            self.progress.emit("Checking {}".format(os.path.basename(asset.rel)), self._done_bytes + self._received, self._total_bytes)
            digest = sha256_of(part, QCoreApplication.processEvents)
            if self._stop:
                self._finish_canceled()
                return
            if self._expected is None:
                log("The repository states no checksum for {}: not verified.".format(asset.tree + asset.rel), WARNING)
            elif digest != self._expected:
                self._fail("{} does not have the checksum the repository states: the file is damaged or was changed in transit. "
                           "It was not kept; try again.".format(os.path.basename(asset.rel)))
                return
        os.replace(part, target)
        self.cache.mark(target, digest if (digest and self._expected) else None)
        self._done_bytes += float(asset.bytes or os.path.getsize(target))
        self._results[asset] = target
        self._next()


def copy_to(paths_by_asset, folder, repo, pulse=None):
    """Copies fetched files into a folder of the user's, under names that say what they are. Returns the new paths."""
    os.makedirs(folder, exist_ok=True)
    out = {}
    for asset, path in paths_by_asset.items():
        ext = os.path.splitext(path)[1]
        name = "terrae-{}-{}-{}-{}{}{}".format(asset.dataset.replace("_", "-"), repo.version, asset.level, asset.geometry,
                                               "-" + asset.key if asset.key else "", ext)
        dest = os.path.join(folder, name)
        if os.path.abspath(dest) != os.path.abspath(path):
            with open(path, "rb") as src, open(dest + ".part", "wb") as dst:
                while True:
                    block = src.read(CHUNK)
                    if not block:
                        break
                    dst.write(block)
                    if pulse:
                        pulse()
            os.replace(dest + ".part", dest)
        out[asset] = dest
    return out