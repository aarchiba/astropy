# Licensed under a 3-clause BSD style license - see LICENSE.rst

""" This module contains helper functions for accessing, downloading, and caching data files.
"""

import atexit
import contextlib
import dbm
import fnmatch
import hashlib
import os
import io
import json
import pathlib
import shutil
import socket
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import shelve
import zipfile

from tempfile import NamedTemporaryFile, gettempdir, TemporaryDirectory
from warnings import warn

from astropy import config as _config
from astropy.utils.exceptions import AstropyWarning
from astropy.utils.introspection import find_current_module, resolve_name

# Order here determines order in the autosummary
__all__ = [
    'Conf', 'conf',
    'download_file', 'download_files_in_parallel',
    'get_readable_fileobj',
    'get_pkg_data_fileobj', 'get_pkg_data_filename',
    'get_pkg_data_contents', 'get_pkg_data_fileobjs',
    'get_pkg_data_filenames',
    'is_url_in_cache', 'get_cached_urls',
    'cache_total_size', 'cache_contents',
    'export_download_cache', 'import_download_cache',
    'check_download_cache',
    'clear_download_cache',
    'compute_hash',
    'get_free_space_in_dir',
    'check_free_space_in_dir',
    'get_file_contents',
    'CacheMissingWarning',
]

_dataurls_to_alias = {}


class Conf(_config.ConfigNamespace):
    """
    Configuration parameters for `astropy.utils.data`.
    """

    dataurl = _config.ConfigItem(
        'http://data.astropy.org/',
        'Primary URL for astropy remote data site.')
    dataurl_mirror = _config.ConfigItem(
        'http://www.astropy.org/astropy-data/',
        'Mirror URL for astropy remote data site.')
    remote_timeout = _config.ConfigItem(
        10.,
        'Time to wait for remote data queries (in seconds).',
        aliases=['astropy.coordinates.name_resolve.name_resolve_timeout'])
    compute_hash_block_size = _config.ConfigItem(
        2 ** 16,  # 64K
        'Block size for computing MD5 file hashes.')
    download_block_size = _config.ConfigItem(
        2 ** 16,  # 64K
        'Number of bytes of remote data to download per step.')
    download_cache_lock_attempts = _config.ConfigItem(
        5,
        'Number of times to try to get the lock ' +
        'while accessing the data cache before giving up.')
    delete_temporary_downloads_at_exit = _config.ConfigItem(
        True,
        'If True, temporary download files created when the cache is '
        'inaccessible will be deleted at the end of the python session.')


conf = Conf()


class CacheMissingWarning(AstropyWarning):
    """
    This warning indicates the standard cache directory is not accessible, with
    the first argument providing the warning message. If args[1] is present, it
    is a filename indicating the path to a temporary file that was created to
    store a remote data download in the absence of the cache.
    """


def _is_url(string):
    """
    Test whether a string is a valid URL

    Parameters
    ----------
    string : str
        The string to test
    """
    url = urllib.parse.urlparse(string)
    # we can't just check that url.scheme is not an empty string, because
    # file paths in windows would return a non-empty scheme (e.g. e:\\
    # returns 'e').
    return url.scheme.lower() in [
        'http', 'https', 'ftp', 'sftp', 'ssh', 'file']


def _is_inside(path, parent_path):
    # We have to try realpath too to avoid issues with symlinks, but we leave
    # abspath because some systems like debian have the absolute path (with no
    # symlinks followed) match, but the real directories in different
    # locations, so need to try both cases.
    return os.path.abspath(path).startswith(os.path.abspath(parent_path)) \
        or os.path.realpath(path).startswith(os.path.realpath(parent_path))


