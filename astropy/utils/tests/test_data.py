# -*- coding: utf-8 -*-
# Licensed under a 3-clause BSD style license - see LICENSE.rst

import os
import sys
import base64
import hashlib
import pathlib
import tempfile
import contextlib
import urllib.error
import urllib.parse
import urllib.request
import warnings
from tempfile import NamedTemporaryFile

import pytest

from astropy.utils import data
from astropy.config import paths
from astropy.utils.data import (
    CacheMissingWarning, conf, compute_hash, download_file, get_cached_urls,
    is_url_in_cache, check_download_cache, clear_download_cache,
    get_pkg_data_fileobj, get_readable_fileobj, export_download_cache,
    get_pkg_data_contents, get_pkg_data_filename, import_download_cache,
    _get_download_cache_locs, download_files_in_parallel)
from astropy.tests.helper import raises, catch_warnings

TESTURL = 'http://www.astropy.org'
TESTURL2 = "http://www.astropy.org/about.html"
TESTLOCAL = get_pkg_data_filename(os.path.join('data', 'local.dat'))

# General file object function

try:
    import bz2  # noqa
except ImportError:
    HAS_BZ2 = False
else:
    HAS_BZ2 = True

try:
    import lzma  # noqa
except ImportError:
    HAS_XZ = False
else:
    HAS_XZ = True


@contextlib.contextmanager
def preserve_cache(clear=False, check=False):
    """Allow tests to operate without wiping existing cache"""
    with NamedTemporaryFile("wb") as zip_file:
        export_download_cache(zip_file)
        try:
            if clear:
                clear_download_cache()
            yield
            if check:
                check_download_cache(check_hashes=True)
        finally:
            import_download_cache(zip_file.name)


@pytest.mark.remote_data(source='astropy')
def test_download_nocache():
    fnout = download_file(TESTURL)
    assert os.path.isfile(fnout)


@pytest.mark.remote_data(source='astropy')
def test_download_parallel():
    main_url = conf.dataurl
    mirror_url = conf.dataurl_mirror
    fileloc = 'intersphinx/README'
    try:
        fnout = download_files_in_parallel([main_url, main_url + fileloc])
    except urllib.error.URLError:  # Use mirror if timed out
        fnout = download_files_in_parallel([mirror_url, mirror_url + fileloc])
    assert all([os.path.isfile(f) for f in fnout]), fnout


def url_to(path):
    return pathlib.Path(path).resolve().as_uri()


def test_clear_download_multiple_references():
    """Check that files with the same hash don't confuse the storage."""
    content = "Test data; doesn't matter much.\n"

    # First ensure that there is no file with these contents in the cache
    with NamedTemporaryFile("w+") as a:
        a.write(content)
        a_url = url_to(a.name)
        clear_download_cache(a_url)
        a_hash = download_file(a_url, cache=True)
        clear_download_cache(a_hash)

    # Now make two URLs with the same content
    f_hash = None
    g_hash = None
    with NamedTemporaryFile("w+") as f:
        f.write(content)
        f_url = url_to(f.name)
        clear_download_cache(f_url)
        f_hash = download_file(f_url, cache=True)
    with NamedTemporaryFile("w+") as g:
        g.write(content)
        g_url = url_to(g.name)
        clear_download_cache(g_url)
        g_hash = download_file(g_url, cache=True)
    assert f_hash == g_hash
    assert is_url_in_cache(f_url)
    assert is_url_in_cache(g_url)

    clear_download_cache(f_url)
    assert not is_url_in_cache(f_url)
    assert is_url_in_cache(g_url)
    assert os.path.exists(g_hash), \
        "Contents should not be deleted while a reference exists"

    clear_download_cache(g_url)
    assert not os.path.exists(g_hash), \
        "No reference exists any more, file should be deleted"


@contextlib.contextmanager
def make_url(contents, delete=True):
    with NamedTemporaryFile("w+") as f:
        f.write(contents)
        f.flush()
        url = url_to(f.name)
        clear_download_cache(url)
        if not delete:
            yield url
    if delete:
        yield url


