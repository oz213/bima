"""Offline tests for automatic Chrome + ChromeDriver compatibility management."""
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock
import zipfile
import sys

import chrome_runtime as rt
import crawler_multi as cm


VERSION = '157.0.8103.38'
BASE = 'https://storage.googleapis.com/chrome-for-testing-public/' + VERSION + '/linux64/'


def zip_data(folder, binary, shell_text, extra_bytes=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode='w') as archive:
        info = zipfile.ZipInfo(folder + '/' + binary)
        info.external_attr = (0o100755 << 16)
        archive.writestr(info, shell_text)
        archive.writestr(folder + '/resources.bin', extra_bytes or os.urandom(4096))
    return buf.getvalue()


class FakeResponse:
    def __init__(self, payload=None, blob=None, status=200):
        self.payload, self.blob, self.status_code = payload, blob, status
        self.closed = False

    def raise_for_status(self):
        if not 200 <= self.status_code < 300:
            raise RuntimeError('HTTP ' + str(self.status_code))

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        for i in range(0, len(self.blob), chunk_size):
            yield self.blob[i:i + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, bad_metadata=False):
        self.calls = []
        self.bad_metadata = bad_metadata
        self.metadata = {'channels': {'Stable': {'version': VERSION, 'downloads': {
            'chrome': [{'platform': 'linux64', 'url': BASE + 'chrome-linux64.zip'}],
            'chromedriver': [{'platform': 'linux64', 'url': BASE + 'chromedriver-linux64.zip'}],
        }}}}
        self.chrome_zip = zip_data('chrome-linux64', 'chrome',
                                   '#!/bin/sh\necho "Google Chrome for Testing ' + VERSION + '"\n')
        self.driver_zip = zip_data('chromedriver-linux64', 'chromedriver',
                                   '#!/bin/sh\necho "ChromeDriver ' + VERSION + '"\n')

    def get(self, url, **kwargs):
        self.calls.append(url)
        if self.bad_metadata:
            raise ConnectionError('official Chrome release metadata unavailable')
        if url == rt.METADATA_URL:
            return FakeResponse(payload=self.metadata)
        if url == BASE + 'chrome-linux64.zip':
            return FakeResponse(blob=self.chrome_zip)
        if url == BASE + 'chromedriver-linux64.zip':
            return FakeResponse(blob=self.driver_zip)
        raise AssertionError('Unexpected URL: ' + url)