@contextlib.contextmanager
def get_readable_fileobj(name_or_obj, encoding=None, cache=False,
                         show_progress=True, remote_timeout=None,
                         sources=None, update_cache=False):
    """Yield a readable, seekable file-like object from a file or URL.

    This supports passing filenames, URLs, and readable file-like objects,
    any of which can be compressed in gzip, bzip2 or lzma (xz) if the
    appropriate compression libraries are provided by the Python installation.

    Notes
    -----

    This function is a context manager, and should be used for example
    as::

        with get_readable_fileobj('file.dat') as f:
            contents = f.read()

    Parameters
    ----------
    name_or_obj : str or file-like object
        The filename of the file to access (if given as a string), or
        the file-like object to access.

        If a file-like object, it must be opened in binary mode.

    encoding : str, optional
        When `None` (default), returns a file-like object with a
        ``read`` method that returns `str` (``unicode``) objects, using
        `locale.getpreferredencoding` as an encoding.  This matches
        the default behavior of the built-in `open` when no ``mode``
        argument is provided.

        When ``'binary'``, returns a file-like object where its ``read``
        method returns `bytes` objects.

        When another string, it is the name of an encoding, and the
        file-like object's ``read`` method will return `str` (``unicode``)
        objects, decoded from binary using the given encoding.

    cache : bool, optional
        Whether to cache the contents of remote URLs.

    show_progress : bool, optional
        Whether to display a progress bar if the file is downloaded
        from a remote server.  Default is `True`.

    remote_timeout : float
        Timeout for remote requests in seconds (default is the configurable
        `astropy.utils.data.Conf.remote_timeout`, which is 3s by default)

    sources : list of str, optional
        If provided, a list of URLs to try to obtain the file from. The
        result will be stored under the original URL. The original URL
        will *not* be tried unless it is in this list; this is to prevent
        long waits for a primary server that is known to be inaccessible
        at the moment.

    update_cache : bool, optional
        If true, attempt to download the file anew; if this is successful
        replace the current version. If not, raise a URLError but leave
        the existing cached value intact.

    Returns
    -------
    file : readable file-like object

    """

    # close_fds is a list of file handles created by this function
    # that need to be closed.  We don't want to always just close the
    # returned file handle, because it may simply be the file handle
    # passed in.  In that case it is not the responsibility of this
    # function to close it: doing so could result in a "double close"
    # and an "invalid file descriptor" exception.
    PATH_TYPES = (str, pathlib.Path)

    close_fds = []
    delete_fds = []

    if remote_timeout is None:
        # use configfile default
        remote_timeout = conf.remote_timeout

    # Get a file object to the content
    if isinstance(name_or_obj, PATH_TYPES):
        # name_or_obj could be a Path object if pathlib is available
        name_or_obj = str(name_or_obj)

        is_url = _is_url(name_or_obj)
        if is_url:
            name_or_obj = download_file(
                name_or_obj, cache=cache, show_progress=show_progress,
                timeout=remote_timeout, sources=sources,
                update_cache=update_cache)
        fileobj = io.FileIO(name_or_obj, 'r')
        if is_url and not cache:
            delete_fds.append(fileobj)
        close_fds.append(fileobj)
    else:
        fileobj = name_or_obj

    # Check if the file object supports random access, and if not,
    # then wrap it in a BytesIO buffer.  It would be nicer to use a
    # BufferedReader to avoid reading loading the whole file first,
    # but that is not compatible with streams or urllib2.urlopen
    # objects on Python 2.x.
    if not hasattr(fileobj, 'seek'):
        fileobj = io.BytesIO(fileobj.read())

    # Now read enough bytes to look at signature
    signature = fileobj.read(4)
    fileobj.seek(0)

    if signature[:3] == b'\x1f\x8b\x08':  # gzip
        import struct
        try:
            import gzip
            fileobj_new = gzip.GzipFile(fileobj=fileobj, mode='rb')
            fileobj_new.read(1)  # need to check that the file is really gzip
        except (OSError, EOFError, struct.error):  # invalid gzip file
            fileobj.seek(0)
            fileobj_new.close()
        else:
            fileobj_new.seek(0)
            fileobj = fileobj_new
    elif signature[:3] == b'BZh':  # bzip2
        try:
            import bz2
        except ImportError:
            for fd in close_fds:
                fd.close()
            raise ValueError(
                ".bz2 format files are not supported since the Python "
                "interpreter does not include the bz2 module")
        try:
            # bz2.BZ2File does not support file objects, only filenames, so we
            # need to write the data to a temporary file
            with NamedTemporaryFile("wb", delete=False) as tmp:
                tmp.write(fileobj.read())
                tmp.close()
                fileobj_new = bz2.BZ2File(tmp.name, mode='rb')
            fileobj_new.read(1)  # need to check that the file is really bzip2
        except OSError:  # invalid bzip2 file
            fileobj.seek(0)
            fileobj_new.close()
            # raise
        else:
            fileobj_new.seek(0)
            close_fds.append(fileobj_new)
            fileobj = fileobj_new
    elif signature[:3] == b'\xfd7z':  # xz
        try:
            import lzma
            fileobj_new = lzma.LZMAFile(fileobj, mode='rb')
            fileobj_new.read(1)  # need to check that the file is really xz
        except ImportError:
            for fd in close_fds:
                fd.close()
            raise ValueError(
                ".xz format files are not supported since the Python "
                "interpreter does not include the lzma module.")
        except (OSError, EOFError):  # invalid xz file
            fileobj.seek(0)
            fileobj_new.close()
            # should we propagate this to the caller to signal bad content?
            # raise ValueError(e)
        else:
            fileobj_new.seek(0)
            fileobj = fileobj_new

    # By this point, we have a file, io.FileIO, gzip.GzipFile, bz2.BZ2File
    # or lzma.LZMAFile instance opened in binary mode (that is, read
    # returns bytes).  Now we need to, if requested, wrap it in a
    # io.TextIOWrapper so read will return unicode based on the
    # encoding parameter.

    needs_textio_wrapper = encoding != 'binary'

    if needs_textio_wrapper:
        # A bz2.BZ2File can not be wrapped by a TextIOWrapper,
        # so we decompress it to a temporary file and then
        # return a handle to that.
        try:
            import bz2
        except ImportError:
            pass
        else:
            if isinstance(fileobj, bz2.BZ2File):
                tmp = NamedTemporaryFile("wb", delete=False)
                data = fileobj.read()
                tmp.write(data)
                tmp.close()
                delete_fds.append(tmp)

                fileobj = io.FileIO(tmp.name, 'r')
                close_fds.append(fileobj)

        fileobj = io.BufferedReader(fileobj)
        fileobj = io.TextIOWrapper(fileobj, encoding=encoding)

        # Ensure that file is at the start - io.FileIO will for
        # example not always be at the start:
        # >>> import io
        # >>> f = open('test.fits', 'rb')
        # >>> f.read(4)
        # 'SIMP'
        # >>> f.seek(0)
        # >>> fileobj = io.FileIO(f.fileno())
        # >>> fileobj.tell()
        # 4096L

        fileobj.seek(0)

    try:
        yield fileobj
    finally:
        for fd in close_fds:
            fd.close()
        for fd in delete_fds:
            os.remove(fd.name)


def get_file_contents(*args, **kwargs):
    """
    Retrieves the contents of a filename or file-like object.

    See  the `get_readable_fileobj` docstring for details on parameters.

    Returns
    -------
    content
        The content of the file (as requested by ``encoding``).

    """
    with get_readable_fileobj(*args, **kwargs) as f:
        return f.read()