def test_sources_normal():
    with make_url("primary", delete=False) as primary:
        with make_url("fallback1", delete=False) as fallback1:
            f = download_file(primary, cache=True,
                              sources=[primary, fallback1])
            assert open(f).read() == "primary"
            assert not is_url_in_cache(fallback1)


def test_sources_fallback():
    with make_url("primary", delete=True) as primary:
        with make_url("fallback1", delete=False) as fallback1:
            f = download_file(primary, cache=True,
                              sources=[primary, fallback1])
            assert open(f).read() == "fallback1"
            assert not is_url_in_cache(fallback1)


def test_sources_ignore_primary():
    with make_url("primary", delete=False) as primary:
        with make_url("fallback1", delete=False) as fallback1:
            f = download_file(primary, cache=True,
                              sources=[fallback1])
            assert open(f).read() == "fallback1"
            assert not is_url_in_cache(fallback1)


def test_sources_multiple():
    with make_url("primary", delete=True) as primary:
        with make_url("fallback1", delete=True) as fallback1:
            with make_url("fallback2", delete=False) as fallback2:
                f = download_file(primary, cache=True,
                                  sources=[primary,
                                           fallback1,
                                           fallback2])
                assert open(f).read() == "fallback2"
                assert not is_url_in_cache(fallback1)
                assert not is_url_in_cache(fallback2)


def test_sources_multiple_missing():
    with make_url("primary", delete=True) as primary:
        with make_url("fallback1", delete=True) as fallback1:
            with make_url("fallback2", delete=True) as fallback2:
                with pytest.raises(urllib.error.URLError):
                    f = download_file(primary, cache=True,
                                      sources=[primary,
                                               fallback1,
                                               fallback2])
                assert not is_url_in_cache(fallback1)
                assert not is_url_in_cache(fallback2)


def test_update_url():
    with NamedTemporaryFile("w+") as f:
        f.write("old")
        f.flush()
        f_url = url_to(f.name)
        assert open(download_file(f_url, cache=True)).read() == "old"
        with open(f.name, "w") as g:
            g.write("new")
        assert open(download_file(f_url, cache=True)).read() == "old"
        assert open(download_file(f_url, cache=True,
                                  update_cache=True)).read() == "new"
    assert open(download_file(f_url, cache=True)).read() == "new"
    with pytest.raises(urllib.error.URLError):
        download_file(f_url, cache=True,
                      update_cache=True)
    assert open(download_file(f_url, cache=True)).read() == "new"


@pytest.mark.remote_data(source='astropy')
def test_download_noprogress():
    fnout = download_file(TESTURL, show_progress=False)
    assert os.path.isfile(fnout)


@pytest.mark.remote_data(source='astropy')
def test_download_cache():

    download_dir = _get_download_cache_locs()[0]

    # Download the test URL and make sure it exists, then clear just that
    # URL and make sure it got deleted.
    fnout = download_file(TESTURL, cache=True)
    assert os.path.isdir(download_dir)
    assert os.path.isfile(fnout)
    clear_download_cache(TESTURL)
    assert not os.path.exists(fnout)

    # Clearing download cache succeeds even if the URL does not exist.
    clear_download_cache('http://this_was_never_downloaded_before.com')

    # Make sure lockdir was released
    lockdir = os.path.join(download_dir, 'lock')
    assert not os.path.isdir(lockdir), 'Cache dir lock was not released!'


@pytest.mark.remote_data(source='astropy')
def test_download_cache_after_clear():

    download_dir = _get_download_cache_locs()[0]

    with preserve_cache():
        # Test issues raised in #4427 with clear_download_cache() without a URL,
        # followed by subsequent download.
        fnout = download_file(TESTURL, cache=True)
        assert os.path.isfile(fnout)
        clear_download_cache()
        assert not os.path.exists(fnout)
        assert not os.path.exists(download_dir)
        fnout = download_file(TESTURL, cache=True)
        assert os.path.isfile(fnout)


