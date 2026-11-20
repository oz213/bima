import os
import re
import json
import time
import io
import base64
import shutil
import tempfile
import requests
import joblib

from datetime import datetime, timezone
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont
from bs4 import BeautifulSoup

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import undetected_chromedriver as uc

from selenium.common.exceptions import (
    WebDriverException,
    TimeoutException,
)

try:
    import resource
except ImportError:
    resource = None


# =========================
# Path
# =========================
BASE_DIR = "/home/fauzi/crawler"
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DOMAINS_PATH = os.path.join(BASE_DIR, "domains.json")
MODEL_PATH = os.path.join(BASE_DIR, "gambling_classifier.pkl")


# =========================
# Resource Limit
# =========================
def setup_resource_limit():
    if resource is None:
        return

    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)

        if hard == resource.RLIM_INFINITY:
            new_soft = 65535
        else:
            new_soft = min(65535, hard)

        resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
        print(f"[INFO] RLIMIT_NOFILE set to soft={new_soft}, hard={hard}", flush=True)

    except Exception as e:
        print(f"[WARN] Failed to set resource limit: {e}", flush=True)


setup_resource_limit()


# =========================
# Logger
# =========================
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message):
    print(f"[{now_str()}] {message}", flush=True)


# =========================
# Helper
# =========================
def normalize_space(text):
    if not isinstance(text, str):
        return ""

    return re.sub(r"\s+", " ", text).strip()


def parse_bool(value, default=False):
    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        value = value.strip().lower()
        if value in ["true", "1", "yes", "y"]:
            return True
        if value in ["false", "0", "no", "n"]:
            return False

    return default


def resolve_env_value(value, default=None):
    """
    Support config value:
    "X_API_KEY": "${IDADX_X_API_KEY}"
    """
    if value is None:
        return default

    if isinstance(value, str):
        value = value.strip()

        if value.startswith("${") and value.endswith("}"):
            env_name = value[2:-1].strip()
            return os.getenv(env_name, default)

    return value


def separator():
    try:
        return "-" * os.get_terminal_size().columns
    except Exception:
        return "-" * 80


# =========================
# Load config
# =========================
with open(CONFIG_PATH, "r", encoding="utf-8") as config_file:
    config = json.load(config_file)

with open(DOMAINS_PATH, "r", encoding="utf-8") as domains_file:
    domains = json.load(domains_file)["domains"]


# =========================
# Load model
# Support:
# 1. Model lama: tuple(vectorizer, model)
# 2. Model baru: sklearn Pipeline
# =========================
loaded_model = joblib.load(MODEL_PATH)

if isinstance(loaded_model, tuple) and len(loaded_model) == 2:
    vectorizer, model = loaded_model
    classifier_pipeline = None
    log("[INFO] Loaded legacy model format: vectorizer + model")
else:
    vectorizer = None
    model = None
    classifier_pipeline = loaded_model
    log("[INFO] Loaded pipeline model format")


# =========================
# Config values
# =========================
GOOGLE_API_KEY = resolve_env_value(config.get("GOOGLE_API_KEY"))
GOOGLE_CX = resolve_env_value(config.get("GOOGLE_CX"))

X_API_KEY = resolve_env_value(config.get("X_API_KEY"))
ENDPOINT_URL = resolve_env_value(config.get("ENDPOINT_URL"))

FONT_PATH = resolve_env_value(config.get("FONT_PATH", "arial.ttf"))
FONT_SIZE = int(config.get("FONT_SIZE", 10))

QUERY_KEYWORDS = config.get("QUERY_KEYWORDS", [])

GOOGLE_NUM_RESULTS = int(config.get("GOOGLE_NUM_RESULTS", 50))
GOOGLE_RESULTS_PER_PAGE = int(config.get("GOOGLE_RESULTS_PER_PAGE", 10))
GOOGLE_SLEEP_SECONDS = float(config.get("GOOGLE_SLEEP_SECONDS", 1))

PAGE_LOAD_TIMEOUT = int(config.get("PAGE_LOAD_TIMEOUT", 35))
PAGE_SLEEP_SECONDS = float(config.get("PAGE_SLEEP_SECONDS", 5))

