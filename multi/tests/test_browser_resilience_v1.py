"""Regression tests for Selenium dialog recovery and CAPTCHA false positives."""
import unittest

from browser_resilience import (
    dismiss_unexpected_dialog,
    is_likely_antibot_page,
    retry_observation_after_dialog,
)


class UnexpectedAlertPresentException(Exception):
    pass


class WebDriverException(Exception):
    pass


class NoAlertPresentException(Exception):
    pass


class FakeAlert:
    def __init__(self):
        self.dismissals = 0

    def dismiss(self):
        self.dismissals += 1


class FakeDriver:
    def __init__(self):
        self.switch_to = type("Switch", (), {})()
        self.switch_to.alert = FakeAlert()


class ChallengeDetectionTests(unittest.TestCase):
    def test_cloudflare_challenge_title(self):
        self.assertTrue(is_likely_antibot_page("Checking your browser", "Just a moment..."))

    def test_cloudflare_attention_title(self):
        self.assertTrue(is_likely_antibot_page("", "Attention Required! | Cloudflare"))

    def test_explicit_human_verification(self):
        self.assertTrue(is_likely_antibot_page("Please verify you are human to proceed"))

    def test_unicode_ellipsis_challenge_title(self):
        self.assertTrue(is_likely_antibot_page("", "Just a moment…"))

    def test_long_article_quoting_challenge(self):
        article = ("An article explains why websites say verify you are human. " * 50)
        self.assertFalse(is_likely_antibot_page(article, "Security overview"))

    def test_cf_challenge_context(self):
        self.assertTrue(is_likely_antibot_page("Cloudflare Ray ID: abc. Access denied"))

    def test_long_article_with_captcha_keywords(self):
        text = ("Artikel ini membahas CAPTCHA dan Cloudflare. " * 100)
        self.assertFalse(is_likely_antibot_page(text, "Tutorial keamanan"))

    def test_cloudflare_footer(self):
        self.assertFalse(is_likely_antibot_page("Powered by Cloudflare", "Beranda kampus"))

    def test_waiting_for_not_blocked(self):
        self.assertFalse(is_likely_antibot_page("Waiting for student registration", "Pendaftaran"))

    def test_verification_successful_not_blocked(self):
        self.assertFalse(is_likely_antibot_page("Verification successful", "Survei"))

    def test_recapcha_mention_not_blocked(self):
        self.assertFalse(is_likely_antibot_page("A site can use reCAPTCHA", "FAQ"))

    def test_access_denied_alone_not_sufficient(self):
        self.assertFalse(is_likely_antibot_page("Access denied", "Status layanan"))

    def test_empty_page_not_challenge(self):
        self.assertFalse(is_likely_antibot_page("", ""))


class DialogRecoveryTests(unittest.TestCase):
    def test_single_dialog_is_dismissed_and_read_retried(self):
        driver = FakeDriver()
        calls = []

        def read():
            calls.append(1)
            if len(calls) == 1:
                raise UnexpectedAlertPresentException("sensitive remote dialog text")
            return "safe observed text"

        self.assertEqual(retry_observation_after_dialog(driver, read), "safe observed text")
        self.assertEqual(len(calls), 2)
        self.assertEqual(driver.switch_to.alert.dismissals, 1)

    def test_generic_webdriver_unexpected_alert_text(self):
        driver = FakeDriver()
        self.assertTrue(dismiss_unexpected_dialog(driver, WebDriverException("unexpected alert open")))
        self.assertEqual(driver.switch_to.alert.dismissals, 1)

    def test_unrelated_webdriver_error_not_dismissed(self):
        driver = FakeDriver()
        def read():
            raise WebDriverException("connection lost")
        with self.assertRaises(WebDriverException):
            retry_observation_after_dialog(driver, read)
        self.assertEqual(driver.switch_to.alert.dismissals, 0)

    def test_only_one_retry(self):
        driver = FakeDriver()
        calls = []
        def read():
            calls.append(1)
            raise UnexpectedAlertPresentException("repeat")
        with self.assertRaises(UnexpectedAlertPresentException):
            retry_observation_after_dialog(driver, read)
        self.assertEqual(len(calls), 2)
        self.assertEqual(driver.switch_to.alert.dismissals, 1)

    def test_auto_dismissed_dialog_retries_safe_read(self):
        driver = FakeDriver()

        class AutoDismiss:
            def dismiss(self):
                raise NoAlertPresentException()

        driver.switch_to.alert = AutoDismiss()
        calls = []
        def read():
            calls.append(1)
            if len(calls) == 1:
                raise UnexpectedAlertPresentException("browser auto dismissed")
            return "page"
        self.assertEqual(retry_observation_after_dialog(driver, read), "page")
        self.assertEqual(len(calls), 2)

    def test_no_dialog_available_keeps_original_error(self):
        driver = FakeDriver()
        driver.switch_to.alert = None
        def read():
            raise UnexpectedAlertPresentException("no dialog")
        with self.assertRaises(UnexpectedAlertPresentException):
            retry_observation_after_dialog(driver, read)

    def test_unexpected_python_value_error_never_dismissed(self):
        driver = FakeDriver()
        def read():
            raise ValueError("unexpected alert open")
        with self.assertRaises(ValueError):
            retry_observation_after_dialog(driver, read)
        self.assertEqual(driver.switch_to.alert.dismissals, 0)


if __name__ == "__main__":
    unittest.main()