@pytest.mark.remote_data(source='astropy')
def test_url_nocache():
    with get_readable_fileobj(TESTURL, cache=False, encoding='utf-8') as page:
        assert page.read().find('Astropy') > -1


@pytest.mark.remote_data(source='astropy')
def test_find_by_hash():

    with get_readable_fileobj(TESTURL, encoding="binary", cache=True) as page:
        hash = hashlib.md5(page.read())

    hashstr = 'hash/' + hash.hexdigest()

    fnout = get_pkg_data_filename(hashstr)
    assert os.path.isfile(fnout)
    clear_download_cache(hashstr[5:])
    assert not os.path.isfile(fnout)

    lockdir = os.path.join(_get_download_cache_locs()[0], 'lock')
    assert not os.path.isdir(lockdir), 'Cache dir lock was not released!'


@pytest.mark.remote_data(source='astropy')
def test_find_invalid():
    # this is of course not a real data file and not on any remote server, but
    # it should *try* to go to the remote server
    with pytest.raises(urllib.error.URLError):
        get_pkg_data_filename('kjfrhgjklahgiulrhgiuraehgiurhgiuhreglhurieghruelighiuerahiulruli')


# Package data functions
@pytest.mark.parametrize(('filename'), ['local.dat', 'local.dat.gz',
                                        'local.dat.bz2', 'local.dat.xz'])
def test_local_data_obj(filename):

    if (not HAS_BZ2 and 'bz2' in filename) or (not HAS_XZ and 'xz' in filename):
        with pytest.raises(ValueError) as e:
            with get_pkg_data_fileobj(os.path.join('data', filename), encoding='binary') as f:
                f.readline()
                # assert f.read().rstrip() == b'CONTENT'
        assert ' format files are not supported' in str(e.value)
    else:
        with get_pkg_data_fileobj(os.path.join('data', filename), encoding='binary') as f:
            f.readline()
            assert f.read().rstrip() == b'CONTENT'


@pytest.fixture(params=['invalid.dat.bz2', 'invalid.dat.gz'])
def bad_compressed(request, tmpdir):

    # These contents have valid headers for their respective file formats, but
    # are otherwise malformed and invalid.
    bz_content = b'BZhinvalid'
    gz_content = b'\x1f\x8b\x08invalid'

    datafile = tmpdir.join(request.param)
    filename = datafile.strpath

    if filename.endswith('.bz2'):
        contents = bz_content
    elif filename.endswith('.gz'):
        contents = gz_content
    else:
        contents = 'invalid'

    datafile.write(contents, mode='wb')

    return filename


def test_local_data_obj_invalid(bad_compressed):

    is_bz2 = bad_compressed.endswith('.bz2')
    is_xz = bad_compressed.endswith('.xz')

    # Note, since these invalid files are created on the fly in order to avoid
    # problems with detection by antivirus software
    # (see https://github.com/astropy/astropy/issues/6520), it is no longer
    # possible to use ``get_pkg_data_fileobj`` to read the files. Technically,
    # they're not local anymore: they just live in a temporary directory
    # created by pytest. However, we can still use get_readable_fileobj for the
    # test.
    if (not HAS_BZ2 and is_bz2) or (not HAS_XZ and is_xz):
        with pytest.raises(ValueError) as e:
            with get_readable_fileobj(bad_compressed, encoding='binary') as f:
                f.read()
        assert ' format files are not supported' in str(e.value)
    else:
        with get_readable_fileobj(bad_compressed, encoding='binary') as f:
            assert f.read().rstrip().endswith(b'invalid')


def test_local_data_name():
    assert os.path.isfile(TESTLOCAL) and TESTLOCAL.endswith('local.dat')

    # TODO: if in the future, the root data/ directory is added in, the below
    # test should be uncommented and the README.rst should be replaced with
    # whatever file is there

    # get something in the astropy root
    # fnout2 = get_pkg_data_filename('../../data/README.rst')
    # assert os.path.isfile(fnout2) and fnout2.endswith('README.rst')