ML_REVIEW_THRESHOLD = float(config.get("ML_REVIEW_THRESHOLD", 0.60))
ML_THRESHOLD = float(config.get("ML_THRESHOLD", 0.75))
ML_STRONG_THRESHOLD = float(config.get("ML_STRONG_THRESHOLD", 0.90))

SEND_NEEDS_REVIEW = parse_bool(config.get("SEND_NEEDS_REVIEW", False), default=False)
INCLUDE_HOMEPAGE = parse_bool(config.get("INCLUDE_HOMEPAGE", True), default=True)


# =========================
# Keyword Context
# =========================
DEFAULT_STRONG_GAMBLING_KEYWORDS = [
    "cuan303",
    "daftar cuan303",
    "login cuan303",
    "live chat cuan303",
    "link alternatif cuan303",
    "rtp cuan303",

    "judi online",
    "situs judi",
    "agen judi",
    "permainan judi",

    "casino",
    "casino online",
    "online casino",
    "live casino",

    "slot online",
    "slot gacor",
    "situs slot",
    "agen slot",
    "rtp slot",
    "demo slot",
    "slot deposit",
    "slot qris",
    "slot dana",
    "slot pulsa",
    "slot88",
    "slot 88",

    "togel",
    "togel online",
    "bandar togel",
    "agen togel",
    "togel 4d",
    "togel4d",

    "taruhan bola",
    "taruhan online",
    "sportsbook",
    "sports betting",
    "live betting",

    "poker online",
    "blackjack",
    "baccarat",
    "roulette",
    "sic bo",
    "live dealer",

    "jackpot",
    "maxwin",
    "max win",
    "bonus new member",
    "bonus deposit",
    "deposit qris",
    "deposit dana",
    "deposit ovo",
    "deposit gopay",
    "withdraw",
    "withdrawal",
    "cashback",
    "wager",

    "pragmatic play",
    "pg soft",
    "habanero",
    "spadegaming",
    "microgaming",
    "joker gaming",
    "mahjong ways",
    "gates of olympus",
    "sweet bonanza",
    "starlight princess",
    "fortune tiger",
]

DEFAULT_SAFE_CONTEXT_KEYWORDS = [
    "slot kelompok sapi",
    "slot qurban",
    "slot kurban",
    "slot pendaftaran",
    "slot booking",
    "slot jadwal",
    "slot tersedia",
    "slot peserta",
    "slot wawancara",
    "slot konsultasi",
    "slot parkir",
    "slot waktu",
    "slot kelas",
    "slot pelatihan",

    "kuota qurban",
    "kuota kurban",
    "pendaftaran qurban",
    "pendaftaran kurban",
    "hewan qurban",
    "hewan kurban",

    "masjid",
    "mushola",
    "musala",
    "jadwal sholat",
    "jadwal salat",
    "pengajian",
    "jamaah",
    "ustadz",
    "ustaz",
    "idul adha",
    "sapi",
    "kambing",
    "domba",
    "zakat",
    "infaq",
    "infak",
    "sedekah",

    "sekolah",
    "kampus",
    "universitas",
    "desa",
    "kelurahan",
    "pemerintah",
    "yayasan",
    "organisasi",
    "pendaftaran peserta",
    "jadwal kegiatan",
    "layanan masyarakat",
    "pelayanan administrasi",
]

STRONG_GAMBLING_KEYWORDS = config.get(
    "STRONG_GAMBLING_KEYWORDS",
    DEFAULT_STRONG_GAMBLING_KEYWORDS,
)

SAFE_CONTEXT_KEYWORDS = config.get(
    "SAFE_CONTEXT_KEYWORDS",
    DEFAULT_SAFE_CONTEXT_KEYWORDS,
)


# =========================
# HTTP Session
# =========================
def requests_session():
    session = requests.Session()

    retries = Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retries,
        pool_connections=10,
        pool_maxsize=10,
    )

    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


session_http = requests_session()


# =========================
# Google CSE
# =========================
def normalize_domain(domain):
    domain = str(domain).strip()
    domain = domain.replace("https://", "").replace("http://", "")
    domain = domain.strip("/")
    return domain