@contextlib.contextmanager
def get_pkg_data_fileobj(data_name, package=None, encoding=None, cache=True):
    """
    Retrieves a data file from the standard locations for the package and
    provides the file as a file-like object that reads bytes.

    Parameters
    ----------
    data_name : str
        Name/location of the desired data file.  One of the following:

            * The name of a data file included in the source
              distribution.  The path is relative to the module
              calling this function.  For example, if calling from
              ``astropy.pkname``, use ``'data/file.dat'`` to get the
              file in ``astropy/pkgname/data/file.dat``.  Double-dots
              can be used to go up a level.  In the same example, use
              ``'../data/file.dat'`` to get ``astropy/data/file.dat``.
            * If a matching local file does not exist, the Astropy
              data server will be queried for the file.
            * A hash like that produced by `compute_hash` can be
              requested, prefixed by 'hash/'
              e.g. 'hash/34c33b3eb0d56eb9462003af249eff28'.  The hash
              will first be searched for locally, and if not found,
              the Astropy data server will be queried.

    package : str, optional
        If specified, look for a file relative to the given package, rather
        than the default of looking relative to the calling module's package.

    encoding : str, optional
        When `None` (default), returns a file-like object with a
        ``read`` method returns `str` (``unicode``) objects, using
        `locale.getpreferredencoding` as an encoding.  This matches
        the default behavior of the built-in `open` when no ``mode``
        argument is provided.

        When ``'binary'``, returns a file-like object where its ``read``
        method returns `bytes` objects.

        When another string, it is the name of an encoding, and the
        file-like object's ``read`` method will return `str` (``unicode``)
        objects, decoded from binary using the given encoding.

    cache : bool
        If True, the file will be downloaded and saved locally or the
        already-cached local copy will be accessed. If False, the
        file-like object will directly access the resource (e.g. if a
        remote URL is accessed, an object like that from
        `urllib.request.urlopen` is returned).

    Returns
    -------
    fileobj : file-like
        An object with the contents of the data file available via
        ``read`` function.  Can be used as part of a ``with`` statement,
        automatically closing itself after the ``with`` block.

    Raises
    ------
    urllib2.URLError, urllib.error.URLError
        If a remote file cannot be found.
    OSError
        If problems occur writing or reading a local file.

    Examples
    --------

    This will retrieve a data file and its contents for the `astropy.wcs`
    tests::

        >>> from astropy.utils.data import get_pkg_data_fileobj
        >>> with get_pkg_data_fileobj('data/3d_cd.hdr',
        ...                           package='astropy.wcs.tests') as fobj:
        ...     fcontents = fobj.read()
        ...

    This next example would download a data file from the astropy data server
    because the ``allsky/allsky_rosat.fits`` file is not present in the
    source distribution.  It will also save the file locally so the
    next time it is accessed it won't need to be downloaded.::

        >>> from astropy.utils.data import get_pkg_data_fileobj
        >>> with get_pkg_data_fileobj('allsky/allsky_rosat.fits',
        ...                           encoding='binary') as fobj:  # doctest: +REMOTE_DATA +IGNORE_OUTPUT
        ...     fcontents = fobj.read()
        ...
        Downloading http://data.astropy.org/allsky/allsky_rosat.fits [Done]

    This does the same thing but does *not* cache it locally::

        >>> with get_pkg_data_fileobj('allsky/allsky_rosat.fits',
        ...                           encoding='binary', cache=False) as fobj:  # doctest: +REMOTE_DATA +IGNORE_OUTPUT
        ...     fcontents = fobj.read()
        ...
        Downloading http://data.astropy.org/allsky/allsky_rosat.fits [Done]

    See Also
    --------
    get_pkg_data_contents : returns the contents of a file or url as a bytes object
    get_pkg_data_filename : returns a local name for a file containing the data
    """

    datafn = _find_pkg_data_path(data_name, package=package)
    if os.path.isdir(datafn):
        raise OSError("Tried to access a data file that's actually "
                      "a package data directory")
    elif os.path.isfile(datafn):  # local file
        with get_readable_fileobj(datafn, encoding=encoding) as fileobj:
            yield fileobj
    else:  # remote file
        with get_readable_fileobj(
            conf.dataurl + data_name,
            encoding=encoding,
            cache=cache,
            sources=[conf.dataurl + data_name,
                     conf.dataurl_mirror + data_name],
        ) as fileobj:
            # We read a byte to trigger any URLErrors
            fileobj.read(1)
            fileobj.seek(0)
            yield fileobj


def get_pkg_data_filename(data_name, package=None, show_progress=True,
                          remote_timeout=None):
    """
    Retrieves a data file from the standard locations for the package and
    provides a local filename for the data.

    This function is similar to `get_pkg_data_fileobj` but returns the
    file *name* instead of a readable file-like object.  This means
    that this function must always cache remote files locally, unlike
    `get_pkg_data_fileobj`.

    Parameters
    ----------
    data_name : str
        Name/location of the desired data file.  One of the following:

            * The name of a data file included in the source
              distribution.  The path is relative to the module
              calling this function.  For example, if calling from
              ``astropy.pkname``, use ``'data/file.dat'`` to get the
              file in ``astropy/pkgname/data/file.dat``.  Double-dots
              can be used to go up a level.  In the same example, use
              ``'../data/file.dat'`` to get ``astropy/data/file.dat``.
            * If a matching local file does not exist, the Astropy
              data server will be queried for the file.
            * A hash like that produced by `compute_hash` can be
              requested, prefixed by 'hash/'
              e.g. 'hash/34c33b3eb0d56eb9462003af249eff28'.  The hash
              will first be searched for locally, and if not found,
              the Astropy data server will be queried.

    package : str, optional
        If specified, look for a file relative to the given package, rather
        than the default of looking relative to the calling module's package.

    show_progress : bool, optional
        Whether to display a progress bar if the file is downloaded
        from a remote server.  Default is `True`.

    remote_timeout : float
        Timeout for the requests in seconds (default is the
        configurable `astropy.utils.data.Conf.remote_timeout`, which
        is 3s by default)

    Raises
    ------
    urllib2.URLError, urllib.error.URLError
        If a remote file cannot be found.
    OSError
        If problems occur writing or reading a local file.

    Returns
    -------
    filename : str
        A file path on the local file system corresponding to the data
        requested in ``data_name``.

    Examples
    --------

    This will retrieve the contents of the data file for the `astropy.wcs`
    tests::

        >>> from astropy.utils.data import get_pkg_data_filename
        >>> fn = get_pkg_data_filename('data/3d_cd.hdr',
        ...                            package='astropy.wcs.tests')
        >>> with open(fn) as f:
        ...     fcontents = f.read()
        ...

    This retrieves a data file by hash either locally or from the astropy data
    server::

        >>> from astropy.utils.data import get_pkg_data_filename
        >>> fn = get_pkg_data_filename('hash/34c33b3eb0d56eb9462003af249eff28')  # doctest: +SKIP
        >>> with open(fn) as f:
        ...     fcontents = f.read()
        ...

    See Also
    --------
    get_pkg_data_contents : returns the contents of a file or url as a bytes object
    get_pkg_data_fileobj : returns a file-like object with the data
    """

    if remote_timeout is None:
        # use configfile default
        remote_timeout = conf.remote_timeout

    if data_name.startswith('hash/'):
        # first try looking for a local version if a hash is specified
        hashfn = _find_hash_fn(data_name[5:])

        if hashfn is None:
            return download_file(conf.dataurl + data_name, cache=True,
                                 show_progress=show_progress,
                                 timeout=remote_timeout,
                                 sources=[conf.dataurl + data_name,
                                          conf.dataurl_mirror + data_name])
        else:
            return hashfn
    else:
        fs_path = os.path.normpath(data_name)
        datafn = _find_pkg_data_path(fs_path, package=package)
        if os.path.isdir(datafn):
            raise OSError("Tried to access a data file that's actually "
                          "a package data directory")
        elif os.path.isfile(datafn):  # local file
            return datafn
        else:  # remote file
            return download_file(conf.dataurl + data_name, cache=True,
                                 show_progress=show_progress,
                                 timeout=remote_timeout,
                                 sources=[conf.dataurl + data_name,
                                          conf.dataurl_mirror + data_name])