def test_data_name_third_party_package():
    """Regression test for issue #1256

    Tests that `get_pkg_data_filename` works in a third-party package that
    doesn't make any relative imports from the module it's used from.

    Uses a test package under ``data/test_package``.
    """

    # Get the actual data dir:
    data_dir = os.path.join(os.path.dirname(__file__), 'data')

    sys.path.insert(0, data_dir)
    try:
        import test_package
        filename = test_package.get_data_filename()
        assert filename == os.path.join(data_dir, 'test_package', 'data',
                                        'foo.txt')
    finally:
        sys.path.pop(0)


@raises(RuntimeError)
def test_local_data_nonlocalfail():
    # this would go *outside* the astropy tree
    get_pkg_data_filename('../../../data/README.rst')


def test_compute_hash(tmpdir):

    rands = b'1234567890abcdefghijklmnopqrstuvwxyz'

    filename = tmpdir.join('tmp.dat').strpath

    with open(filename, 'wb') as ntf:
        ntf.write(rands)
        ntf.flush()

    chhash = compute_hash(filename)
    shash = hashlib.md5(rands).hexdigest()

    assert chhash == shash


def test_get_pkg_data_contents():

    with get_pkg_data_fileobj('data/local.dat') as f:
        contents1 = f.read()

    contents2 = get_pkg_data_contents('data/local.dat')

    assert contents1 == contents2


@pytest.mark.remote_data(source='astropy')
def test_data_noastropy_fallback(monkeypatch):
    """
    Tests to make sure the default behavior when the cache directory can't
    be located is correct
    """

    # needed for testing the *real* lock at the end
    lockdir = os.path.join(_get_download_cache_locs()[0], 'lock')

    # better yet, set the configuration to make sure the temp files are deleted
    conf.delete_temporary_downloads_at_exit = True

    # make sure the config and cache directories are not searched
    monkeypatch.setenv('XDG_CONFIG_HOME', 'foo')
    monkeypatch.delenv('XDG_CONFIG_HOME')
    monkeypatch.setenv('XDG_CACHE_HOME', 'bar')
    monkeypatch.delenv('XDG_CACHE_HOME')

    monkeypatch.setattr(paths.set_temp_config, '_temp_path', None)
    monkeypatch.setattr(paths.set_temp_cache, '_temp_path', None)

    # make sure the _find_or_create_astropy_dir function fails as though the
    # astropy dir could not be accessed
    def osraiser(dirnm, linkto):
        raise OSError
    monkeypatch.setattr(paths, '_find_or_create_astropy_dir', osraiser)

    with pytest.raises(OSError):
        # make sure the config dir search fails
        paths.get_cache_dir()

    # first try with cache
    with catch_warnings(CacheMissingWarning) as w:
        warnings.simplefilter("ignore", ResourceWarning)
        fnout = data.download_file(TESTURL, cache=True)

    assert os.path.isfile(fnout)

    assert len(w) > 1

    w1 = w.pop(0)
    w2 = w.pop(0)

    assert w1.category == CacheMissingWarning
    assert 'Remote data cache could not be accessed' in w1.message.args[0]
    assert w2.category == CacheMissingWarning
    assert 'File downloaded to temporary location' in w2.message.args[0]
    assert fnout == w2.message.args[1]

    # clearing the cache should be a no-up that doesn't affect fnout
    with catch_warnings(CacheMissingWarning) as w:
        data.clear_download_cache(TESTURL)
    assert os.path.isfile(fnout)

    # now remove it so tests don't clutter up the temp dir this should get
    # called at exit, anyway, but we do it here just to make sure it's working
    # correctly
    data._deltemps()
    assert not os.path.isfile(fnout)

    assert len(w) > 0
    w3 = w.pop()

    assert w3.category == data.CacheMissingWarning
    assert 'Not clearing data cache - cache inacessable' in str(w3.message)

    # now try with no cache
    with catch_warnings(CacheMissingWarning) as w:
        fnnocache = data.download_file(TESTURL, cache=False)
    with open(fnnocache, 'rb') as page:
        assert page.read().decode('utf-8').find('Astropy') > -1

    # no warnings should be raise in fileobj because cache is unnecessary
    assert len(w) == 0

    # lockdir determined above as the *real* lockdir, not the temp one
    assert not os.path.isdir(lockdir), 'Cache dir lock was not released!'