def build_google_query(domain, query_keywords):
    domain = normalize_domain(domain)

    keywords = [
        str(k).strip()
        for k in query_keywords
        if str(k).strip()
    ]

    if not keywords:
        return f"site:{domain}"

    # Lebih fleksibel daripada "judi casino poker slot" yang terlalu strict.
    keyword_query = " OR ".join(keywords)
    return f"site:{domain} ({keyword_query})"


def generate_urls_google(domain, query_keywords, num_results=50):
    urls = []
    seen = set()

    domain = normalize_domain(domain)

    if INCLUDE_HOMEPAGE:
        for home_url in [
            f"https://{domain}",
            f"http://{domain}",
            f"https://www.{domain}",
            f"http://www.{domain}",
        ]:
            if home_url not in seen:
                urls.append(home_url)
                seen.add(home_url)

    if not GOOGLE_API_KEY or not GOOGLE_CX:
        log("[WARN] GOOGLE_API_KEY or GOOGLE_CX is empty. Google search skipped.")
        return urls

    query = build_google_query(domain, query_keywords)

    results_per_page = min(max(GOOGLE_RESULTS_PER_PAGE, 1), 10)
    max_results = min(num_results, 90)

    for start in range(1, max_results + 1, results_per_page):
        api_url = "https://www.googleapis.com/customsearch/v1"

        params = {
            "q": query,
            "key": GOOGLE_API_KEY,
            "cx": GOOGLE_CX,
            "start": start,
            "num": results_per_page,
        }

        try:
            response = session_http.get(
                api_url,
                params=params,
                timeout=(5, 30),
            )

            response.raise_for_status()
            data = response.json()

            items = data.get("items", [])

            for item in items:
                link = item.get("link")

                if link and link not in seen:
                    urls.append(link)
                    seen.add(link)

        except requests.RequestException as e:
            log(f"[ERROR] Google CSE error for {domain}: {e}")
            break

        time.sleep(GOOGLE_SLEEP_SECONDS)

    return urls[:num_results]


# =========================
# Selenium
# =========================
def make_driver():
    profile_dir = tempfile.mkdtemp(prefix="crawler-chrome-")

    options = uc.ChromeOptions()
    options.binary_location = "/usr/bin/google-chrome"

    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--window-size=1366,2000")
    options.add_argument(f"--user-data-dir={profile_dir}")

    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-translate")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-client-side-phishing-detection")
    options.add_argument("--disable-hang-monitor")
    options.add_argument("--disable-prompt-on-repost")
    options.add_argument("--disable-ipc-flooding-protection")
    options.add_argument("--mute-audio")

    # Tetap aktifkan gambar karena butuh screenshot.
    options.add_argument("--blink-settings=imagesEnabled=true")

    driver = uc.Chrome(
        options=options,
        use_subprocess=True,
    )

    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    driver.set_script_timeout(20)

    driver._crawler_profile_dir = profile_dir

    return driver


def safe_quit(driver):
    profile_dir = None

    try:
        if driver:
            profile_dir = getattr(driver, "_crawler_profile_dir", None)

            try:
                driver.quit()
            except Exception:
                pass

    finally:
        if profile_dir:
            shutil.rmtree(profile_dir, ignore_errors=True)


def stop_page_loading(driver):
    try:
        driver.execute_script("window.stop();")
    except Exception:
        pass


# =========================
# Page Text Extraction
# =========================
def split_domain_tokens(url):
    """
    Membantu domain seperti:
    b200mslot.id -> b200mslot id slot
    slotgacor88.id -> slotgacor88 id slot gacor 88
    """
    try:
        hostname = urlparse(url).hostname or ""
        hostname = hostname.lower()

        raw = hostname.replace(".", " ").replace("-", " ").replace("_", " ")

        extra = []

        fragments = [
            "slot",
            "gacor",
            "casino",
            "togel",
            "bet",
            "poker",
            "baccarat",
            "roulette",
            "jackpot",
            "maxwin",
            "777",
            "88",
        ]

        for frag in fragments:
            if frag in hostname:
                extra.append(frag)

        return normalize_space(" ".join([raw] + extra))

    except Exception:
        return ""


def get_meta_content(soup, name=None, prop=None):
    try:
        tag = None

        if name:
            tag = soup.find("meta", attrs={"name": name})

        if not tag and prop:
            tag = soup.find("meta", attrs={"property": prop})

        if tag and tag.get("content"):
            return tag.get("content", "")

    except Exception:
        pass

    return ""