def get_pkg_data_contents(data_name, package=None, encoding=None, cache=True):
    """
    Retrieves a data file from the standard locations and returns its
    contents as a bytes object.

    Parameters
    ----------
    data_name : str
        Name/location of the desired data file.  One of the following:

            * The name of a data file included in the source
              distribution.  The path is relative to the module
              calling this function.  For example, if calling from
              ``astropy.pkname``, use ``'data/file.dat'`` to get the
              file in ``astropy/pkgname/data/file.dat``.  Double-dots
              can be used to go up a level.  In the same example, use
              ``'../data/file.dat'`` to get ``astropy/data/file.dat``.
            * If a matching local file does not exist, the Astropy
              data server will be queried for the file.
            * A hash like that produced by `compute_hash` can be
              requested, prefixed by 'hash/'
              e.g. 'hash/34c33b3eb0d56eb9462003af249eff28'.  The hash
              will first be searched for locally, and if not found,
              the Astropy data server will be queried.
            * A URL to some other file.

    package : str, optional
        If specified, look for a file relative to the given package, rather
        than the default of looking relative to the calling module's package.


    encoding : str, optional
        When `None` (default), returns a file-like object with a
        ``read`` method that returns `str` (``unicode``) objects, using
        `locale.getpreferredencoding` as an encoding.  This matches
        the default behavior of the built-in `open` when no ``mode``
        argument is provided.

        When ``'binary'``, returns a file-like object where its ``read``
        method returns `bytes` objects.

        When another string, it is the name of an encoding, and the
        file-like object's ``read`` method will return `str` (``unicode``)
        objects, decoded from binary using the given encoding.

    cache : bool
        If True, the file will be downloaded and saved locally or the
        already-cached local copy will be accessed. If False, the
        file-like object will directly access the resource (e.g. if a
        remote URL is accessed, an object like that from
        `urllib.request.urlopen` is returned).

    Returns
    -------
    contents : bytes
        The complete contents of the file as a bytes object.

    Raises
    ------
    urllib2.URLError, urllib.error.URLError
        If a remote file cannot be found.
    OSError
        If problems occur writing or reading a local file.

    See Also
    --------
    get_pkg_data_fileobj : returns a file-like object with the data
    get_pkg_data_filename : returns a local name for a file containing the data
    """

    with get_pkg_data_fileobj(data_name, package=package, encoding=encoding,
                              cache=cache) as fd:
        contents = fd.read()
    return contents


def get_pkg_data_filenames(datadir, package=None, pattern='*'):
    """
    Returns the path of all of the data files in a given directory
    that match a given glob pattern.

    Parameters
    ----------
    datadir : str
        Name/location of the desired data files.  One of the following:

            * The name of a directory included in the source
              distribution.  The path is relative to the module
              calling this function.  For example, if calling from
              ``astropy.pkname``, use ``'data'`` to get the
              files in ``astropy/pkgname/data``.
            * Remote URLs are not currently supported.

    package : str, optional
        If specified, look for a file relative to the given package, rather
        than the default of looking relative to the calling module's package.

    pattern : str, optional
        A UNIX-style filename glob pattern to match files.  See the
        `glob` module in the standard library for more information.
        By default, matches all files.

    Returns
    -------
    filenames : iterator of str
        Paths on the local filesystem in *datadir* matching *pattern*.

    Examples
    --------
    This will retrieve the contents of the data file for the `astropy.wcs`
    tests::

        >>> from astropy.utils.data import get_pkg_data_filenames
        >>> for fn in get_pkg_data_filenames('data/maps', 'astropy.wcs.tests',
        ...                                  '*.hdr'):
        ...     with open(fn) as f:
        ...         fcontents = f.read()
        ...
    """

    path = _find_pkg_data_path(datadir, package=package)
    if os.path.isfile(path):
        raise OSError(
            "Tried to access a data directory that's actually "
            "a package data file")
    elif os.path.isdir(path):
        for filename in os.listdir(path):
            if fnmatch.fnmatch(filename, pattern):
                yield os.path.join(path, filename)
    else:
        raise OSError("Path not found")


def get_pkg_data_fileobjs(datadir, package=None, pattern='*', encoding=None):
    """
    Returns readable file objects for all of the data files in a given
    directory that match a given glob pattern.

    Parameters
    ----------
    datadir : str
        Name/location of the desired data files.  One of the following:

            * The name of a directory included in the source
              distribution.  The path is relative to the module
              calling this function.  For example, if calling from
              ``astropy.pkname``, use ``'data'`` to get the
              files in ``astropy/pkgname/data``
            * Remote URLs are not currently supported

    package : str, optional
        If specified, look for a file relative to the given package, rather
        than the default of looking relative to the calling module's package.

    pattern : str, optional
        A UNIX-style filename glob pattern to match files.  See the
        `glob` module in the standard library for more information.
        By default, matches all files.

    encoding : str, optional
        When `None` (default), returns a file-like object with a
        ``read`` method that returns `str` (``unicode``) objects, using
        `locale.getpreferredencoding` as an encoding.  This matches
        the default behavior of the built-in `open` when no ``mode``
        argument is provided.

        When ``'binary'``, returns a file-like object where its ``read``
        method returns `bytes` objects.

        When another string, it is the name of an encoding, and the
        file-like object's ``read`` method will return `str` (``unicode``)
        objects, decoded from binary using the given encoding.

    Returns
    -------
    fileobjs : iterator of file objects
        File objects for each of the files on the local filesystem in
        *datadir* matching *pattern*.

    Examples
    --------
    This will retrieve the contents of the data file for the `astropy.wcs`
    tests::

        >>> from astropy.utils.data import get_pkg_data_filenames
        >>> for fd in get_pkg_data_fileobjs('data/maps', 'astropy.wcs.tests',
        ...                                 '*.hdr'):
        ...     fcontents = fd.read()
        ...
    """

    for fn in get_pkg_data_filenames(datadir, package=package,
                                     pattern=pattern):
        with get_readable_fileobj(fn, encoding=encoding) as fd:
            yield fd


def compute_hash(localfn):
    """ Computes the MD5 hash for a file.

    The hash for a data file is used for looking up data files in a unique
    fashion. This is of particular use for tests; a test may require a
    particular version of a particular file, in which case it can be accessed
    via hash to get the appropriate version.

    Typically, if you wish to write a test that requires a particular data
    file, you will want to submit that file to the astropy data servers, and
    use
    e.g. ``get_pkg_data_filename('hash/34c33b3eb0d56eb9462003af249eff28')``,
    but with the hash for your file in place of the hash in the example.

    Parameters
    ----------
    localfn : str
        The path to the file for which the hash should be generated.

    Returns
    -------
    md5hash : str
        The hex digest of the MD5 hash for the contents of the ``localfn``
        file.

    """

    with open(localfn, 'rb') as f:
        h = hashlib.md5()
        block = f.read(conf.compute_hash_block_size)
        while block:
            h.update(block)
            block = f.read(conf.compute_hash_block_size)

    return h.hexdigest()