@pytest.mark.parametrize(('filename'), [
    'unicode.txt',
    'unicode.txt.gz',
    pytest.param('unicode.txt.bz2', marks=pytest.mark.xfail(not HAS_BZ2, reason='no bz2 support')),
    pytest.param('unicode.txt.xz', marks=pytest.mark.xfail(not HAS_XZ, reason='no lzma support'))])
def test_read_unicode(filename):

    contents = get_pkg_data_contents(os.path.join('data', filename), encoding='utf-8')
    assert isinstance(contents, str)
    contents = contents.splitlines()[1]
    assert contents == "האסטרונומי פייתון"

    contents = get_pkg_data_contents(os.path.join('data', filename), encoding='binary')
    assert isinstance(contents, bytes)
    x = contents.splitlines()[1]
    assert x == (b"\xff\xd7\x94\xd7\x90\xd7\xa1\xd7\x98\xd7\xa8\xd7\x95\xd7\xa0"
                 b"\xd7\x95\xd7\x9e\xd7\x99 \xd7\xa4\xd7\x99\xd7\x99\xd7\xaa\xd7\x95\xd7\x9f"[1:])


def test_compressed_stream():

    gzipped_data = (b"H4sICIxwG1AAA2xvY2FsLmRhdAALycgsVkjLzElVANKlxakpCpl5CiUZqQ"
                    b"olqcUl8Tn5yYk58SmJJYnxWmCRzLx0hbTSvOSSzPy8Yi5nf78QV78QLgAlLytnRQAAAA==")
    gzipped_data = base64.b64decode(gzipped_data)
    assert isinstance(gzipped_data, bytes)

    class FakeStream:
        """
        A fake stream that has `read`, but no `seek`.
        """

        def __init__(self, data):
            self.data = data

        def read(self, nbytes=None):
            if nbytes is None:
                result = self.data
                self.data = b''
            else:
                result = self.data[:nbytes]
                self.data = self.data[nbytes:]
            return result

    stream = FakeStream(gzipped_data)
    with get_readable_fileobj(stream, encoding='binary') as f:
        f.readline()
        assert f.read().rstrip() == b'CONTENT'


@pytest.mark.remote_data(source='astropy')
def test_invalid_location_download():
    """
    checks that download_file gives a URLError and not an AttributeError,
    as its code pathway involves some fiddling with the exception.
    """

    with pytest.raises(urllib.error.URLError):
        download_file('http://www.astropy.org/nonexistentfile')


def test_invalid_location_download_noconnect():
    """
    checks that download_file gives an OSError if the socket is blocked
    """

    # This should invoke socket's monkeypatched failure
    with pytest.raises(OSError):
        download_file('http://astropy.org/nonexistentfile')


@pytest.mark.remote_data(source='astropy')
def test_is_url_in_cache():

    assert not is_url_in_cache('http://astropy.org/nonexistentfile')

    download_file(TESTURL, cache=True, show_progress=False)
    assert is_url_in_cache(TESTURL)


@pytest.mark.remote_data(source='astropy')
def test_check_download_cache():
    with preserve_cache():
        with NamedTemporaryFile("wb") as zip_file:
            clear_download_cache()
            assert not check_download_cache()
            download_file(TESTURL, cache=True)
            normal = check_download_cache()
            download_file(TESTURL2, cache=True)
            assert check_download_cache() == normal
            export_download_cache(zip_file, [TESTURL, TESTURL2])
            assert check_download_cache(check_hashes=True) == normal
            clear_download_cache(TESTURL2)
            assert check_download_cache() == normal
            import_download_cache(zip_file.name, [TESTURL])
            assert check_download_cache(check_hashes=True) == normal