def get_joined_text(elements):
    texts = []

    for el in elements:
        try:
            txt = el.get_text(" ", strip=True)
            if txt:
                texts.append(txt)
        except Exception:
            pass

    return " ".join(texts)


def get_page_text(driver, url=None):
    html = driver.page_source
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    meta_description = get_meta_content(soup, name="description")
    meta_keywords = get_meta_content(soup, name="keywords")
    og_title = get_meta_content(soup, prop="og:title")
    og_description = get_meta_content(soup, prop="og:description")

    headings = get_joined_text(soup.find_all(["h1", "h2", "h3"]))
    anchors = get_joined_text(soup.find_all("a"))
    buttons = get_joined_text(soup.find_all("button"))

    image_alts = " ".join(
        img.get("alt", "")
        for img in soup.find_all("img")
        if img.get("alt")
    )

    body_text = soup.get_text(separator=" ", strip=True)

    url_text = url or ""
    domain_text = split_domain_tokens(url) if url else ""

    combined = f"""
    URL: {url_text}
    DOMAIN: {domain_text}
    TITLE: {title}
    META_DESCRIPTION: {meta_description}
    META_KEYWORDS: {meta_keywords}
    OG_TITLE: {og_title}
    OG_DESCRIPTION: {og_description}
    HEADINGS: {headings}
    ANCHORS: {anchors}
    BUTTONS: {buttons}
    IMAGE_ALTS: {image_alts}
    BODY: {body_text}
    """

    return normalize_space(combined)


# =========================
# Keyword Signal
# =========================
def find_phrase_hits(text, phrases):
    lower = text.lower()
    hits = []

    for phrase in phrases:
        phrase = str(phrase).strip().lower()

        if not phrase:
            continue

        if phrase in lower:
            hits.append(phrase)

    return hits


def keyword_signal(text):
    strong_hits = find_phrase_hits(text, STRONG_GAMBLING_KEYWORDS)
    safe_hits = find_phrase_hits(text, SAFE_CONTEXT_KEYWORDS)

    return {
        "strong_hits": strong_hits,
        "safe_hits": safe_hits,
        "strong_count": len(strong_hits),
        "safe_count": len(safe_hits),
    }


# =========================
# ML Detection
# =========================
def is_gambling_content_ml(text, threshold=0.75):
    """
    Return:
    - ml_hit: bool
    - confidence: float
    - pred: label
    """
    try:
        # Model baru: Pipeline
        if classifier_pipeline is not None:
            clf = classifier_pipeline

            if hasattr(clf, "predict_proba"):
                proba = clf.predict_proba([text])[0]
                classes = list(clf.classes_)

                if "gambling" in classes:
                    gambling_index = classes.index("gambling")
                    confidence = float(proba[gambling_index])
                else:
                    confidence = float(max(proba))

                pred = clf.predict([text])[0]

                return confidence >= threshold, confidence, pred

            pred = clf.predict([text])[0]
            confidence = 1.0 if pred == "gambling" else 0.0

            return pred == "gambling", confidence, pred

        # Model lama: vectorizer + model
        X = vectorizer.transform([text])

        if hasattr(model, "predict_proba"):
            proba = model.predict_proba(X)[0]
            classes = list(model.classes_)

            if "gambling" in classes:
                gambling_index = classes.index("gambling")
                confidence = float(proba[gambling_index])
            else:
                confidence = float(max(proba))

            pred = model.predict(X)[0]

            return confidence >= threshold, confidence, pred

        pred = model.predict(X)[0]

        if pred == "gambling":
            return True, 1.0, pred

        return False, 0.0, pred

    except Exception as e:
        log(f"[WARN] ML detection fail: {e}")
        return False, 0.0, "unknown"