def _find_pkg_data_path(data_name, package=None):
    """
    Look for data in the source-included data directories and return the
    path.
    """

    if package is None:
        module = find_current_module(1, finddiff=['astropy.utils.data', 'contextlib'])
        if module is None:
            # not called from inside an astropy package.  So just pass name
            # through
            return data_name

        if not hasattr(module, '__package__') or not module.__package__:
            # The __package__ attribute may be missing or set to None; see
            # PEP-366, also astropy issue #1256
            if '.' in module.__name__:
                package = module.__name__.rpartition('.')[0]
            else:
                package = module.__name__
        else:
            package = module.__package__
    else:
        module = resolve_name(package)

    rootpkgname = package.partition('.')[0]

    rootpkg = resolve_name(rootpkgname)

    module_path = os.path.dirname(module.__file__)
    path = os.path.join(module_path, data_name)

    root_dir = os.path.dirname(rootpkg.__file__)
    if not _is_inside(path, root_dir):
        raise RuntimeError("attempted to get a local data file outside "
                           "of the {} tree.".format(rootpkgname))

    return path


def _find_hash_fn(hash):
    """
    Looks for a local file by hash - returns file name if found and a valid
    file, otherwise returns None.
    """

    try:
        with _cache() as (dldir, url2hash):
            hashfn = os.path.join(dldir, hash)
            if os.path.isfile(hashfn):
                return hashfn
            else:
                return None
    except OSError as e:
        msg = 'Could not access cache directory to search for data file: '
        warn(CacheMissingWarning(msg + str(e)))
        return None


def get_free_space_in_dir(path):
    """
    Given a path to a directory, returns the amount of free space (in
    bytes) on that filesystem.

    Parameters
    ----------
    path : str
        The path to a directory

    Returns
    -------
    bytes : int
        The amount of free space on the partition that the directory
        is on.
    """

    if sys.platform.startswith('win'):
        import ctypes
        free_bytes = ctypes.c_ulonglong(0)
        retval = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(path), None, None, ctypes.pointer(free_bytes))
        if retval == 0:
            raise OSError('Checking free space on {!r} failed '
                          'unexpectedly.'.format(path))
        return free_bytes.value
    else:
        stat = os.statvfs(path)
        return stat.f_bavail * stat.f_frsize


def check_free_space_in_dir(path, size):
    """
    Determines if a given directory has enough space to hold a file of
    a given size.  Raises an OSError if the file would be too large.

    Parameters
    ----------
    path : str
        The path to a directory

    size : int
        A proposed filesize (in bytes)

    Raises
    -------
    OSError : There is not enough room on the filesystem
    """
    from astropy.utils.console import human_file_size

    space = get_free_space_in_dir(path)
    if space < size:
        raise OSError(
            "Not enough free space in '{}' "
            "to download a {} file".format(
                path, human_file_size(size)))


def download_file(remote_url, cache=False, show_progress=True, timeout=None,
                  sources=None, update_cache=False):
    """Accepts a URL, downloads and optionally caches the result.

    It returns the filename, with a name determined by the file's MD5
    hash. If ``cache=True`` and the file is present in the cache, just
    returns the filename.

    Parameters
    ----------
    remote_url : str
        The URL of the file to download

    cache : bool, optional
        Whether to use the cache

    show_progress : bool, optional
        Whether to display a progress bar during the download (default
        is `True`). Regardless of this setting, the progress bar is only
        displayed when outputting to a terminal.

    timeout : float, optional
        The timeout, in seconds.  Otherwise, use
        `astropy.utils.data.Conf.remote_timeout`.

    sources : list of str, optional
        If provided, a list of URLs to try to obtain the file from. The
        result will be stored under the original URL. The original URL
        will *not* be tried unless it is in this list; this is to prevent
        long waits for a primary server that is known to be inaccessible
        at the moment.

    update_cache : bool, optional
        If true, attempt to download the file anew; if this is successful
        replace the current version. If not, raise a URLError but leave
        the existing cached value intact.

    Returns
    -------
    local_path : str
        Returns the local path that the file was download to.

    Raises
    ------
    urllib2.URLError, urllib.error.URLError
        Whenever there's a problem getting the remote file.

    Note
    ----
    Because this returns a filename, another process could run
    clear_download_cache before you actually open the file, leaving
    you with a filename that no longer points to a usable file.

    """

    from astropy.utils.console import ProgressBarOrSpinner

    if timeout is None:
        timeout = conf.remote_timeout
    if sources is None:
        sources = [remote_url]
    if not sources:
        raise ValueError(
            "No sources listed! Please include primary URL if you want it "
            "to be included as a valid source.")
    if update_cache and not cache:
        raise ValueError("update_cache only makes sense when using the cache!")

    missing_cache = False

    url_key = remote_url

    if cache:
        try:
            with _cache() as (dldir, url2hash):
                if not update_cache:
                    try:
                        return url2hash[url_key]
                    except KeyError:
                        pass
                else:
                    # Cache works
                    pass
        except OSError as e:
            msg = 'Remote data cache could not be accessed due to '
            estr = '' if len(e.args) < 1 else (': ' + str(e))
            warn(CacheMissingWarning(msg + e.__class__.__name__ + estr))

            cache = False
            update_cache = False
            missing_cache = True  # indicates that the cache is missing to raise a warning later

    errors = {}
    for source_url in sources:
        try:
            with urllib.request.urlopen(source_url, timeout=timeout) as remote:
                # keep a hash to rename the local file to the hashed name
                hash = hashlib.md5()

                info = remote.info()
                try:
                    size = int(info['Content-Length'])
                except (KeyError, ValueError, TypeError):
                    size = None

                if size is not None:
                    check_free_space_in_dir(gettempdir(), size)
                    if cache:
                        with _cache() as (dldir, url2hash):
                            check_free_space_in_dir(dldir, size)

                if show_progress and sys.stdout.isatty():
                    progress_stream = sys.stdout
                else:
                    progress_stream = io.StringIO()

                if source_url == remote_url:
                    dlmsg = f"Downloading {remote_url}"
                else:
                    dlmsg = f"Downloading {remote_url} from {source_url}"
                with ProgressBarOrSpinner(size, dlmsg, file=progress_stream) as p:
                    with NamedTemporaryFile(
                        prefix="astropy-download-{}-".format(os.getpid()),
                        delete=False
                    ) as f:
                        try:
                            bytes_read = 0
                            block = remote.read(conf.download_block_size)
                            while block:
                                f.write(block)
                                hash.update(block)
                                bytes_read += len(block)
                                p.update(bytes_read)
                                block = remote.read(conf.download_block_size)
                        except BaseException:
                            if os.path.exists(f.name):
                                os.remove(f.name)
                            raise
                # Success!
                break

        except urllib.error.URLError as e:
            if hasattr(e, 'reason') and hasattr(e.reason, 'errno') and e.reason.errno == 8:
                e.reason.strerror = e.reason.strerror + '. requested URL: ' + remote_url
                e.reason.args = (e.reason.errno, e.reason.strerror)
            errors[source_url] = e
        except socket.timeout as e:
            # this isn't supposed to happen, but occasionally a socket.timeout gets
            # through.  It's supposed to be caught in `urrlib2` and raised in this
            # way, but for some reason in mysterious circumstances it doesn't. So
            # we'll just re-raise it here instead
            errors[source_url] = e
    else:   # No success
        raise urllib.error.URLError(
            "Unable to open any source! Exceptions were {}".format(errors)) \
            from errors[sources[0]]

    if cache:
        # If there is something there already for url_key, probably because of
        # update_cache, clear it out to ensure that it gets deleted when
        # nothing else references it.
        clear_download_cache(url_key)
        local_path = _import_to_cache(url_key, f.name,
                                      hexdigest=hash.hexdigest(),
                                      remove_original=True)
    else:
        local_path = f.name
        if missing_cache:
            msg = ('File downloaded to temporary location due to problem '
                   'with cache directory and will not be cached.')
            warn(CacheMissingWarning(msg, local_path))
        if conf.delete_temporary_downloads_at_exit:
            global _tempfilestodel
            _tempfilestodel.append(local_path)

    return local_path