@pytest.mark.remote_data(source='astropy')
def test_export_import_roundtrip_one():
    with NamedTemporaryFile("wb") as zip_file:
        with open(download_file(TESTURL, cache=True, show_progress=False),
                  "rb") as f:
            contents = f.read()
        normal = check_download_cache()
        initial_urls_in_cache = sorted(get_cached_urls())
        export_download_cache(zip_file, [TESTURL])
        clear_download_cache(TESTURL)
        import_download_cache(zip_file.name)
        assert is_url_in_cache(TESTURL)
        assert sorted(get_cached_urls()) == initial_urls_in_cache
        with open(download_file(TESTURL, cache=True, show_progress=False),
                  "rb") as f:
            new_contents = f.read()
        assert new_contents == contents
        assert check_download_cache(check_hashes=True) == normal


@pytest.mark.remote_data(source='astropy')
def test_export_with_download():
    with NamedTemporaryFile("wb") as zip_file:
        clear_download_cache(TESTURL)
        export_download_cache(zip_file, [TESTURL])
        assert is_url_in_cache(TESTURL)


@pytest.mark.remote_data(source='astropy')
def test_import_one():
    with NamedTemporaryFile("wb") as zip_file:
        download_file(TESTURL, cache=True)
        download_file(TESTURL2, cache=True)
        assert is_url_in_cache(TESTURL2)
        export_download_cache(zip_file, [TESTURL, TESTURL2])
        clear_download_cache(TESTURL)
        clear_download_cache(TESTURL2)
        import_download_cache(zip_file.name, [TESTURL])
        assert is_url_in_cache(TESTURL)
        assert not is_url_in_cache(TESTURL2)


@pytest.mark.remote_data(source='astropy')
def test_export_import_roundtrip():
    # No preserve_cache here - if this test fails then preserve_cache
    # is broken, and if it doesn't raise an error the cache is fine.
    with NamedTemporaryFile("wb") as zip_file:
        download_file(TESTURL, cache=True)
        download_file(TESTURL2, cache=True)
        normal = check_download_cache()
        initial_urls_in_cache = sorted(get_cached_urls())
        export_download_cache(zip_file)
        clear_download_cache()
        import_download_cache(zip_file.name)
        assert sorted(get_cached_urls()) == initial_urls_in_cache
        assert check_download_cache(check_hashes=True) == normal


def test_get_readable_fileobj_cleans_up_temporary_files(tmpdir, monkeypatch):
    """checks that get_readable_fileobj leaves no temporary files behind"""
    # Create a 'file://' URL pointing to a path on the local filesystem
    url = url_to(TESTLOCAL)

    # Save temporary files to a known location
    monkeypatch.setattr(tempfile, 'tempdir', str(tmpdir))

    # Call get_readable_fileobj() as a context manager
    with get_readable_fileobj(url):
        pass

    # Get listing of files in temporary directory
    tempdir_listing = tmpdir.listdir()

    # Assert that the temporary file was empty after get_readable_fileobj()
    # context manager finished running
    assert len(tempdir_listing) == 0


def test_path_objects_get_readable_fileobj():
    fpath = pathlib.Path(TESTLOCAL)
    with get_readable_fileobj(fpath) as f:
        assert f.read().rstrip() == ('This file is used in the test_local_data_* '
                                     'testing functions\nCONTENT')


def test_nested_get_readable_fileobj():
    """Ensure fileobj state is as expected when get_readable_fileobj()
    is called inside another get_readable_fileobj().
    """
    with get_readable_fileobj(TESTLOCAL, encoding='binary') as fileobj:
        with get_readable_fileobj(fileobj, encoding='UTF-8') as fileobj2:
            fileobj2.seek(1)
        fileobj.seek(1)

        # Theoretically, fileobj2 should be closed already here but it is not.
        # See https://github.com/astropy/astropy/pull/8675.
        # UNCOMMENT THIS WHEN PYTHON FINALLY LETS IT HAPPEN.
        #assert fileobj2.closed

    assert fileobj.closed and fileobj2.closed
