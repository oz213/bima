"""Bounded browser read recovery and high-precision anti-bot page recognition.

Keep all gambling decisions in crawler_url.py. This module only decides whether
an observed page looks like a challenge and whether a blocked *read* can be
retried once after dismissing a page-owned JavaScript dialog.
"""

import re


# A challenge title is much stronger evidence than a generic word in a footer.
_CHALLENGE_TITLE = re.compile(
    r"^(?:just a moment[.!…]{0,3}|attention required!?\s*(?:\|\s*cloudflare)?|"
    r"verify (?:you are )?human|checking your browser(?:\.\.\.)?|"
    r"security verification|captcha(?: verification)?|one more step[.!]?)$",
    re.IGNORECASE,
)

# These phrases are deliberately more specific than the old substring list.
_CHALLENGE_BODY = (
    "verify you are human",
    "verify that you are human",
    "confirm you are human",
    "checking if the site connection is secure",
    "checking your browser before accessing",
    "performing security verification",
    "verifying you are not a bot",
    "human verification required",
    "please complete the captcha",
    "please solve the captcha",
    "please enable javascript and cookies to continue",
)


def is_likely_antibot_page(text, title=""):
    """Detect challenge pages, not articles merely mentioning anti-bot terms.

    Long, content-rich pages are not challenge pages solely because their
    footer/article contains 'cloudflare', 'captcha', or 'waiting for'.
    Uncertain content continues through the *existing* IndoBERT/ML engine;
    this function never labels a URL safe or triggers an alert.
    """
    heading = " ".join(str(title or "").casefold().split()).strip()
    if _CHALLENGE_TITLE.fullmatch(heading):
        return True

    body = " ".join(str(text or "").casefold().split())
    if not body or len(body) > 1400:
        return False

    if any(phrase in body for phrase in _CHALLENGE_BODY):
        return True
    if (any(phrase in body for phrase in ("cloudflare", "ray id", "cf-challenge"))
            and any(phrase in body for phrase in (
                "access denied", "browser check", "checking your browser",
                "please wait while we check", "security verification",
            ))):
        return True
    return False


def is_unexpected_dialog(exc):
    """Accept only a WebDriver unexpected-alert failure, never other errors."""
    name = type(exc).__name__
    if name == "UnexpectedAlertPresentException":
        return True
    return name == "WebDriverException" and "unexpected alert open" in str(exc).lower()


def dismiss_unexpected_dialog(driver, exc):
    """Dismiss (not accept) a page dialog once. Never read/log dialog text."""
    if not is_unexpected_dialog(exc):
        return False
    try:
        driver.switch_to.alert.dismiss()
        return True
    except Exception as error:
        # Chrome may have already dismissed the alert while notifying the
        # caller. Reading again is safe; never retry a mutating operation.
        return type(error).__name__ == "NoAlertPresentException"


def retry_observation_after_dialog(driver, read):
    """Retry a *read-only* browser observation at most once, never navigation.

    Only caller-supplied reads such as title, page_source and current_url
    belong here. Do not wrap form submissions, link clicks, alert POSTs or
    Selenium navigation in this helper.
    """
    try:
        return read()
    except Exception as exc:
        if not dismiss_unexpected_dialog(driver, exc):
            raise
        return read()