def is_url_in_cache(url_key):
    """Check if a download from ``url_key`` is in the cache.

    Parameters
    ----------
    url_key : string
        The URL retrieved

    Returns
    -------
    bool
        `True` if a download from ``url_key`` is in the cache

    See Also
    --------
    cache_contents : obtain a dictionary listing everything in the cache
    """
    try:
        with _cache() as (dldir, url2hash):
            return url_key in url2hash
    except OSError as e:
        msg = 'Remote data cache could not be accessed due to '
        estr = '' if len(e.args) < 1 else (': ' + str(e))
        warn(CacheMissingWarning(msg + e.__class__.__name__ + estr))
        return False


def cache_total_size():
    """Return the total size in bytes of all files in the cache."""
    with _cache() as (dldir, url2hash):
        return sum(os.path.getsize(os.path.join(dldir, h)) for h in url2hash.values())


def _do_download_files_in_parallel(kwargs):
    return download_file(**kwargs)


def download_files_in_parallel(urls,
                               cache=True,
                               show_progress=True,
                               timeout=None,
                               sources=None,
                               update_cache=False):
    """Download multiple files in parallel from the given URLs.

    Blocks until all files have downloaded.  The result is a list of
    local file paths corresponding to the given urls.

    Parameters
    ----------
    urls : list of str
        The URLs to retrieve.

    cache : bool, optional
        Whether to use the cache (default is `True`). If you want to check
        for new versions on the internet, use the update_cache option.

        .. versionchanged:: 3.0
            The default was changed to ``True`` and setting it to ``False`` will
            print a Warning and set it to ``True`` again, because the function
            will not work properly without cache.

    show_progress : bool, optional
        Whether to display a progress bar during the download (default
        is `True`)

    timeout : float, optional
        Timeout for each individual requests in seconds (default is the
        configurable `astropy.utils.data.Conf.remote_timeout`).

    sources : dict, optional
        If provided, for each URL a list of URLs to try to obtain the
        file from. The result will be stored under the original URL.
        For any URL in this dictionary, the original URL will *not* be
        tried unless it is in this list; this is to prevent long waits
        for a primary server that is known to be inaccessible at the
        moment.

    update_cache : bool, optional
        If true, attempt to download the file anew; if this is successful
        replace the current version. If not, raise a URLError but leave
        the existing cached value intact.

    Returns
    -------
    paths : list of str
        The local file paths corresponding to the downloaded URLs.

    Note
    ----
    If a URL is unreachable, the downloading will grind to a halt and the
    exception will propagate upward, but an unpredictable number of
    files will have been successfully downloaded and will remain in
    the cache.
    """
    from .console import ProgressBar

    if timeout is None:
        timeout = conf.remote_timeout
    if sources is None:
        sources = {}

    if not cache:
        # See issue #6662, on windows won't work because the files are removed
        # again before they can be used. On *NIX systems it will behave as if
        # cache was set to True because multiprocessing cannot insert the items
        # in the list of to-be-removed files. This could be fixed, but really,
        # just use the cache, with update_cache if appropriate.
        warn("Disabling the cache does not work because of multiprocessing, it "
             "will be set to ``True``. You may need to manually remove the "
             "cached files with clear_download_cache() afterwards.", AstropyWarning)
        cache = True
        update_cache = True

    if show_progress:
        progress = sys.stdout
    else:
        progress = io.BytesIO()

    # Combine duplicate URLs
    combined_urls = list(set(urls))
    combined_paths = ProgressBar.map(
        _do_download_files_in_parallel,
        [dict(remote_url=u,
              cache=cache,
              show_progress=False,
              timeout=timeout,
              sources=sources.get(u, [u]),
              update_cache=update_cache)
         for u in combined_urls],
        file=progress,
        multiprocess=True)
    paths = []
    for url in urls:
        paths.append(combined_paths[combined_urls.index(url)])
    return paths


# This is used by download_file and _deltemps to determine the files to delete
# when the interpreter exits
_tempfilestodel = []


@atexit.register
def _deltemps():

    global _tempfilestodel

    if _tempfilestodel is not None:
        while len(_tempfilestodel) > 0:
            fn = _tempfilestodel.pop()
            if os.path.isfile(fn):
                try:
                    os.remove(fn)
                except OSError:
                    # oh well we tried
                    # could be held open by some process, on Windows
                    pass