# =========================
# Decision Logic
# =========================
def decide_gambling_status(text, ml_conf, ml_pred):
    signal = keyword_signal(text)

    strong_hits = signal["strong_hits"]
    safe_hits = signal["safe_hits"]

    # Safe context kuat:
    # contoh: slot kelompok sapi, masjid, qurban.
    if safe_hits and not strong_hits and ml_conf < ML_STRONG_THRESHOLD:
        return {
            "status": "low_risk",
            "should_send": False,
            "reason": "safe_context_detected",
            "strong_hits": strong_hits,
            "safe_hits": safe_hits,
        }

    # ML sangat yakin gambling.
    if ml_pred == "gambling" and ml_conf >= ML_STRONG_THRESHOLD:
        return {
            "status": "high_risk",
            "should_send": True,
            "reason": "ml_strong_confidence",
            "strong_hits": strong_hits,
            "safe_hits": safe_hits,
        }

    # ML cukup yakin + ada minimal 1 sinyal gambling kuat.
    if ml_conf >= ML_THRESHOLD and len(strong_hits) >= 1:
        return {
            "status": "high_risk",
            "should_send": True,
            "reason": "ml_and_strong_keyword",
            "strong_hits": strong_hits,
            "safe_hits": safe_hits,
        }

    # Banyak keyword gambling kuat, berguna kalau halaman pendek.
    if len(strong_hits) >= 2 and not safe_hits:
        return {
            "status": "high_risk",
            "should_send": True,
            "reason": "multiple_strong_keywords",
            "strong_hits": strong_hits,
            "safe_hits": safe_hits,
        }

    # Campuran konteks aman dan gambling.
    # Jangan langsung kirim kecuali SEND_NEEDS_REVIEW true.
    if safe_hits and strong_hits and ml_conf < ML_STRONG_THRESHOLD:
        return {
            "status": "needs_review",
            "should_send": SEND_NEEDS_REVIEW,
            "reason": "mixed_safe_and_gambling_context",
            "strong_hits": strong_hits,
            "safe_hits": safe_hits,
        }

    # Medium risk.
    if ml_conf >= ML_REVIEW_THRESHOLD or len(strong_hits) == 1:
        return {
            "status": "needs_review",
            "should_send": SEND_NEEDS_REVIEW,
            "reason": "medium_risk",
            "strong_hits": strong_hits,
            "safe_hits": safe_hits,
        }

    return {
        "status": "low_risk",
        "should_send": False,
        "reason": "low_signal",
        "strong_hits": strong_hits,
        "safe_hits": safe_hits,
    }


# =========================
# Watermark
# =========================
def add_watermark(image, text1, text2):
    watermark_image = image.copy()
    draw = ImageDraw.Draw(watermark_image)

    try:
        font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    except Exception:
        log(f"[WARN] Failed to load font: {FONT_PATH}. Using default font.")
        font = ImageFont.load_default()

    text1_bbox = draw.textbbox((0, 0), text1, font=font)
    text1_height = text1_bbox[3] - text1_bbox[1]

    text2_bbox = draw.textbbox((0, 0), text2, font=font)
    text2_height = text2_bbox[3] - text2_bbox[1]

    box_height = text1_height + text2_height + 24

    rectangle_bbox = (
        0,
        watermark_image.height - box_height - 10,
        watermark_image.width,
        watermark_image.height,
    )

    draw.rectangle(rectangle_bbox, fill="white")

    draw.text(
        (10, watermark_image.height - box_height + 5),
        text1,
        font=font,
        fill="black",
    )

    draw.text(
        (10, watermark_image.height - text2_height - 10),
        text2,
        font=font,
        fill="black",
    )

    return watermark_image


# =========================
# Screenshot
# =========================
def take_screenshot_png(driver):
    try:
        driver.execute_script("window.scrollTo(0, 0)")

        result = driver.execute_cdp_cmd(
            "Page.captureScreenshot",
            {
                "fromSurface": True,
            },
        )

        return base64.b64decode(result["data"])

    except Exception as e:
        log(f"[WARN] CDP screenshot failed, fallback normal screenshot: {e}")
        return driver.get_screenshot_as_png()