class RuntimeTests(unittest.TestCase):
    def test_clean_install_checks_full_versions_and_reuses_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeSession()
            runtime = rt.ensure_browser(directory, session=fake)
            self.assertTrue(rt.valid_runtime(runtime))
            self.assertEqual(runtime.version, VERSION)
            self.assertEqual(len(fake.calls), 3)
            self.assertTrue(runtime.chrome.stat().st_mode & 0o100)
            next_session = FakeSession(bad_metadata=True)
            second = rt.ensure_browser(directory, session=next_session)
            self.assertEqual(second, runtime)
            self.assertEqual(next_session.calls, [])
            self.assertEqual(json.loads((Path(directory)/'current.json').read_text())['version'], VERSION)

    def test_explicit_refresh_rechecks_remote_but_does_not_redownload_same_version(self):
        with tempfile.TemporaryDirectory() as directory:
            rt.ensure_browser(directory, session=FakeSession())
            session = FakeSession()
            rt.ensure_browser(directory, force_refresh=True, session=session)
            self.assertEqual(session.calls, [rt.METADATA_URL])

    def test_failed_update_uses_verified_existing_install(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = rt.ensure_browser(directory, session=FakeSession())
            notices = []
            recovered = rt.ensure_browser(directory, force_refresh=True,
                                         session=FakeSession(bad_metadata=True),
                                         logger=notices.append)
            self.assertEqual(existing, recovered)
            self.assertIn('using verified cached', notices[0])

    def test_force_reinstall_rebuilds_browser_and_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            old = rt.ensure_browser(directory, session=FakeSession())
            old.chrome.write_text('broken browser')
            fake = FakeSession()
            new = rt.ensure_browser(directory, force_refresh=True, force_reinstall=True,
                                    session=fake)
            self.assertTrue(rt.valid_runtime(new))
            self.assertEqual(len(fake.calls), 3)

    def test_uc_driver_copy_keeps_pristine_official_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = rt.ensure_browser(directory, session=FakeSession())
            driver = rt.prepare_uc_driver(browser, directory)
            self.assertNotEqual(browser.driver, driver)
            self.assertEqual(rt.read_binary_version(driver), VERSION)
            self.assertEqual(browser.driver.read_bytes(), driver.read_bytes())
            self.assertEqual(driver, rt.prepare_uc_driver(browser, directory))

    def test_release_url_must_match_official_google_host_and_version(self):
        fake = FakeSession()
        fake.metadata['channels']['Stable']['downloads']['chrome'][0]['url'] = 'https://evil.example/chrome.zip'
        with self.assertRaisesRegex(ValueError, 'Unexpected Chrome download'):
            rt._fetch_release_metadata(fake)

    def test_path_traversal_zip_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            blob = io.BytesIO()
            with zipfile.ZipFile(blob, mode='w') as archive:
                archive.writestr('chrome-linux64/../../escape', b'bad')
            archive_path = Path(directory)/'bad.zip'
            archive_path.write_bytes(blob.getvalue())
            with self.assertRaisesRegex(ValueError, 'Unsafe Chrome archive'):
                rt._extract_zip_safe(archive_path, Path(directory), 'chrome-linux64', 10000)
            self.assertFalse((Path(directory)/'escape').exists())

    def test_driver_launch_supplies_matching_version_and_managed_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = rt.ensure_browser(directory, session=FakeSession())
            patched = rt.prepare_uc_driver(runtime, directory)
            startup = []
            class FakeChromeOptions:
                def __init__(self):
                    self.args = []
                    self.page_load_strategy = None
                def add_argument(self, arg):
                    self.args.append(arg)
            class FakeChrome:
                def __init__(self, **kwargs):
                    startup.append(kwargs)
                def set_page_load_timeout(self, seconds):
                    pass
                def set_script_timeout(self, seconds):
                    pass
                def quit(self):
                    pass
            fake_uc = types.SimpleNamespace(ChromeOptions=FakeChromeOptions, Chrome=FakeChrome)
            args = types.SimpleNamespace(
                chrome_mode='managed', chrome_cache_dir=directory,
                chrome_update_days=7, chrome_refresh=False,
                chrome_binary='/usr/bin/google-chrome',
            )
            with mock.patch.dict(sys.modules, {'undetected_chromedriver': fake_uc}):
                browser, profile = cm.create_browser_driver(args)
                cm.close_browser(browser, profile)
            self.assertEqual(len(startup), 1)
            self.assertEqual(startup[0]['browser_executable_path'], str(runtime.chrome))
            self.assertEqual(startup[0]['driver_executable_path'], str(patched))
            self.assertEqual(startup[0]['version_main'], 157)
            self.assertEqual(startup[0]['no_sandbox'], False)
            self.assertNotIn('--no-sandbox', startup[0]['options'].args)

    def test_mismatch_repairs_cached_pair_and_retries_once(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = rt.ensure_browser(directory, session=FakeSession())
            attempts = []
            class FakeChromeOptions:
                def add_argument(self, arg):
                    pass
                page_load_strategy = 'eager'
            class FakeChrome:
                def __init__(self, **kwargs):
                    attempts.append(kwargs)
                    if len(attempts) <= 2:
                        raise RuntimeError('session not created: This version of ChromeDriver only supports Chrome version 156. Current browser version is 157.0.8103.38')
                def set_page_load_timeout(self, seconds): pass
                def set_script_timeout(self, seconds): pass
                def quit(self): pass
            fake_uc = types.SimpleNamespace(ChromeOptions=FakeChromeOptions, Chrome=FakeChrome)
            args = types.SimpleNamespace(
                chrome_mode='managed', chrome_cache_dir=directory,
                chrome_update_days=7, chrome_refresh=False,
                chrome_binary='/usr/bin/google-chrome',
            )
            real_ensure = rt.ensure_browser
            with mock.patch.dict(sys.modules, {'undetected_chromedriver': fake_uc}),\
                 mock.patch.object(rt, 'ensure_browser', wraps=real_ensure) as ensured,\
                 mock.patch.object(rt, '_fetch_release_metadata', return_value=(VERSION, {
                     'chrome': BASE + 'chrome-linux64.zip',
                     'chromedriver': BASE + 'chromedriver-linux64.zip'})),\
                 mock.patch.object(rt, '_install', return_value=runtime):
                browser, profile = cm.create_browser_driver(args)
                cm.close_browser(browser, profile)
            self.assertEqual(len(attempts), 3)
            self.assertTrue(any(x.kwargs.get('force_reinstall') is True for x in ensured.call_args_list))


# Additional regression test: the first mismatch should heal offline from a
# pristine local driver without refreshing the official download.
class DriverOfflineRecoveryTests(unittest.TestCase):
    def test_single_mismatch_repairs_using_local_copy_only(self):
        with tempfile.TemporaryDirectory() as directory:
            rt.ensure_browser(directory, session=FakeSession())
            attempts = []
            class FakeChromeOptions:
                def add_argument(self, arg): pass
                page_load_strategy = 'eager'
            class FakeChrome:
                def __init__(self, **kwargs):
                    attempts.append(kwargs)
                    if len(attempts) == 1:
                        raise RuntimeError('This version of ChromeDriver only supports Chrome version 156')
                def set_page_load_timeout(self, seconds): pass
                def set_script_timeout(self, seconds): pass
                def quit(self): pass
            args = types.SimpleNamespace(chrome_mode='managed', chrome_cache_dir=directory,
                chrome_update_days=7, chrome_refresh=False,
                chrome_binary='/usr/bin/google-chrome')
            fake_uc = types.SimpleNamespace(ChromeOptions=FakeChromeOptions, Chrome=FakeChrome)
            with mock.patch.dict(sys.modules, {'undetected_chromedriver': fake_uc}),\
                 mock.patch.object(rt, '_fetch_release_metadata', side_effect=RuntimeError('Offline')):
                browser, profile = cm.create_browser_driver(args)
                cm.close_browser(browser, profile)
            self.assertEqual(len(attempts), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