def clear_download_cache(hashorurl=None):
    """Clears the data file cache by deleting the local file(s).

    Parameters
    ----------
    hashorurl : str or None
        If None, the whole cache is cleared.  Otherwise, specifies
        a hash for the cached file that is supposed to be deleted,
        the full path to a file in the cache that should be deleted,
        or a URL that should be removed from the cache if present.

    """

    try:
        with _cache(write=True) as (dldir, url2hash):
            if hashorurl is None:
                if os.path.exists(dldir):
                    shutil.rmtree(dldir)
            elif _is_url(hashorurl):
                try:
                    filepath = url2hash.pop(hashorurl)
                    if not any(v == filepath for v in url2hash.values()):
                        try:
                            os.unlink(filepath)
                        except FileNotFoundError:
                            # Maybe someone else got it first
                            pass
                    return
                except KeyError:
                    pass
            else:  # it's a path
                filepath = os.path.join(dldir, hashorurl)
                if not _is_inside(filepath, dldir):
                    # URLs look like they are insided the directory
                    # Should this be ValueError? IOError?
                    raise RuntimeError(
                        "attempted to use clear_download_cache on the path {} "
                        "outside the data cache directory {}"
                        .format(filepath, dldir)
                    )
                if os.path.exists(filepath):
                    # Convert to list because we'll be modifying it as we go
                    for k, v in list(url2hash.items()):
                        if v == filepath:
                            del url2hash[k]
                    os.unlink(filepath)
                    return
                # Otherwise could not find file or url, but no worries.
                # Clearing download cache just makes sure that the file or url
                # is no longer in the cache regardless of starting condition.
    except OSError as e:
        msg = 'Not clearing data cache - cache inacessible due to '
        estr = '' if len(e.args) < 1 else (': ' + str(e))
        warn(CacheMissingWarning(msg + e.__class__.__name__ + estr))
        return


def _get_download_cache_locs():
    """Finds the path to the cache directory and makes them if they don't exist.

    Returns
    -------
    datadir : str
        The path to the data cache directory.
    shelveloc : str
        The path to the shelve object that stores the cache info.

    """
    from astropy.config.paths import get_cache_dir

    # datadir includes both the download files and the shelveloc.  This structure
    # is required since we cannot know a priori the actual file name corresponding
    # to the shelve map named shelveloc.  (The backend can vary and is allowed to
    # do whatever it wants with the filename.  Filename munging can and does happen
    # in practice).
    py_version = 'py' + str(sys.version_info.major)
    datadir = os.path.join(get_cache_dir(), 'download', py_version)
    shelveloc = os.path.join(datadir, 'urlmap')

    if not os.path.exists(datadir):
        try:
            os.makedirs(datadir)
        except OSError:
            if not os.path.exists(datadir):
                raise
    elif not os.path.isdir(datadir):
        msg = 'Data cache directory {0} is not a directory'
        raise OSError(msg.format(datadir))

    if os.path.isdir(shelveloc):
        msg = 'Data cache shelve object location {0} is a directory'
        raise OSError(msg.format(shelveloc))

    return datadir, shelveloc


# the cache directory must be locked before any writes are performed.  Same for
# the hash shelve, so this should be used for both.
#
# In fact the shelve, if you're using gdbm (the default on Linux), it can't
# be open for reading if someone has it open for writing. So we need to lock
# access to the shelve even for reading.
@contextlib.contextmanager
def _cache_lock():
    lockdir = os.path.join(_get_download_cache_locs()[0], 'lock')
    pidfn = os.path.join(lockdir, 'pid')
    try:
        msg = ("Config file requests {} tries"
               .format(conf.download_cache_lock_attempts))
        waited = 0.
        while True:
            try:
                os.mkdir(lockdir)
            except OSError:
                try:
                    with open(pidfn) as f:
                        pid = int(f.read())
                    # Could check if pid is os.getpid() and notify the user
                    # we deadlocked but it might be another thread in this process.
                    msg = (
                        "Cache directory is locked by process {} after waiting {} s. "
                        "If this is not a running python process, delete the directory "
                        "\"{}\" and all its contents to break the lock."
                        .format(pid, waited, lockdir)
                    )
                    # Somebody has the lock. So wait.
                except (ValueError, IOError):
                    pid = None
                    msg = (
                        "Cache lock exists but is in an inconsistent state after "
                        "{} s. This may indicate an astropy bug or that kill -9 "
                        "was used. If you want to unlock the cache remove the "
                        "directory or file \"{}\"."
                        .format(waited, lockdir)
                    )
                    # We could bail out here but there is a sliver of time during
                    # which the directory exists but the pidfile hasn't been created
                    # yet. So wait.
            else:
                # Got the lock!
                # write the pid of this process for informational purposes
                with open(pidfn, 'w') as f:
                    f.write(str(os.getpid()))
                break
            if waited < conf.download_cache_lock_attempts:
                # Wait to try again
                # slightly random wait time to desynchronize retries
                # unfortunately multiprocessing uses fork() so
                # subprocesses all see the same random seed
                # could include threading.current_thread().ident as well
                pid_based_random = hash(str(os.getpid()))/2.**sys.hash_info.width
                dt = 0.05*(1+pid_based_random)
                time.sleep(dt)
                waited += dt
            else:
                # Never did get the lock.
                raise RuntimeError(msg)

        yield

    finally:
        if os.path.exists(lockdir):
            if os.path.isdir(lockdir):
                # if the pid file is present, be sure to remove it
                if os.path.exists(pidfn):
                    os.remove(pidfn)
                os.rmdir(lockdir)
            else:
                msg = 'Error releasing lock. "{}" exists but is not a directory.'
                raise RuntimeError(msg.format(lockdir))
        else:
            # Just in case we were called from _clear_download_cache
            # or something went wrong before creating the directory
            pass


@contextlib.contextmanager
def _cache(write=False):
    # if clear_download_cache happens while a reader
    # is working then Bad Things will happen. But in any case the
    # API uses filenames so clear_download cache can disappear
    # them out from under users well after we've exited any
    # locking.

    dldir, urlmapfn = _get_download_cache_locs()
    # raises an error if cache inaccessible
    if write:
        with _cache_lock(), shelve.open(urlmapfn, flag="c") as url2hash:
            yield dldir, url2hash
    else:
        try:
            with _cache_lock(), shelve.open(urlmapfn, flag="r") as url2hash:
                # Copy so we can release the lock.
                d = dict(url2hash.items())
        except dbm.error:
            # Might be a "file not found", might be something serious,
            # no way to tell as shelve just gives you a plain dbm.error
            # Also the file doesn't have a platform-independent name
            d = {}
        yield dldir, d