# =========================
# Send Alert
# =========================
def send_alert(url, screenshot_png_b64, confidence):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    payload = {
        "url": url,
        "brand": "-",
        "discovered": timestamp,
        "confidence_level": int(round(confidence * 100)),
        "status": "0",
        "source": "crawling",
        "resolved": "0",
        "category": ["online gambling"],
        "screenshots": [
            f"data:image/png;base64,{screenshot_png_b64}"
        ],
    }

    headers = {
        "Content-Type": "application/json",
        "X-API-key": X_API_KEY,
    }

    try:
        response = session_http.post(
            ENDPOINT_URL,
            headers=headers,
            json=payload,
            timeout=(5, 30),
        )

        if response.status_code != 200:
            log(f"[ERROR] Failed to send data: {response.status_code} - {response.text[:300]}")
        else:
            log("[INFO] Alert sent OK")

    except requests.RequestException as e:
        log(f"[ERROR] Send request failed: {e}")


# =========================
# Process URL
# =========================
def process_url(url):
    driver = None

    try:
        driver = make_driver()

        log(f"[INFO] Opening URL: {url}")

        try:
            driver.get(url)
        except TimeoutException:
            log("[WARN] Page load timeout, stopping load and continuing with available HTML")
            stop_page_loading(driver)

        time.sleep(PAGE_SLEEP_SECONDS)

        page_text = get_page_text(driver, url=url)

        if not page_text or len(page_text) < 30:
            log("[INFO] Empty or very short page text, skip")
            return

        ml_hit, ml_conf, ml_pred = is_gambling_content_ml(
            page_text,
            threshold=ML_THRESHOLD,
        )

        decision = decide_gambling_status(page_text, ml_conf, ml_pred)

        log(
            "[DEBUG] "
            f"ml_pred={ml_pred}, "
            f"ml_hit={ml_hit}, "
            f"p={ml_conf:.2f}, "
            f"status={decision['status']}, "
            f"reason={decision['reason']}, "
            f"strong_hits={decision['strong_hits'][:10]}, "
            f"safe_hits={decision['safe_hits'][:10]}, "
            f"text_len={len(page_text)}"
        )

        if not decision["should_send"]:
            log(f"[INFO] No high risk gambling content in: {url}")
            return

        log(f"[ALERT] Gambling content found: {url}")

        screenshot = take_screenshot_png(driver)
        image = Image.open(io.BytesIO(screenshot))

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        watermark1 = f"Screenshot taken at {timestamp}"
        watermark2 = f"from BIMA: {url}"

        image_with_watermark = add_watermark(image, watermark1, watermark2)

        buffered = io.BytesIO()
        image_with_watermark.save(buffered, format="PNG")

        img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")

        image.close()
        image_with_watermark.close()
        buffered.close()

        send_alert(url, img_str, ml_conf)

    except WebDriverException as e:
        log(f"[ERROR] Selenium failed processing {url}: {e}")

    except Exception as e:
        log(f"[ERROR] Failed processing {url}: {e}")

    finally:
        if driver:
            safe_quit(driver)


# =========================
# Crawl Domain
# =========================
def crawl(domain):
    domain = normalize_domain(domain)

    log(f"[INFO] Crawling domain: {domain}")

    urls = generate_urls_google(
        domain,
        QUERY_KEYWORDS,
        num_results=GOOGLE_NUM_RESULTS,
    )

    if not urls:
        log(f"[INFO] No URLs found for {domain}")
        return

    log(f"[INFO] Found {len(urls)} candidate URLs for {domain}")

    for i, url in enumerate(urls, start=1):
        log(f"[INFO] Processing URL {i}/{len(urls)}: {url}")
        process_url(url)


# =========================
# Main
# =========================
if __name__ == "__main__":
    if not GOOGLE_API_KEY:
        log("[WARN] GOOGLE_API_KEY is empty")

    if not GOOGLE_CX:
        log("[WARN] GOOGLE_CX is empty")

    if not X_API_KEY:
        log("[WARN] X_API_KEY is empty")

    if not ENDPOINT_URL:
        log("[WARN] ENDPOINT_URL is empty")

    log(f"[INFO] Total domains: {len(domains)}")
    log(f"[INFO] ML_REVIEW_THRESHOLD={ML_REVIEW_THRESHOLD}")
    log(f"[INFO] ML_THRESHOLD={ML_THRESHOLD}")
    log(f"[INFO] ML_STRONG_THRESHOLD={ML_STRONG_THRESHOLD}")
    log(f"[INFO] SEND_NEEDS_REVIEW={SEND_NEEDS_REVIEW}")

    for domain in domains:
        crawl(domain)
        print(separator(), flush=True)