def check_download_cache(check_hashes=False):
    """Do a consistency check on the cache

    Because the cache is shared by all versions of astropy in all virtualenvs
    run by your user, possibly concurrently, it could accumulate problems.
    This could lead to hard-to-debug problems or wasted space. This function
    detects a number of incorrect conditions, including nonexistent files that
    are indexed, files that are indexed but in the wrong place, and, if you
    request it, files whose content does not mash the hash that is indexed.

    This funtion also returns a list of non-indexed files. A few will be
    associated with the shelve object; their exact names depend on the backend
    used but will probably be based on ``urlmap``. The presence of other files,
    particularly those that 32-character hex strings or start with those,
    probably indicates that something has gone wrong and inaccessible files
    have accumulated in the cache. These can be removed with
    `clear_download_cache`, either passing the filename returned here, or
    with no arguments to  empty the entire cache and return it to a
    reasonable, if empty, state.

    Returns
    -------
    strays : set of strings
        This is the set of files in the cache directory that do not correspond
        to known URLs.
    """
    # We're only reading but without the lock goodness knows what
    # inconsistencies might be detected
    with _cache(write=True) as (dldir, url2hash):
        hash_files = set(os.path.join(dldir, k)
                         for k in os.listdir(dldir))
        try:
            hash_files.remove(os.path.join(dldir, "urlmap"))
        except KeyError:
            pass
        try:
            hash_files.remove(os.path.join(dldir, "lock"))
        except KeyError:
            raise ValueError("Lock file missing!?")
        for u, h in url2hash.items():
            if not os.path.exists(h):
                msg = "URL '{}' points to nonexistent file '{}'".format(
                        u, h)
                raise ValueError(msg)
            try:
                hash_files.remove(h)
            except KeyError:
                # Huh. Two different URLs retrieved identical files.
                pass
            d, hexdigest = os.path.split(h)
            if dldir != d:
                msg = "Expected downloaded files to be in '{}' but " +\
                      "URL '{}' points to file '{}'".format(
                              dldir, u, h)
                raise ValueError(msg)
            if check_hashes:
                hexdigest_file = compute_hash(h)
                if hexdigest_file != hexdigest:
                    msg = "File corresponding to {} has contents that " +\
                          "do not match the hash value: should be '{}' " +\
                          "but is '{}'".format(
                                  u, hexdigest, hexdigest_file)
                    raise ValueError(msg)
    return hash_files


def _import_to_cache(url_key, filename,
                     hexdigest=None,
                     remove_original=False):
    """Import the on-disk file specified by filename to the cache"""
    if hexdigest is None:
        hexdigest = compute_hash(filename)
    with _cache(write=True) as (dldir, url2hash):
        # We check now to see if another process has
        # inadvertently written the file underneath us
        # already
        if url_key not in url2hash:
            local_path = os.path.join(dldir, hexdigest)
            if remove_original:
                shutil.move(filename, local_path)
            else:
                shutil.copy(filename, local_path)
            url2hash[url_key] = local_path
        else:
            if remove_original:
                os.remove(filename)
        return url2hash[url_key]


def get_cached_urls():
    """
    Get the list of URLs in the cache. Especially useful for looking up what
    files are stored in your cache when you don't have internet access.

    Returns
    -------
    cached_urls : list
        List of cached URLs.

    See Also
    --------
    cache_contents : obtain a dictionary listing everything in the cache
    """
    try:
        with _cache() as (dldir, url2hash):
            return list(url2hash.keys())
    except OSError as e:
        msg = 'Remote data cache could not be accessed due to '
        estr = '' if len(e.args) < 1 else (': ' + str(e))
        warn(CacheMissingWarning(msg + e.__class__.__name__ + estr))
        return []


def cache_contents():
    """Obtain a dict mapping cached URLs to filenames."""
    with _cache() as (dldir, url2hash):
        return {k: os.path.join(dldir, v) for (k, v) in url2hash.items()}


_cache_zip_index_name = "index.json"


def export_download_cache(filename_or_obj, urls=None):
    """Exports the cache contents as a ZIP file

    Parameters
    ----------
    filename_or_obj : str or file-like
        Where to put the created ZIP file. Must be something the zipfile
        module can write to.
    urls : iterable of str or None
        The URLs to include in the exported cache. The default is all
        URLs currently in the cache. If a URL is included in this list
        but is not currently in the cache, it will be downloaded to the
        cache first and then exported.

    See Also
    --------
    import_download_cache : import the contents of such a ZIP file into the cache
    """
    if urls is None:
        urls = get_cached_urls()
    added = set()
    with zipfile.ZipFile(filename_or_obj, 'w') as z:
        index = {}
        for u in urls:
            fn = download_file(u, cache=True)
            z_fn = os.path.join("cache", os.path.basename(fn))
            if z_fn not in added:
                index[u] = z_fn
                z.write(fn, z_fn)
                added.add(z_fn)
        z.writestr(_cache_zip_index_name, json.dumps(index))


def import_download_cache(filename_or_obj, urls=None, update_cache=False):
    """Imports the contents of a ZIP file into the cache

    The ZIP file must be in the format produced by `export_download_cache`,
    specifically it must have a file ``index.json`` that is a dictionary
    mapping URLs to filenames inside the ZIP.

    Parameters
    ----------
    filename_or_obj : str or file-like
        Where to put the created ZIP file. Must be something the zipfile
        module can write to.
    urls : iterable of str or None
        The URLs to import from the ZIP file. The default is all
        URLs in the file.
    update_cache : bool, optional
        If True, any entry in the ZIP file will overwrite the value in the
        cache; if False, leave untouched any entry already in the cache.

    See Also
    --------
    export_download_cache : export the contents the cache to of such a ZIP file
    """
    block_size = 65536
    with zipfile.ZipFile(filename_or_obj, 'r') as z, TemporaryDirectory() as d:
        index = json.loads(z.read(_cache_zip_index_name))
        if urls is None:
            urls = index.keys()
        for i, k in enumerate(urls):
            if not update_cache and is_url_in_cache(k):
                continue
            v = index[k]
            f_temp_name = os.path.join(d, str(i))
            with z.open(v) as f_zip, open(f_temp_name, "wb") as f_temp:
                hash = hashlib.md5()
                block = f_zip.read(block_size)
                while block:
                    f_temp.write(block)
                    hash.update(block)
                    block = f_zip.read(block_size)
                hexdigest = hash.hexdigest()
            _import_to_cache(k, f_temp_name,
                             hexdigest=hexdigest,
                             remove_original=True)
