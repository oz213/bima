import os

import re

import json

import time

import io

import base64

import joblib

import requests

import tempfile

import shutil

import psutil

import gc

import socket

import signal



try:

    import resource

except ImportError:

    resource = None



from datetime import datetime, timezone

from urllib.parse import urlparse, urljoin, parse_qsl, urlencode, unquote



from PIL import Image, ImageDraw, ImageFont

from bs4 import BeautifulSoup



from requests.adapters import HTTPAdapter

from urllib3.util.retry import Retry



import undetected_chromedriver as uc



try:

    import torch

    from transformers import AutoTokenizer, AutoModelForSequenceClassification

except Exception:

    torch = None

    AutoTokenizer = None

    AutoModelForSequenceClassification = None



from selenium.webdriver.common.by import By

from selenium.webdriver.support.ui import WebDriverWait

from selenium.webdriver.support import expected_conditions as EC



from selenium.common.exceptions import (

    WebDriverException,

    TimeoutException,

)





# =========================

# Path

# =========================

BASE_DIR = os.environ.get("PANDI_CRAWLER_BASE_DIR", "/home/fauzi/crawler")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

URLS_PATH = os.path.join(BASE_DIR, "urls.json")

MODEL_PATH = os.path.join(BASE_DIR, "gambling_classifier.pkl")

PROGRESS_FILE = os.path.join(BASE_DIR, "progress.txt")





def parse_bool(value, default=False):

    """

    Parse boolean dari config JSON/string dengan aman.

    """

    if value is None:

        return default



    if isinstance(value, bool):

        return value



    if isinstance(value, (int, float)):

        return value != 0



    if isinstance(value, str):

        normalized = value.strip().lower()



        if normalized in ("1", "true", "yes", "y", "on"):

            return True



        if normalized in ("0", "false", "no", "n", "off"):

            return False



    return default





# =========================

# Load config & model

# =========================

with open(CONFIG_PATH, "r") as f:

    config = json.load(f)



with open(URLS_PATH, "r") as f:

    urls_all = json.load(f)["urls"]



vectorizer, model = joblib.load(MODEL_PATH)



X_API_KEY = config["X_API_KEY"]

ENDPOINT_URL = config["ENDPOINT_URL"]

FONT_PATH = config["FONT_PATH"]

FONT_SIZE = int(config["FONT_SIZE"])



RESTART_DRIVER_EVERY = int(config.get("RESTART_DRIVER_EVERY", 50))

RESTART_DRIVER_MAX_AGE_SECONDS = int(config.get("RESTART_DRIVER_MAX_AGE_SECONDS", 600))

MAX_CONSECUTIVE_RESTARTS = int(config.get("MAX_CONSECUTIVE_RESTARTS", 3))

HARD_RECOVERY_SLEEP_SECONDS = int(config.get("HARD_RECOVERY_SLEEP_SECONDS", 60))



ANTIBOT_MAX_WAIT_SECONDS = int(config.get("ANTIBOT_MAX_WAIT_SECONDS", 30))

ANTIBOT_WAIT_INTERVAL_SECONDS = int(config.get("ANTIBOT_WAIT_INTERVAL_SECONDS", 3))

ANTIBOT_MAX_STABLE_COUNT = int(config.get("ANTIBOT_MAX_STABLE_COUNT", 4))



DNS_CHECK_TIMEOUT_SECONDS = int(config.get("DNS_CHECK_TIMEOUT_SECONDS", 2))

ENABLE_DNS_PRECHECK = bool(config.get("ENABLE_DNS_PRECHECK", False))



ML_THRESHOLD = float(config.get("ML_THRESHOLD", 0.75))

ML_STRONG_THRESHOLD = float(config.get("ML_STRONG_THRESHOLD", 0.90))

REQUIRE_GAMBLING_CONTEXT_FOR_ML = bool(config.get("REQUIRE_GAMBLING_CONTEXT_FOR_ML", True))

MIN_ALERT_CONFIDENCE = float(config.get("MIN_ALERT_CONFIDENCE", 0.60))



ALERT_CONNECT_TIMEOUT = float(config.get("ALERT_CONNECT_TIMEOUT", 5))

ALERT_READ_TIMEOUT = float(config.get("ALERT_READ_TIMEOUT", 30))

ALERT_RAISE_ON_FAILURE = parse_bool(

    config.get("ALERT_RAISE_ON_FAILURE", False),

    default=False,

)



MAX_SECONDS_PER_URL = int(config.get("MAX_SECONDS_PER_URL", 60))



NON_ABUSE_CACHE_FILE = config.get(

    "NON_ABUSE_CACHE_FILE",

    os.path.join(BASE_DIR, "non_abuse_urls.jsonl"),

)

NON_ABUSE_CACHE_TTL_DAYS = int(config.get("NON_ABUSE_CACHE_TTL_DAYS", 7))

NON_ABUSE_URL_CACHE = {}



ENABLE_INDO_BERT = bool(config.get("ENABLE_INDO_BERT", False))

INDO_BERT_MODEL_PATH = config.get(

    "INDO_BERT_MODEL_PATH",

    os.path.join(BASE_DIR, "models", "indobert-judol"),

)

INDO_BERT_THRESHOLD = float(config.get("INDO_BERT_THRESHOLD", 0.80))

INDO_BERT_STRONG_THRESHOLD = float(config.get("INDO_BERT_STRONG_THRESHOLD", 0.92))

INDO_BERT_MAX_CHUNKS = int(config.get("INDO_BERT_MAX_CHUNKS", 8))

INDO_BERT_MAX_LENGTH = int(config.get("INDO_BERT_MAX_LENGTH", 256))

INDO_BERT_BATCH_SIZE = int(config.get("INDO_BERT_BATCH_SIZE", 4))



indobert_tokenizer = None

indobert_model = None

indobert_device = "cpu"





# =========================

# Logger

# =========================

def now_str():

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")





def log(message):

    print(f"[{now_str()}] {message}", flush=True)





def load_indobert_if_enabled():

    global ENABLE_INDO_BERT

    global indobert_tokenizer

    global indobert_model

    global indobert_device



    if not ENABLE_INDO_BERT:

        log("[INFO] IndoBERT disabled by config")

        return



    if torch is None or AutoTokenizer is None or AutoModelForSequenceClassification is None:

        ENABLE_INDO_BERT = False

        log("[WARN] IndoBERT dependencies are not available, disabled")

        return



    try:

        indobert_device = "cuda" if torch.cuda.is_available() else "cpu"

        indobert_tokenizer = AutoTokenizer.from_pretrained(INDO_BERT_MODEL_PATH)

        indobert_model = AutoModelForSequenceClassification.from_pretrained(INDO_BERT_MODEL_PATH)

        indobert_model.to(indobert_device)

        indobert_model.eval()



        log(f"[INFO] IndoBERT loaded from {INDO_BERT_MODEL_PATH} on {indobert_device}")



    except Exception as e:

        ENABLE_INDO_BERT = False

        indobert_tokenizer = None

        indobert_model = None

        log(f"[WARN] Failed to load IndoBERT, disabled: {e}")





load_indobert_if_enabled()







# =========================

# File Descriptor / HTTP Cleanup Helpers

# =========================

def setup_resource_limit():

    """

    Naikkan limit open files untuk crawler panjang.



    Error [Errno 24] Too many open files biasanya muncul setelah crawler

    berjalan lama karena FD limit kecil atau koneksi/handle belum dilepas OS.

    """

    if resource is None:

        return



    try:

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)



        if hard == resource.RLIM_INFINITY:

            new_soft = 1048576

        else:

            new_soft = min(max(soft, 65535), hard)



        if new_soft > soft:

            resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))

            log(f"[INFO] RLIMIT_NOFILE raised soft={new_soft}, hard={hard}")

        else:

            log(f"[INFO] RLIMIT_NOFILE soft={soft}, hard={hard}")



    except Exception as e:

        log(f"[WARN] Failed to set RLIMIT_NOFILE: {e}")





def get_open_fd_count():

    try:

        return len(os.listdir("/proc/self/fd"))

    except Exception:

        return -1





def get_fd_limit_text():

    if resource is None:

        return "unknown"



    try:

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)

        return f"{soft}/{hard}"

    except Exception:

        return "unknown"





def reset_http_session():

    """

    Tutup pool requests agar socket/file descriptor lama dilepas.

    """

    global session_http



    try:

        session_http.close()

    except Exception:

        pass



    session_http = requests_session()

    gc.collect()



setup_resource_limit()





# =========================

# Per URL Watchdog Timeout

# =========================

class UrlProcessingTimeout(Exception):

    pass





def _url_timeout_handler(signum, frame):

    raise UrlProcessingTimeout("URL processing timeout")





class url_timeout:

    def __init__(self, seconds):

        self.seconds = seconds

        self.old_handler = None



    def __enter__(self):

        self.old_handler = signal.signal(signal.SIGALRM, _url_timeout_handler)

        signal.alarm(self.seconds)



    def __exit__(self, exc_type, exc_val, exc_tb):

        signal.alarm(0)

        if self.old_handler is not None:

            signal.signal(signal.SIGALRM, self.old_handler)





# =========================

# Basic Helpers

# =========================

def escape_html(value):

    value = str(value or "")

    return (

        value

        .replace("&", "&amp;")

        .replace("<", "&lt;")

        .replace(">", "&gt;")

        .replace('"', "&quot;")

        .replace("'", "&#039;")

    )





def normalize_text_for_match(text):

    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()





def normalize_space(text):

    if not isinstance(text, str):

        return ""



    return re.sub(r"\s+", " ", text).strip()





def ensure_url_scheme(url, default_scheme="https"):

    """

    Pastikan URL punya scheme http/https.



    Contoh:

    - example.id          -> https://example.id

    - www.example.id/path -> https://www.example.id/path

    - //example.id/path   -> https://example.id/path

    """

    url = str(url or "").strip()



    if not url:

        return ""



    if url.startswith("//"):

        return f"{default_scheme}:{url}"



    parsed = urlparse(url)



    if parsed.scheme:

        return url



    return f"{default_scheme}://{url.lstrip('/')}"





def alternate_scheme_url(url):

    """

    Return URL dengan scheme kebalikan.



    Contoh:

    - http://example.id  -> https://example.id

    - https://example.id -> http://example.id

    - example.id         -> http://example.id, jika default awalnya https

    """

    url = ensure_url_scheme(url, default_scheme="https")



    if url.startswith("http://"):

        return "https://" + url[len("http://"):]



    if url.startswith("https://"):

        return "http://" + url[len("https://"):]



    return ""





def is_id_tld_host(hostname):

    """

    True kalau hostname berada di bawah TLD .id.

    Contoh valid:

    - example.id

    - example.co.id

    - sub.example.net.id

    """

    host = normalize_hostname_for_compare(hostname) if "normalize_hostname_for_compare" in globals() else (hostname or "").strip().lower()



    if not host:

        return False



    return host == "id" or host.endswith(".id")





def is_id_tld_url(url):

    try:

        parsed = urlparse(ensure_url_scheme(url, default_scheme="https"))

        return is_id_tld_host(parsed.hostname)

    except Exception:

        return False





def choose_alert_url_for_id_endpoint(original_url, current_url=None, final_url=None):

    """

    Endpoint alert hanya menerima URL dengan TLD .id.



    Prioritas:

    1. original_url dari urls.json atau hasil Google Search jika .id

    2. current_url jika masih .id

    3. final_url jika masih .id

    4. "" kalau tidak ada URL .id, artinya alert harus di-skip



    Contoh:

    input  http://denis-toto.id

    final  https://denistoto.vip/

    kirim  http://denis-toto.id

    """

    candidates = [

        original_url,

        current_url,

        final_url,

    ]



    for candidate in candidates:

        candidate = str(candidate or "").strip()



        if candidate and is_id_tld_url(candidate):

            return ensure_url_scheme(candidate, default_scheme="https")



    return ""











# =========================

# Non-Abuse URL Cache

# =========================

def normalize_url_cache_key(url):

    """

    Normalisasi URL untuk cache hasil non-abuse.



    Tujuan:

    - URL duplikat di urls.json atau hasil Google tidak diproses ulang.

    - http/https dianggap sama jika host/path/query sama.

    - fragment dibuang.

    - trailing slash diseragamkan.

    """

    try:

        parsed = urlparse(ensure_url_scheme(url, default_scheme="https"))

        host = normalize_hostname_for_compare(parsed.hostname)

        path = parsed.path or "/"



        if path != "/":

            path = path.rstrip("/")



        query = parsed.query or ""

        return normalize_space(f"{host}{path}?{query}".rstrip("?")).lower()



    except Exception:

        return normalize_space(str(url or "")).lower()





def parse_cache_time(value):

    try:

        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    except Exception:

        return None





def is_cache_record_fresh(record):

    if NON_ABUSE_CACHE_TTL_DAYS <= 0:

        return True



    checked_at = parse_cache_time(record.get("checked_at"))



    if checked_at is None:

        return False



    age_seconds = (datetime.now(timezone.utc) - checked_at).total_seconds()

    return age_seconds <= NON_ABUSE_CACHE_TTL_DAYS * 86400





def load_non_abuse_cache():

    """

    Load cache URL yang sudah dicek dan dinyatakan non-abuse.



    Cache ini tidak menyimpan URL yang gagal diakses, DNS error, timeout,

    CAPTCHA, atau empty text.

    """

    NON_ABUSE_URL_CACHE.clear()



    if not NON_ABUSE_CACHE_FILE or not os.path.exists(NON_ABUSE_CACHE_FILE):

        return



    loaded = 0

    expired = 0



    try:

        with open(NON_ABUSE_CACHE_FILE, "r", encoding="utf-8") as f:

            for line in f:

                line = line.strip()



                if not line:

                    continue



                try:

                    record = json.loads(line)

                except Exception:

                    continue



                key = record.get("key") or normalize_url_cache_key(record.get("url"))



                if not key:

                    continue



                if not is_cache_record_fresh(record):

                    expired += 1

                    continue



                NON_ABUSE_URL_CACHE[key] = record

                loaded += 1



        log(

            f"[INFO] Non-abuse cache loaded: active={loaded}, expired_ignored={expired}, "

            f"file={NON_ABUSE_CACHE_FILE}, ttl_days={NON_ABUSE_CACHE_TTL_DAYS}"

        )



    except Exception as e:

        log(f"[WARN] Failed to load non-abuse cache: {e}")





def get_non_abuse_cache_record(url):

    key = normalize_url_cache_key(url)



    if not key:

        return None



    record = NON_ABUSE_URL_CACHE.get(key)



    if not record:

        return None



    if not is_cache_record_fresh(record):

        NON_ABUSE_URL_CACHE.pop(key, None)

        return None



    return record





def save_non_abuse_checked_url(url, final_url=None, reason="", source=""):

    """

    Simpan URL sebagai sudah dicek dan non-abuse.



    Fungsi ini hanya boleh dipanggil setelah konten berhasil diakses

    dan classifier memutuskan low_risk. Jangan panggil untuk DNS error,

    timeout, blocked/CAPTCHA, empty text, atau URL access error.

    """

    candidates = []



    for candidate in [url, final_url]:

        candidate = str(candidate or "").strip()



        if candidate and candidate not in candidates:

            candidates.append(candidate)



    if not candidates:

        return



    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    records_to_write = []



    for candidate in candidates:

        key = normalize_url_cache_key(candidate)



        if not key:

            continue



        existing = NON_ABUSE_URL_CACHE.get(key)



        if existing and is_cache_record_fresh(existing):

            continue



        record = {

            "key": key,

            "url": candidate,

            "final_url": final_url or "",

            "reason": reason or "",

            "source": source or "",

            "checked_at": checked_at,

        }



        NON_ABUSE_URL_CACHE[key] = record

        records_to_write.append(record)



    if not records_to_write:

        return



    try:

        cache_dir = os.path.dirname(NON_ABUSE_CACHE_FILE)



        if cache_dir:

            os.makedirs(cache_dir, exist_ok=True)



        with open(NON_ABUSE_CACHE_FILE, "a", encoding="utf-8") as f:

            for record in records_to_write:

                f.write(json.dumps(record, ensure_ascii=False) + "\n")



        log(

            f"[INFO] Saved non-abuse checked URL cache: "

            f"count={len(records_to_write)}, reason={reason}, source={source}"

        )



    except Exception as e:

        log(f"[WARN] Failed to save non-abuse cache: {e}")



# =========================

# DNS Helpers

# =========================

def get_hostname(url):

    try:

        parsed = urlparse(url)

        host = parsed.netloc or parsed.path

        host = host.split("/")[0]

        host = host.split(":")[0]

        return host.strip().lower()

    except Exception:

        return ""





def is_dns_resolvable(url, timeout=2):

    host = get_hostname(url)



    if not host:

        return False



    old_timeout = socket.getdefaulttimeout()



    try:

        socket.setdefaulttimeout(timeout)

        socket.getaddrinfo(host, None)

        return True

    except socket.gaierror:

        return False

    except Exception as e:

        log(f"[WARN] DNS check error for {host}: {e}")

        return False

    finally:

        socket.setdefaulttimeout(old_timeout)





def is_dns_error_text(error):

    msg = str(error).lower()



    dns_keywords = [

        "name resolution",

        "failed to resolve",

        "no address associated with hostname",

        "name or service not known",

        "temporary failure in name resolution",

        "err_name_not_resolved",

        "nameresolutionerror",

    ]



    return any(k in msg for k in dns_keywords)





# =========================

# HTTP Session

# =========================

def requests_session():

    session = requests.Session()



    retries = Retry(

        total=1,

        connect=1,

        read=1,

        status=1,

        backoff_factor=0.3,

        status_forcelist=[429, 500, 502, 503, 504],

        allowed_methods=["GET", "POST"],

        raise_on_status=False,

    )



    adapter = HTTPAdapter(

        max_retries=retries,

        pool_connections=2,

        pool_maxsize=2,

        pool_block=False,

    )



    session.mount("http://", adapter)

    session.mount("https://", adapter)



    return session





session_http = requests_session()





# =========================

# Keywords

# =========================

def compile_keyword_regex(keywords):

    patterns = []



    for keyword in keywords:

        keyword = keyword.strip().lower()



        if not keyword:

            continue



        escaped = re.escape(keyword)

        escaped = escaped.replace(r"\ ", r"[\s\\-]+")

        patterns.append(escaped)



    if not patterns:

        return None



    return re.compile(r"(?i)(?:" + "|".join(patterns) + r")")





KEYWORD_RE = compile_keyword_regex(config["KEYWORDS"])





STRONG_TEXT_KEYWORDS = [

    "judi", "judol", "slot", "slots", "slot gacor", "rtp slot",

    "togel", "lottery", "number games", "casino", "live casino",

    "sportsbook", "sports betting", "betting", "bet", "wager",

    "wagering", "odds", "live dealer", "live-dealer", "baccarat",

    "roulette", "blackjack", "dragon tiger", "sic bo", "bandarq",

    "texas poker", "sbobet", "joker123", "aviator", "sweet bonanza",

    "gates of olympus", "fortune tiger", "mahjong ways", "mahjong wins",

    "deposit", "withdrawal", "withdraw", "bonus", "cashback", "jackpot",

    "rtp", "maxwin", "scatter", "scatter hitam", "scatter mahjong",

    "pragmatic", "pragmatic play", "pg soft", "pgsoft", "free spins",

    "free bets", "vip tier", "loyalty rewards", "playthrough",

    "link alternatif", "dana", "ovo", "gopay", "shopeepay", "qris",

]





def is_gambling_content_keywords(text):

    if not KEYWORD_RE:

        return False



    return KEYWORD_RE.search(text) is not None





def find_keyword_hits(text, keywords):

    normalized = normalize_text_for_match(text)

    hits = []



    for keyword in keywords:

        kw = normalize_text_for_match(keyword)



        if not kw:

            continue



        if len(kw) <= 3:

            pattern = r"\b" + re.escape(kw) + r"\b"

            if re.search(pattern, normalized):

                hits.append(keyword)

        else:

            if kw in normalized:

                hits.append(keyword)



    return list(dict.fromkeys(hits))





def has_contextual_gambling_phrase(page_text):

    text = normalize_text_for_match(page_text)



    phrases = [

        "slot gacor", "rtp slot", "scatter hitam", "scatter mahjong",

        "scatter pragmatic", "scatter pg soft", "free spins", "maxwin",

        "pragmatic play", "pg soft", "deposit slot", "bonus new member",

        "togel online", "casino online", "situs judi", "judi online",

        "judi bola", "apk slot", "slot depo", "slot deposit", "slot777",

        "sbobet", "joker123", "link alternatif judi", "daftar judi",

        "mahjong ways", "mahjong wins",

        "mahjongways kasino online",

        "mahjong ways kasino online",

        "kasino online",

        "rtp pgsoft",

        "rtp pg soft",

        "saldo dana gratis",

        "game online slot",

        "game online gacor",

        "game online kasino",

        "blackjack online",

        "mahjong ways 2",

        "mahjongways 2",

        "link slot online",

        "situs kasino online",

        "congtogel", "cong togel", "toto macau", "togel macau",

        "pengeluaran macau", "keluaran macau", "data macau",

        "result macau", "live draw macau", "pasaran togel",

        "pasaran macau", "link resmi login",

        "login dan daftar game online", "login & daftar game online",

        "daftar game online eksklusif", "toto 4d", "toto4d",

        "situs toto", "bandar toto",

    ]



    return any(normalize_text_for_match(p) in text for p in phrases)





# =========================

# Hidden Gambling Link Detection

# =========================



HIDDEN_GAMBLING_TERMS = [

    "slot",

    "togel",

    "casino",

    "judi",

    "judol",

    "gacor",

    "rtp",

    "mahjong ways",

    "mahjong wins",

    "gama69",

    "rajajago",

    "raja-jago",

    "virgobet",

    "virgobet88",

    "petimas88",

    "babe181",

    "dewata88",

    "kenzo688",

    "sbobet",

    "joker123",

    "lotre4d",

    "lotre",

    "bet88",

    "slot online",

    "situs slot",

    "pasaran togel",

    "rtp slot",

    "slot gacor",

    "casino online",

    "togel online",

    "situs judi",

    "judi online",

    "link alternatif judi",

    "mahjongways kasino online",

    "mahjong ways kasino online",

    "kasino online",

    "rtp pgsoft",

    "rtp pg soft",

    "saldo dana gratis",

    "game online slot",

    "game online gacor",

    "game online kasino",

    "blackjack online",

    "mahjong ways 2",

    "mahjongways 2",

    "link slot online",

    "situs kasino online",

]



# Keyword pendek/ambigu jangan dijadikan bukti tunggal.

# Catatan penting:

# - "rtp" bisa false positive dari "smartphone"

# - "gacor" bisa false positive dari brand/domain seperti "gacorgame.id"

# - "slot" bisa false positive dari UI booking/jadwal atau nama menu internal

AMBIGUOUS_HIDDEN_TERMS = {

    "rtp",

    "gacor",

    "slot",

    "bet",

    "bonus",

    "deposit",

    "withdraw",

    "withdrawal",

}



# Keyword/frasa yang kuat untuk hidden-link, terutama jika link outbound.

STRONG_HIDDEN_TERMS = {

    "togel",

    "casino",

    "judi",

    "judol",

    "mahjong ways",

    "mahjong wins",

    "sbobet",

    "joker123",

    "gama69",

    "rajajago",

    "raja-jago",

    "virgobet",

    "virgobet88",

    "petimas88",

    "babe181",

    "dewata88",

    "kenzo688",

    "lotre4d",

    "slot online",

    "situs slot",

    "pasaran togel",

    "rtp slot",

    "slot gacor",

    "casino online",

    "togel online",

    "situs judi",

    "judi online",

    "link alternatif judi",

    "mahjongways kasino online",

    "mahjong ways kasino online",

    "kasino online",

    "rtp pgsoft",

    "rtp pg soft",

    "saldo dana gratis",

    "game online slot",

    "game online gacor",

    "game online kasino",

    "blackjack online",

    "mahjong ways 2",

    "mahjongways 2",

    "link slot online",

    "situs kasino online",

}



# Untuk link internal/same-domain, syarat dibuat lebih ketat karena banyak

# website memakai mobile menu / dropdown display:none yang sebenarnya normal.

INTERNAL_ALLOWED_STRONG_TERMS = {

    "togel",

    "casino",

    "judi",

    "judol",

    "sbobet",

    "joker123",

    "slot online",

    "situs slot",

    "pasaran togel",

    "rtp slot",

    "slot gacor",

    "casino online",

    "togel online",

    "situs judi",

    "judi online",

    "link alternatif judi",

}



DOMAIN_DERIVED_IGNORE_TERMS = {

    "gacor",

    "slot",

    "bet",

    "rtp",

}



# Konteks aman untuk kata "slot" yang bukan judi.

# Contoh nyata:

# - "slot drills" pada toko industrial/cutting tools

# - "Slot-type Photomicrosensor" pada produk sensor industrial automation

SAFE_SLOT_TOOL_CONTEXT_TERMS = [

    "slot drill",

    "slot drills",

    "slot-drill",

    "slot-drills",

    "slot mill",

    "slot mills",

    "slot milling",

    "end mill",

    "end mills",

    "endmill",

    "milling",

    "milling cutter",

    "milling cutters",

    "cutting tool",

    "cutting tools",

    "drill bit",

    "drill bits",

    "cutter",

    "cutters",

    "carbide",

    "hss",

    "hss-e",

    "high speed steel",

    "cobalt high speed steel",

    "flute",

    "2 flute",

    "3 flute",

    "4 flute",

    "shank",

    "plain shank",

    "weldon",

    "tialn",

    "ticn",

    "uncoated",

    "metalworking",

    "woodworking",

    "machining",

    "cromwell",



    # Safe context untuk produk sensor/industrial automation.

    # Contoh nyata: OMRON Slot-type Photomicrosensor.

    "slot type",

    "slot-type",

    "photomicrosensor",

    "photomicrosensors",

    "photomicro sensor",

    "photomicro sensors",

    "photo microsensor",

    "photo microsensors",

    "photoelectric sensor",

    "photoelectric sensors",

    "optical sensor",

    "optical sensors",

    "sensor",

    "sensors",

    "connector",

    "pre wired",

    "pre-wired",

    "non modulated",

    "non-modulated",

    "direct switching",

    "switching capacity",

    "instruction manual",

    "instruction sheet",

    "manual",

    "catalog",

    "cad",

    "rohs",

    "industrial automation",

    "automation",

    "control components",

    "safety components",

    "motion drives",

    "relays",

    "omron",

    "ee sx",

    "ee-sx",

    "sx47",

    "sx67",

    "sx95",

    "sx77",

    "sx87",

    "spx",

    "amplifier",

    "amplier",

]





def is_safe_slot_tool_context(text):

    normalized = normalize_text_for_match(text)



    if not normalized:

        return False



    # Wajib ada kata slot agar fungsi ini tidak terlalu luas.

    if not term_exists_as_token(normalized, "slot"):

        return False



    score = 0



    for term in SAFE_SLOT_TOOL_CONTEXT_TERMS:

        term_normalized = normalize_text_for_match(term)



        if not term_normalized:

            continue



        if term_normalized in normalized:

            score += 1



    # Minimal 2 agar "slot" + satu kata generik tidak langsung dianggap aman.

    return score >= 2





def normalize_term(value):

    return normalize_text_for_match(value)





def term_exists_as_token(text, term):

    """

    Match keyword sebagai token/frasa utuh, bukan substring.



    Contoh:

    - term "rtp" tidak cocok di "smartphone"

    - term "slot" cocok di "slot gacor"

    - term "mahjong ways" cocok sebagai frasa

    """

    normalized_text = normalize_term(text)

    normalized_term = normalize_term(term)



    if not normalized_text or not normalized_term:

        return False



    pattern = (

        r"(?<![a-z0-9])"

        + re.escape(normalized_term)

        + r"(?![a-z0-9])"

    )



    return re.search(pattern, normalized_text) is not None





def normalize_hostname_for_compare(hostname):

    hostname = (hostname or "").strip().lower().rstrip(".")



    if hostname.startswith("www."):

        hostname = hostname[4:]



    return hostname





def hostname_contains_term(hostname, term):

    """

    Cek apakah term berasal dari hostname/domain.

    Ini sengaja substring-based khusus hostname agar kasus seperti:

    - gacorgame.id mengandung gacor

    - slotgame.id mengandung slot

    bisa dianggap domain-derived, bukan bukti hidden judi.

    """

    hostname = normalize_hostname_for_compare(hostname)

    normalized_host = normalize_text_for_match(hostname.replace(".", " ").replace("-", " "))

    normalized_compact_host = re.sub(r"[^a-z0-9]+", "", hostname)

    normalized_term = normalize_term(term)

    compact_term = re.sub(r"[^a-z0-9]+", "", normalized_term)



    if not normalized_term or not compact_term:

        return False



    return (

        term_exists_as_token(normalized_host, normalized_term)

        or compact_term in normalized_compact_host

    )





REDIRECT_QUERY_PARAM_NAMES = {

    "next",

    "redirect",

    "redirect_url",

    "redirect_uri",

    "return",

    "return_url",

    "returnurl",

    "continue",

    "callback",

    "callback_url",

    "target",

    "destination",

    "dest",

    "url",

    "u",

}



AUTH_PATH_KEYWORDS = [

    "/auth/",

    "/login",

    "/signin",

    "/sign-in",

    "/account",

    "/oauth",

    "/sso",

]



AUTH_HOST_KEYWORDS = [

    "account.",

    "accounts.",

    "auth.",

    "login.",

    "sso.",

]





def is_probably_auth_or_login_link(parsed_url):

    host = normalize_hostname_for_compare(parsed_url.hostname)

    path = (parsed_url.path or "").lower()



    if any(keyword in path for keyword in AUTH_PATH_KEYWORDS):

        return True



    if any(keyword in host for keyword in AUTH_HOST_KEYWORDS):

        return True



    return False





def clean_query_for_hidden_detection(query, page_url=None):

    """

    Buang parameter redirect seperti next=, return_url=, redirect=, dst.



    Contoh false positive yang dicegah:

    beta.account.tirto.id/auth/login?next=https://tirto.id/...judi-slot...

    """

    if not query:

        return ""



    page_host = normalize_hostname_for_compare(urlparse(page_url or "").hostname)

    kept_params = []



    for key, value in parse_qsl(query, keep_blank_values=False):

        key_lower = key.lower().strip()

        value_raw = value or ""

        value_decoded = unquote(value_raw)

        value_host = normalize_hostname_for_compare(urlparse(value_decoded).hostname)



        if key_lower in REDIRECT_QUERY_PARAM_NAMES:

            continue



        if page_host and page_host in value_decoded.lower():

            continue



        if page_host and value_host and value_host == page_host:

            continue



        kept_params.append((key, value))



    return urlencode(kept_params)





def get_link_info_for_hidden_link(href, page_url=None):

    """

    Ambil konteks URL untuk hidden-link detection.



    Perbaikan:

    - Link internal/same-domain tidak memakai hostname sebagai sinyal.

    - Link outbound tetap memakai hostname.

    - Query redirect seperti next=, return_url=, redirect= dibuang.

    - Link auth/login tidak memakai query sebagai sinyal.

    """

    href = (href or "").strip()



    info = {

        "href": href,

        "resolved_url": href,

        "page_host": "",

        "link_host": "",

        "is_internal": False,

        "href_context": "",

    }



    if not href:

        return info



    href_lower = href.lower()



    if href_lower.startswith(("javascript:", "mailto:", "tel:", "#")):

        return info



    try:

        page_host = ""

        if page_url:

            page_host = normalize_hostname_for_compare(urlparse(page_url).hostname)



        resolved = urljoin(page_url or "", href)

        parsed = urlparse(resolved)

        link_host = normalize_hostname_for_compare(parsed.hostname)



        is_internal = False

        if page_host and link_host and page_host == link_host:

            is_internal = True

        elif not link_host:

            is_internal = True



        if is_probably_auth_or_login_link(parsed):

            cleaned_query = ""

        else:

            cleaned_query = clean_query_for_hidden_detection(

                parsed.query or "",

                page_url=page_url,

            )



        path_context = normalize_space(" ".join([

            parsed.path or "",

            cleaned_query,

            parsed.fragment or "",

        ]))



        if is_internal:

            href_context = path_context

        elif is_probably_auth_or_login_link(parsed):

            href_context = normalize_space(" ".join([

                link_host,

                parsed.path or "",

            ]))

        else:

            href_context = normalize_space(" ".join([

                link_host,

                path_context,

            ]))



        info.update({

            "resolved_url": resolved,

            "page_host": page_host,

            "link_host": link_host,

            "is_internal": is_internal,

            "href_context": href_context,

        })



        return info



    except Exception:

        info["href_context"] = href

        return info





def get_link_context_text(a, hidden_container, page_url=None):

    """

    Ambil konteks link secara lokal.



    Jangan memakai seluruh hidden_container.get_text() untuk setiap link,

    karena satu kata normal di container bisa membuat semua link ikut terdeteksi.

    """

    href = (a.get("href") or "").strip()

    anchor = a.get_text(" ", strip=True)

    title = (a.get("title") or "").strip()

    aria_label = (a.get("aria-label") or "").strip()



    link_info = get_link_info_for_hidden_link(href, page_url=page_url)



    parts = [

        link_info.get("href_context", ""),

        anchor,

        title,

        aria_label,

    ]



    # Tambahkan konteks parent terdekat jika pendek.

    try:

        parent = a.parent

        depth = 0



        while parent is not None and parent != hidden_container and depth < 3:

            parent_text = parent.get_text(" ", strip=True)



            if parent_text and len(parent_text) <= 300:

                parts.append(parent_text)

                break



            parent = parent.parent

            depth += 1



    except Exception:

        pass



    return normalize_space(" ".join(parts)), link_info





def find_hidden_gambling_term_hits(text):

    hits = []



    for term in HIDDEN_GAMBLING_TERMS:

        if term_exists_as_token(text, term):

            hits.append(term)



    return list(dict.fromkeys(hits))





def filter_hidden_gambling_hits(matched_terms, link_info, context_text=""):

    if not matched_terms:

        return []



    # Kata "slot" sering muncul dalam konteks non-judi seperti:

    # - slot drills

    # - slot mill cutters

    # - booking slot / time slot

    # Untuk konteks industrial/cutting tools, buang hit "slot" agar tidak alert.

    if is_safe_slot_tool_context(context_text):

        matched_terms = [

            term for term in matched_terms

            if normalize_term(term) != "slot"

        ]



        if not matched_terms:

            return []



    normalized_hits = {normalize_term(hit) for hit in matched_terms}

    ambiguous_hits = {normalize_term(term) for term in AMBIGUOUS_HIDDEN_TERMS}

    strong_hits = {normalize_term(term) for term in STRONG_HIDDEN_TERMS}

    internal_allowed_hits = {normalize_term(term) for term in INTERNAL_ALLOWED_STRONG_TERMS}

    domain_ignore_hits = {normalize_term(term) for term in DOMAIN_DERIVED_IGNORE_TERMS}



    # Kalau hanya keyword ambigu yang muncul, jangan anggap sebagai bukti.

    # Ini menutup false positive seperti:

    # - rtp dari smartphone

    # - gacor dari gacorgame.id / brand Gacor Game

    if normalized_hits and normalized_hits.issubset(ambiguous_hits):

        return []



    is_internal = bool(link_info.get("is_internal"))

    page_host = link_info.get("page_host", "")

    link_host = link_info.get("link_host", "")



    if is_internal:

        non_domain_hits = []



        for hit in matched_terms:

            normalized_hit = normalize_term(hit)



            is_domain_derived = (

                normalized_hit in domain_ignore_hits

                and (

                    hostname_contains_term(page_host, hit)

                    or hostname_contains_term(link_host, hit)

                )

            )



            if not is_domain_derived:

                non_domain_hits.append(hit)



        if not non_domain_hits:

            return []



        normalized_non_domain_hits = {

            normalize_term(hit)

            for hit in non_domain_hits

        }



        # Link internal hidden tidak boleh alert hanya karena 1 kata generik.

        # Wajib ada frasa/kata yang benar-benar kuat.

        if not normalized_non_domain_hits.intersection(internal_allowed_hits):

            return []



        return non_domain_hits



    # Outbound/external:

    # Harus punya minimal satu sinyal kuat, atau minimal dua sinyal non-ambigu.

    if normalized_hits.intersection(strong_hits):

        return matched_terms



    if len(normalized_hits - ambiguous_hits) >= 2:

        return matched_terms



    return []





def is_hidden_element(tag):

    style_raw = tag.get("style") or ""

    style = style_raw.lower().replace(" ", "")

    cls = " ".join(tag.get("class") or []).lower()

    tag_id = (tag.get("id") or "").lower()

    hidden_attr = tag.has_attr("hidden")



    return (

        hidden_attr

        or "display:none" in style

        or "visibility:hidden" in style

        or "opacity:0" in style

        or "height:0" in style

        or "width:0" in style

        or "font-size:0" in style

        or (

            "overflow:hidden" in style

            and (

                "height:0" in style

                or "width:0" in style

            )

        )

        or "position:absolute;left:-" in style

        or "position:absolute;top:-" in style

        or "hidden" in cls

        or "hidden" in tag_id

    )





def extract_hidden_gambling_links(html, page_url=None):

    soup = BeautifulSoup(html or "", "html.parser")



    evidence = []

    seen = set()



    for tag in soup.find_all(True):

        if not is_hidden_element(tag):

            continue



        style_raw = tag.get("style") or ""

        links = tag.find_all("a")



        for a in links:

            href = (a.get("href") or "").strip()

            anchor = a.get_text(" ", strip=True)



            combined, link_info = get_link_context_text(a, tag, page_url=page_url)

            raw_matched_terms = find_hidden_gambling_term_hits(combined)

            matched_terms = filter_hidden_gambling_hits(raw_matched_terms, link_info, combined)



            if not matched_terms:

                continue



            key = f"{href}|{anchor}|{','.join(matched_terms)}"



            if key in seen:

                continue



            seen.add(key)



            evidence.append({

                "href": href,

                "anchor": anchor,

                "matched_terms": matched_terms,

                "raw_matched_terms": raw_matched_terms,

                "hidden_style": style_raw[:300],

                "is_internal": link_info.get("is_internal", False),

                "page_host": link_info.get("page_host", ""),

                "link_host": link_info.get("link_host", ""),

                "context_sample": combined[:300],

            })



    return evidence





SAFE_NEWS_HIDDEN_TOPIC_TERMS = {

    "slot",

    "judi",

    "judol",

    "judi online",

    "judi slot",

    "cegah judi online",

    "motif judi slot",

}





def is_ojs_pdfjs_viewer_noise_url(url):

    """

    Deteksi URL noise khas OJS/PDF.js viewer.

    """

    try:

        parsed = urlparse(ensure_url_scheme(url, default_scheme="https"))

        path = (parsed.path or "").lower()

        query = unquote(parsed.query or "").lower()

        full = f"{path}?{query}"



        pdfjs_hit = (

            "pdfjsviewer" in path

            or "/pdf.js/web/viewer.html" in path

            or ("viewer.html" in path and "pdf" in path)

        )



        ojs_auth_noise = (

            "login/signout" in query

            or "login/signout" in full

            or "source=" in query

            or ("file=" in query and "index.php" in query)

        )



        return pdfjs_hit and ojs_auth_noise



    except Exception:

        return False





def has_operator_promo_gambling_text(text):

    """

    Frasa kuat yang menunjukkan konten operator/promosi judi nyata,

    bukan sekadar URL noise, artikel, atau viewer PDF.

    """

    normalized = normalize_text_for_match(text)



    promo_terms = [

        "slot gacor",

        "slot online",

        "rtp slot",

        "situs slot",

        "situs judi",

        "togel online",

        "casino online",

        "kasino online",

        "live casino",

        "link alternatif judi",

        "link alternatif slot",

        "daftar judi",

        "daftar togel",

        "daftar slot",

        "bonus new member",

        "bonus deposit",

        "minimal deposit",

        "deposit qris",

        "deposit dana",

        "withdraw cepat",

        "live chat 24 jam",

        "agen togel",

        "bandar togel",

        "agen slot",

        "mahjong ways",

        "mahjongways",

        "gates of olympus",

        "sweet bonanza",

        "starlight princess",

        "pragmatic play",

        "pg soft",

        "pgsoft",

        "maxwin",

        "scatter",

    ]



    return any(

        normalize_text_for_match(term) in normalized

        for term in promo_terms

    )





def should_skip_alert_as_pdfjs_ojs_noise(url, page_text):

    if not is_ojs_pdfjs_viewer_noise_url(url):

        return False



    if has_operator_promo_gambling_text(page_text):

        return False



    return True





def should_skip_hidden_alert_as_safe_news_false_positive(html, page_url, evidence_items):

    """

    Hindari false positive hidden-link pada halaman berita/artikel/resmi.



    Contoh:

    - Kompas/Tirto artikel tentang judi slot.

    - Humas Polri related news internal: "cegah judi online".

    """

    if not evidence_items:

        return False



    try:

        soup = BeautifulSoup(html or "", "html.parser")

        page_text = soup.get_text(" ", strip=True)

    except Exception:

        page_text = ""



    safe_context_text = f"{page_url or ''} {page_text[:8000]}"



    try:

        if not has_safe_news_or_factcheck_context(safe_context_text):

            return False

    except Exception:

        return False



    page_host = normalize_hostname_for_compare(

        urlparse(ensure_url_scheme(page_url or "", default_scheme="https")).hostname

    )



    promo_terms = [

        "slot gacor",

        "slot online",

        "situs slot",

        "rtp slot",

        "togel online",

        "casino online",

        "kasino online",

        "situs judi",

        "link alternatif judi",

        "link alternatif slot",

        "daftar judi",

        "bonus new member",

        "deposit qris",

        "deposit dana",

        "withdraw cepat",

        "live chat 24 jam",

        "agen togel",

        "bandar togel",

        "agen slot",

        "mahjong ways",

        "mahjongways",

        "pg soft",

        "pgsoft",

        "pragmatic play",

    ]



    safe_related_news_terms = [

        "cegah",

        "edukasi",

        "sosialisasi",

        "program",

        "polri goes to campus",

        "komdigi",

        "pemerintah",

        "polisi",

        "tangkap",

        "penangkapan",

        "tersangka",

        "pelaku",

        "kasus",

        "motif",

        "pemicu",

        "resahkan masyarakat",

        "tindak aktor",

    ]



    for item in evidence_items:

        matched_terms = item.get("matched_terms", []) or []

        normalized_terms = {

            normalize_text_for_match(term)

            for term in matched_terms

            if normalize_text_for_match(term)

        }



        if not normalized_terms:

            return False



        if not normalized_terms.issubset(SAFE_NEWS_HIDDEN_TOPIC_TERMS):

            return False



        href = str(item.get("href", "") or "").strip()

        anchor = str(item.get("anchor", "") or "")

        context_sample = str(item.get("context_sample", "") or "")



        context = normalize_text_for_match(f"{href} {anchor} {context_sample}")



        try:

            link_host = normalize_hostname_for_compare(

                urlparse(urljoin(page_url or "", href)).hostname

            )

        except Exception:

            link_host = ""



        same_site = False

        if page_host and link_host:

            same_site = (

                link_host == page_host

                or link_host.endswith("." + page_host)

                or page_host.endswith("." + link_host)

            )



            if not same_site:

                return False



        benign_related_news = any(

            normalize_text_for_match(term) in context

            for term in safe_related_news_terms

        )



        # Kalau internal related article bertema edukasi/penindakan, jangan dianggap promo.

        if same_site and benign_related_news:

            continue



        for promo in promo_terms:

            if normalize_text_for_match(promo) in context:

                return False



    return True





def has_hidden_gambling_links(html, page_url=None):

    evidence = extract_hidden_gambling_links(html, page_url=page_url)



    if len(evidence) >= 1:

        all_hits = []



        for item in evidence:

            all_hits.extend(item.get("matched_terms", []))



        unique_hits = list(dict.fromkeys(all_hits))



        if should_skip_hidden_alert_as_safe_news_false_positive(

            html=html,

            page_url=page_url,

            evidence_items=evidence,

        ):

            log(

                f"[INFO] Hidden gambling terms skipped as safe news/article context: "

                f"{page_url}, hits={unique_hits}, evidence_count={len(evidence)}"

            )

            return False, [], []



        return True, unique_hits, evidence



    return False, [], []





# =========================

# ML Detection

# =========================

def is_gambling_content_ml(text, threshold=0.75):

    try:

        X = vectorizer.transform([text])



        if hasattr(model, "predict_proba"):

            proba = model.predict_proba(X)[0]

            classes = list(model.classes_)



            if "gambling" in classes:

                gambling_index = classes.index("gambling")

                confidence = float(proba[gambling_index])

                pred = "gambling" if confidence >= 0.5 else "non-gambling"

            else:

                max_index = int(proba.argmax())

                confidence = float(proba[max_index])

                pred = str(model.classes_[max_index])



            return confidence >= threshold, confidence, pred



        pred = model.predict(X)[0]



        if pred == "gambling":

            return True, 1.0, "gambling"



        return False, 0.0, str(pred)



    except Exception as e:

        log(f"[WARN] ML detection fail: {e}")

        return False, 0.0, "unknown"





# =========================

# IndoBERT Detection

# =========================

def split_text_for_indobert(text, max_chars=1800, max_chunks=8):

    text = re.sub(r"\s+", " ", text or "").strip()



    if not text:

        return []



    chunks = []



    # Prioritaskan bagian awal halaman karena biasanya title, heading,

    # menu, dan promo utama ada di awal.

    while text and len(chunks) < max_chunks:

        chunk = text[:max_chars]



        # Potong di batas kalimat kalau memungkinkan.

        last_boundary = max(

            chunk.rfind("."),

            chunk.rfind("!"),

            chunk.rfind("?"),

            chunk.rfind("\n"),

        )



        if last_boundary > 500:

            chunk = chunk[:last_boundary + 1]



        chunk = chunk.strip()



        if chunk:

            chunks.append(chunk)



        text = text[len(chunk):].strip()



    return chunks





def get_indobert_gambling_index():

    if indobert_model is None:

        return 1



    label2id = getattr(indobert_model.config, "label2id", {}) or {}



    for label, idx in label2id.items():

        normalized = normalize_text_for_match(label).replace(" ", "-")



        if normalized in {"gambling", "judol", "judi", "online-gambling"}:

            try:

                return int(idx)

            except Exception:

                return 1



    num_labels = int(getattr(indobert_model.config, "num_labels", 2) or 2)



    if num_labels <= 1:

        return 0



    return 1





def is_gambling_content_indobert(text):

    """

    Return:

    - bert_hit: bool

    - gambling_confidence: float

    - pred: gambling / non-gambling / disabled / unknown



    Scoring dibuat konservatif:

    - Tidak lagi memakai single max chunk sebagai keputusan final.

    - Untuk halaman panjang, minimal dua chunk harus mendukung gambling.

    - Ini menekan false positive halaman produk/manual seperti Omron, Cromwell, Sony.

    """

    if not ENABLE_INDO_BERT:

        return False, 0.0, "disabled"



    if indobert_model is None or indobert_tokenizer is None or torch is None:

        return False, 0.0, "disabled"



    try:

        chunks = split_text_for_indobert(text)



        if not chunks:

            return False, 0.0, "non-gambling"



        gambling_index = get_indobert_gambling_index()

        all_gambling_conf = []



        with torch.no_grad():

            for start in range(0, len(chunks), INDO_BERT_BATCH_SIZE):

                batch = chunks[start:start + INDO_BERT_BATCH_SIZE]



                encoded = indobert_tokenizer(

                    batch,

                    truncation=True,

                    padding=True,

                    max_length=INDO_BERT_MAX_LENGTH,

                    return_tensors="pt",

                )



                encoded = {

                    key: value.to(indobert_device)

                    for key, value in encoded.items()

                }



                outputs = indobert_model(**encoded)

                probs = torch.softmax(outputs.logits, dim=-1)



                batch_conf = probs[:, gambling_index].detach().cpu().tolist()

                all_gambling_conf.extend(float(v) for v in batch_conf)



        if not all_gambling_conf:

            return False, 0.0, "non-gambling"



        sorted_conf = sorted(all_gambling_conf, reverse=True)

        top1 = sorted_conf[0]

        top2_avg = sum(sorted_conf[:2]) / min(len(sorted_conf), 2)

        chunks_above_threshold = sum(1 for v in all_gambling_conf if v >= INDO_BERT_THRESHOLD)

        chunks_above_strong = sum(1 for v in all_gambling_conf if v >= INDO_BERT_STRONG_THRESHOLD)



        if len(all_gambling_conf) == 1:

            final_conf = top1

            hit = final_conf >= INDO_BERT_STRONG_THRESHOLD

        else:

            final_conf = top2_avg

            hit = (

                final_conf >= INDO_BERT_THRESHOLD

                and (

                    chunks_above_threshold >= 2

                    or chunks_above_strong >= 1

                )

            )



        pred = "gambling" if hit else "non-gambling"



        log(

            "[DEBUG] IndoBERT chunks "

            f"count={len(all_gambling_conf)}, "

            f"top1={top1:.2f}, "

            f"top2_avg={top2_avg:.2f}, "

            f"above_threshold={chunks_above_threshold}, "

            f"above_strong={chunks_above_strong}, "

            f"final_p={final_conf:.2f}, "

            f"pred={pred}"

        )



        return hit, final_conf, pred



    except Exception as e:

        log(f"[WARN] IndoBERT detection fail: {e}")

        return False, 0.0, "unknown"



# =========================

# Safe Context Helpers

# =========================

def safe_context_match(page_text, page_title, safe_terms, gambling_terms, min_safe_score=3):

    combined = f"{page_title} {(page_text or '')[:12000]}".lower()



    safe_score = sum(1 for term in safe_terms if term in combined)

    gambling_score = sum(1 for term in gambling_terms if term in combined)



    return safe_score >= min_safe_score and gambling_score == 0





COMMON_PROMO_GAMBLING_TERMS = [

    "togel", "casino", "sportsbook", "slot gacor", "rtp slot",

    "live casino", "baccarat", "roulette", "blackjack", "sbobet",

    "joker123", "pragmatic play", "deposit slot", "bonus new member",

    "casino online", "situs judi", "togel online", "daftar judi",

    "link alternatif", "mahjong ways", "mahjong wins",

]





def is_safe_weton_context(page_text, page_title):

    return safe_context_match(

        page_text, page_title,

        safe_terms=[

            "weton", "neptu", "pasaran jawa", "primbon jawa",

            "penanggalan jawa", "kalkulator weton", "hari baik",

            "tafsir mimpi", "kecocokan jodoh", "kalender jawa",

            "watak", "rejeki", "rezeki",

        ],

        gambling_terms=COMMON_PROMO_GAMBLING_TERMS,

    )





def is_safe_store_context(page_text, page_title):

    return safe_context_match(

        page_text, page_title,

        safe_terms=[

            "top up", "topup", "voucher game", "diamond", "mobile legends",

            "free fire", "pulsa", "paket data", "checkout", "keranjang",

            "produk", "harga", "payment", "pembayaran", "order", "pesanan",

            "invoice", "customer service", "metode pembayaran",

        ],

        gambling_terms=COMMON_PROMO_GAMBLING_TERMS,

    )





def is_safe_general_ecommerce_context(page_text, page_title):

    return safe_context_match(

        page_text, page_title,

        safe_terms=[

            "product", "products", "produk", "shop", "store", "store location",

            "accessories", "aksesori", "adapter", "earphone", "headphone",

            "charging cable", "charger", "car charger", "powerbank",

            "reseller", "ecommerce", "brand store", "point store",

            "customer service", "privacy policy", "terms of use",

            "warranty", "garansi", "catalog", "katalog", "shopping cart",

            "cart", "checkout", "order", "invoice", "payment", "shipping",

        ],

        gambling_terms=COMMON_PROMO_GAMBLING_TERMS,

        min_safe_score=4,

    )





def is_safe_industrial_automation_product_context(page_text, page_title):

    return safe_context_match(

        page_text, page_title,

        safe_terms=[

            "industrial automation", "automation", "omron",

            "sensor", "sensors", "photomicrosensor", "photomicrosensors",

            "photomicro sensor", "photomicro sensors", "photoelectric sensor",

            "optical sensor", "slot type", "slot-type",

            "connector", "pre-wired", "pre wired", "non-modulated",

            "non modulated", "direct switching", "switching capacity",

            "instruction manual", "instruction sheet", "manual",

            "catalog", "cad", "rohs", "product category",

            "control components", "safety components", "motion", "drives",

            "relays", "automation systems", "ee-sx", "ee sx",

        ],

        gambling_terms=COMMON_PROMO_GAMBLING_TERMS,

        min_safe_score=4,

    )





def is_safe_airport_government_context(page_text, page_title):

    return safe_context_match(

        page_text, page_title,

        safe_terms=[

            "airport", "airports", "air navigation", "aviation",

            "civil aviation", "administration", "timor-leste", "aeroporto",

            "flight", "passenger", "terminal", "runway", "air traffic",

            "navigation administration", "airports & air navigation administration",

        ],

        gambling_terms=COMMON_PROMO_GAMBLING_TERMS,

    )





def is_safe_hr_payroll_context(page_text, page_title):

    return safe_context_match(

        page_text, page_title,

        safe_terms=[

            "employment administration system", "attendance management",

            "payroll management", "absensi", "absensi online",

            "kehadiran karyawan", "data karyawan", "penggajian",

            "hitung gaji", "pph 21", "bpjs kesehatan",

            "bpjs ketenagakerjaan", "shift management", "jadwal kerja",

            "kelola shift", "cuti", "wfh", "wfo", "hris",

            "manajemen absensi",

        ],

        gambling_terms=COMMON_PROMO_GAMBLING_TERMS,

    )





def is_safe_rental_transport_context(page_text, page_title):

    return safe_context_match(

        page_text, page_title,

        safe_terms=[

            "rental mobil", "sewa mobil", "rental car", "car rental",

            "mobil lepas kunci", "dengan sopir", "driver", "chauffeur",

            "antar jemput", "airport transfer", "travel", "wisata", "tour",

            "transportasi", "armada", "booking", "reservasi", "harga sewa",

            "avanza", "xenia", "innova", "hiace", "alphard", "elf",

        ],

        gambling_terms=COMMON_PROMO_GAMBLING_TERMS,

    )





def is_safe_digital_project_context(page_text, page_title):

    return safe_context_match(

        page_text, page_title,

        safe_terms=[

            "digital project", "project digital", "produk digital",

            "jasa digital", "website", "web development", "software",

            "aplikasi", "source code", "template", "portfolio", "freelance",

            "project", "layanan digital", "teknologi", "programming",

            "developer", "coding",

        ],

        gambling_terms=COMMON_PROMO_GAMBLING_TERMS,

    )





def is_safe_news_media_context(page_text, page_title):

    return safe_context_match(

        page_text, page_title,

        safe_terms=[

            "berita", "news", "artikel", "redaksi", "jurnalis", "media",

            "pers", "opini", "nasional", "daerah", "politik", "hukum",

            "ekonomi", "peristiwa", "headline", "breaking news", "editor",

            "reporter", "kategori", "publikasi", "siaran pers",

            "berita utama", "pedoman media siber",

        ],

        gambling_terms=COMMON_PROMO_GAMBLING_TERMS,

    )





def is_safe_news_anti_gambling_context(page_text, page_title):

    combined = f"{page_title} {(page_text or '')[:12000]}".lower()



    news_terms = [

        "berita", "news", "artikel", "redaksi", "jurnalis", "media",

        "reporter", "kategori", "publikasi", "pedoman media siber",

        "berita utama", "daerah", "nasional", "hukum", "tni", "polisi",

        "polres", "polda", "pemerintah", "bupati", "masyarakat",

        "narkoba", "generasi muda", "kenakalan remaja", "korban", "tppo",

        "kamboja",

    ]



    anti_gambling_terms = [

        "korban tppo", "dipaksa kerja judi online", "kerja judi online",

        "cegah judi online", "mencegah judi online", "memberantas judi online",

        "pemberantasan judi online", "bahaya judi online", "dampak judi online",

        "terjerat judi online", "kasus judi online", "praktik judi online",

        "melawan judi online", "menghindari judi online",

        "judi online dan narkoba", "dari judi online",

        "benteng generasi muda dari judi online", "ajakan memerangi judi online",

        "peredaran judi online", "pengaruh negatif judi online",

    ]



    promotional_terms = [

        "slot gacor", "rtp slot", "togel online", "casino online",

        "situs judi", "daftar judi", "link alternatif", "bonus new member",

        "deposit slot", "minimal deposit", "maxwin", "scatter mahjong",

        "scatter hitam", "sbobet", "joker123", "pragmatic play",

    ]



    news_score = sum(1 for term in news_terms if term in combined)

    anti_score = sum(1 for term in anti_gambling_terms if term in combined)

    promo_score = sum(1 for term in promotional_terms if term in combined)



    return news_score >= 2 and anti_score >= 1 and promo_score == 0





def is_safe_anime_comic_popculture_context(page_text, page_title):

    return safe_context_match(

        page_text, page_title,

        safe_terms=[

            "comicfest", "comic fest", "comic", "komik", "cosplay",

            "cosplayer", "anime", "manga", "otaku", "pop culture",

            "jejepangan", "japan", "all about japan", "event pop culture",

            "kalender acara", "profil cosplayer", "drama jepang",

            "festival", "booth", "artist", "photopack", "karakter game",

        ],

        gambling_terms=COMMON_PROMO_GAMBLING_TERMS,

    )





# =========================

# CAPTCHA / Anti-bot

# =========================

def is_captcha_or_antibot_page(text, title=""):

    combined = f"{title} {text}".lower()



    indicators = [

        "performing security verification", "security verification",

        "this website uses a security service", "protect against malicious bots",

        "verifies you are not a bot", "verification successful", "waiting for",

        "verifying", "cloudflare", "ray id", "captcha", "verify you are human",

        "human verification", "checking your browser", "just a moment",

        "please wait while we check", "security check", "access denied",

        "cf-challenge", "turnstile", "recaptcha", "hcaptcha", "are you human",

        "enable javascript", "ddos protection", "browser check",

        "bot detection", "unusual traffic",

    ]



    return any(indicator in combined for indicator in indicators)





# =========================

# Risk Classification

# =========================

def has_context_terms(text, terms):

    normalized = normalize_text_for_match(text)



    if not normalized:

        return False



    for term in terms:

        normalized_term = normalize_text_for_match(term)



        if normalized_term and normalized_term in normalized:

            return True



    return False





def has_real_gambling_context(text):

    """

    Konteks gambling yang eksplisit.



    Sengaja tidak memasukkan standalone "slot", "gacor", "rtp", "bet",

    "deposit", atau "bonus" karena banyak false positive di produk industri,

    artikel support, istilah teknis, dan ecommerce normal.

    """

    gambling_terms = [

        "slot online",

        "slot gacor",

        "rtp slot",

        "situs slot",

        "agen slot",

        "demo slot",

        "slot deposit",

        "slot qris",

        "slot dana",

        "slot pulsa",

        "slot88",

        "slot 88",

        "judi online",

        "situs judi",

        "agen judi",

        "permainan judi",

        "casino online",

        "online casino",

        "live casino",

        "togel online",

        "bandar togel",

        "agen togel",

        "togel 4d",

        "togel4d",

        "taruhan online",

        "taruhan bola",

        "sports betting",

        "sportsbook",

        "poker online",

        "bonus new member",

        "bonus deposit",

        "deposit qris",

        "deposit dana",

        "deposit ovo",

        "deposit gopay",

        "link alternatif judi",

        "link alternatif slot",

        "mahjong ways",

        "gates of olympus",

        "sweet bonanza",

        "starlight princess",

        "fortune tiger",

        "pragmatic play",

        "pg soft",

        "maxwin",

        "max win",

        "mahjongways kasino online",

        "mahjong ways kasino online",

        "kasino online",

        "rtp pgsoft",

        "rtp pg soft",

        "saldo dana gratis",

        "game online slot",

        "game online gacor",

        "game online kasino",

        "blackjack online",

        "mahjong ways 2",

        "mahjongways 2",

        "link slot online",

        "situs kasino online",

        "congtogel",

        "cong togel",

        "toto macau",

        "togel macau",

        "pengeluaran macau",

        "keluaran macau",

        "data macau",

        "result macau",

        "live draw macau",

        "pasaran togel",

        "pasaran macau",

        "link resmi login",

        "login dan daftar game online",

        "login & daftar game online",

        "daftar game online eksklusif",

        "toto 4d",

        "toto4d",

        "situs toto",

        "bandar toto",

    ]



    return has_context_terms(text, gambling_terms)





def has_safe_product_or_industrial_context(text):

    """

    Konteks aman yang sering salah dibaca IndoBERT sebagai gambling karena ada kata ambigu:

    slot, catalog, download, manual, cad, lineup, support, sensor, atau game.

    """

    safe_terms = [

        # OMRON / industrial automation / sensor

        "omron",

        "industrial automation",

        "automation",

        "sensor",

        "sensors",

        "photomicrosensor",

        "photomicrosensors",

        "photomicro sensor",

        "photomicro sensors",

        "photoelectric sensor",

        "photoelectric sensors",

        "optical sensor",

        "optical sensors",

        "proximity sensor",

        "fiber sensor",

        "displacement sensor",

        "slot type",

        "slot-type",

        "slot type photomicrosensor",

        "slot-type photomicrosensor",

        "connector",

        "pre wired",

        "pre-wired",

        "non modulated",

        "non-modulated",

        "direct switching",

        "instruction manual",

        "instruction sheet",

        "download manual",

        "download catalog",

        "catalog",

        "cad",

        "lineup",

        "specification",

        "dimension",

        "rohs",

        "ee sx",

        "ee-sx",

        "sx47",

        "sx67",

        "sx95",

        "sx77",

        "sx87",



        # Cromwell / cutting tools / machining

        "cromwell",

        "cutting tools",

        "cutting tool",

        "slot drill",

        "slot drills",

        "slot drills short",

        "slot drill short",

        "short slot drill",

        "t slot",

        "t-slot",

        "t slot steel",

        "t-slot steel",

        "clamping set",

        "machine metric",

        "threaded shank",

        "high speed steel",

        "hss",

        "flute",

        "2fl",

        "shank",

        "carbide",

        "milling",

        "drilling",

        "metalworking",

        "machining",

        "end mill",

        "end mills",



        # Sony / electronics support

        "sony",

        "electronics support",

        "support articles",

        "support article",

        "manual",

        "firmware",

        "software",

        "driver",

        "download",

        "product support",

        "troubleshooting",

        "warranty",

        "television",

        "camera",

        "headphones",

        "playstation",

        "bravia",

        "xperia",



        # Ecommerce / product catalog normal

        "product category",

        "product family",

        "data sheet",

        "datasheet",

        "technical document",

        "instruction sheet",

        "user manual",

        "download center",

        "spare part",

        "spare parts",

        "accessories",

        "customer support",

    ]



    return has_context_terms(text, safe_terms)





def has_safe_news_or_factcheck_context(text):

    """

    Safe context untuk halaman berita, fact-check, akademik, kampus,

    jurnal, dokumen publik, atau artikel yang membahas judi sebagai isu,

    bukan mempromosikan layanan judi.



    Catatan:

    - "judi online" dan "judi slot" bisa menjadi topik berita, jadi tidak otomatis promo.

    - Frasa operator/promo seperti slot gacor, deposit qris, bonus new member tetap dianggap tidak safe.

    """

    normalized = normalize_text_for_match(text)



    news_or_info_terms = [

        "berita",

        "news",

        "artikel",

        "redaksi",

        "jurnalis",

        "laporan",

        "cek fakta",

        "fact check",

        "periksa fakta",

        "klarifikasi",

        "hoaks",

        "hoax",

        "benarkah",

        "apa benar",

        "take down",

        "diretas",

        "pemerintah",

        "kominfo",

        "komdigi",

        "polisi",

        "kampus",

        "universitas",

        "fakultas",

        "jurnal",

        "journal",

        "article",

        "download",

        "pdf",

        "repository",

        "iain",

        "uin",

        "ut.ac.id",

        "ipb.ac.id",

        "unair.ac.id",

        "upy.ac.id",

        "humas.polri.go.id",

        "humas polri",

        "/news/detail",

        "news detail",

        "kompas.id",

        "/artikel/",

        "motif judi slot",

        "judi slot jadi pemicu",

        "resahkan masyarakat",

        "pemerintah tindak",

        "aktor judi",

        "polisi tangkap",

        "pelaku",

        "tersangka",

        "kriminal",

        "mutilasi",

        "kasus",

        "pemicu",

        "ditangkap",

        "penangkapan",

        "kapolres",

        "polda",

        "polres",

    ]



    hard_promo_or_operator_terms = [

        "daftar sekarang",

        "login member",

        "login dan daftar",

        "link resmi login",

        "link alternatif",

        "minimal deposit",

        "deposit qris",

        "deposit dana",

        "bonus new member",

        "bonus deposit",

        "withdraw cepat",

        "rtp tinggi",

        "slot gacor",

        "slot online",

        "togel online",

        "casino online",

        "kasino online",

        "situs judi",

        "live chat 24 jam",

        "agen togel",

        "bandar togel",

        "agen slot",

        "mahjong ways",

        "mahjongways",

        "pg soft",

        "pgsoft",

        "pragmatic play",

    ]



    has_info_context = any(

        normalize_text_for_match(term) in normalized

        for term in news_or_info_terms

    )



    has_hard_promo_context = any(

        normalize_text_for_match(term) in normalized

        for term in hard_promo_or_operator_terms

    )



    return has_info_context and not has_hard_promo_context





def has_safe_context_without_real_gambling(text):

    return (

        has_safe_product_or_industrial_context(text)

        and not has_real_gambling_context(text)

    )





def has_strong_operator_gambling_promo(page_text, page_title=""):
    """Check multiple independent signals for explicit operator-style promotion."""
    title = normalize_text_for_match(page_title)
    body = normalize_text_for_match(page_text)
    title_promo = (re.search(r"\bspin\b", title) is not None
                   and re.search(r"\bmenang\b", title) is not None)
    account_calls = (re.search(r"\blogin\b", body) is not None
                     and re.search(r"\bdaftar\b", body) is not None)
    terms = ("deposit", "jackpot", "rtp", "link alternatif")
    term_count = sum(bool(has_context_terms(body, [term])) for term in terms)
    return bool(title_promo and account_calls and term_count >= 3)


def has_slot_thailand_registration(page_title, page_html, current_url):
    if not re.search(r"\bslot thailand\b", normalize_text_for_match(page_title)):
        return False
    html = page_html() if callable(page_html) else page_html
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.select("a[href]"):
        if link.find_parent(["nav", "header", "footer", "template", "noscript"]):
            continue
        if link.find_parent(attrs={"hidden": True}) or link.has_attr("hidden"):
            continue
        label = link.get_text(" ", strip=True) or link.get("aria-label", "")
        if normalize_text_for_match(label) not in ("daftar", "daftar sekarang", "daftar akun"):
            continue
        href = link["href"].strip()
        if not href or href.startswith("#"):
            continue
        try:
            target = urlparse(urljoin(current_url or "", href))
            if target.scheme in ("https", "http") and target.hostname:
                return True
        except ValueError:
            continue
    return False


def is_anti_gambling_rejection_context(page_text, page_title="", page_html=None, current_url=""):
    """Recognize an explicit refusal of gambling business, not a domain allowlist.

    Only use this for a contextual-phrase medium-risk decision with a
    non-gambling ML prediction. Strong operator marketing or hidden links
    always veto the exemption. The existing IndoBERT call remains unchanged.
    """
    if not isinstance(page_text, str) or len(page_text) < 40:
        return False
    text = re.sub(r"\s+", " ", page_text[:30000].lower())
    rejection = (
        r"\b(?:tanpa|bebas|anti|menolak|melarang|dilarang|"
        r"tidak\s+(?:menerima|melayani|mengizinkan|memfasilitasi|menyediakan))"
        r"\b[^.!?]{0,110}\b(?:judi|judol|togel|casino)\b",
        r"\b(?:judi|judol|togel|casino)\b[^.!?]{0,110}"
        r"\b(?:ditolak|dilarang|diblokir|tidak\s+diterima|"
        r"tidak\s+diizinkan|tidak\s+diperbolehkan|tidak\s+dilayani|kami\s+tolak)\b",
    )
    if not any(re.search(pattern, text) for pattern in rejection):
        return False

    # A legal disclaimer must not erase actual gambling promotion elsewhere.
    promotions = (
        r"\b(?:slot\s+gacor|rtp\s+slot|bonus\s+new\s+member|"
        r"deposit\s+(?:slot|qris|dana)|togel\s+online|casino\s+online|"
        r"link\s+alternatif\s+(?:judi|slot)|maxwin|mahjong\s+ways|"
        r"gates\s+of\s+olympus|pragmatic\s+play)\b",
        r"\b(?:main|bermain|pasang|taruhan|klaim|spin)\b"
        r"[^.!?]{0,60}\b(?:slot|judi|togel|casino)\b",
        r"\b(?:slot|judi|togel|casino)\b[^.!?]{0,60}"
        r"\b(?:daftar\s+sekarang|login\s+sekarang|main\s+sekarang|"
        r"klaim\s+bonus|pasang\s+sekarang)\b",
    )
    if any(re.search(pattern, text) for pattern in promotions):
        return False

    # Never suppress an existing hidden gambling-link signal. Fail closed.
    try:
        html = page_html() if callable(page_html) else page_html
        if html and has_hidden_gambling_links(html, page_url=current_url)[0]:
            return False
    except Exception:
        return False
    return True


def classify_risk(page_text, ml_hit, ml_conf, ml_pred=None, captcha_hit=False, ml_source="classic_ml", page_title="", page_html=None, current_url=""):

    text_hits = find_keyword_hits(page_text, STRONG_TEXT_KEYWORDS)



    if captcha_hit:

        return "blocked_or_captcha", "captcha_or_antibot", text_hits



    hard_gambling_keywords = [

        "judi", "judol", "togel", "lottery", "number games", "casino",

        "live casino", "sportsbook", "sports betting", "wager", "wagering",

        "odds", "live dealer", "live-dealer", "baccarat", "roulette",

        "blackjack", "dragon tiger", "sic bo", "bandarq", "texas poker",

        "sbobet", "joker123", "aviator", "sweet bonanza", "gates of olympus",

        "fortune tiger", "mahjong ways", "mahjong wins", "slot gacor",

        "rtp slot", "maxwin", "pragmatic", "pragmatic play",

        "pg soft", "pgsoft",

        "mahjongways kasino online",

        "mahjong ways kasino online",

        "kasino online",

        "rtp pgsoft",

        "rtp pg soft",

        "saldo dana gratis",

        "game online slot",

        "game online gacor",

        "game online kasino",

        "blackjack online",

        "mahjong ways 2",

        "mahjongways 2",

        "link slot online",

        "situs kasino online",

    ]



    weak_gambling_keywords = [

        "slot", "slots", "rtp", "bet", "betting", "deposit", "withdrawal",

        "withdraw", "bonus", "cashback", "jackpot", "free spins",

        "free bets", "vip tier", "loyalty rewards", "playthrough",

        "link alternatif",

    ]



    payment_keywords = [

        "dana", "ovo", "gopay", "shopeepay", "qris",

    ]



    threshold = INDO_BERT_THRESHOLD if ml_source == "indobert" else ML_THRESHOLD

    strong_threshold = INDO_BERT_STRONG_THRESHOLD if ml_source == "indobert" else ML_STRONG_THRESHOLD



    real_gambling_context = has_real_gambling_context(page_text)

    safe_product_context = has_safe_product_or_industrial_context(page_text)

    safe_news_context = has_safe_news_or_factcheck_context(page_text)

    contextual_phrase_hit = has_contextual_gambling_phrase(page_text)



    hard_hits = [hit for hit in text_hits if hit in hard_gambling_keywords]

    weak_hits = [hit for hit in text_hits if hit in weak_gambling_keywords]

    payment_hits = [hit for hit in text_hits if hit in payment_keywords]



    hard_count = len(hard_hits)

    weak_count = len(weak_hits)

    payment_count = len(payment_hits)



    # Safe context untuk halaman berita/artikel/fact-check/resmi.

    # Contoh: Kompas/Humas Polri membahas "judi slot" sebagai isu kriminal.

    if safe_news_context:

        return "low_risk", "safe_news_or_factcheck_context", text_hits



    # Safe context untuk halaman produk/manual/CAD/support/sensor/cutting tools.

    # Harus lebih dulu daripada rule ML strong.

    # Bukti promosi operator judi yang kuat mengalahkan konteks produk umum.
    # Hanya berlaku jika IndoBERT sangat yakin dan ada beberapa sinyal eksplisit.
    if (ml_source == "indobert"
            and ml_pred == "gambling"
            and ml_conf >= strong_threshold
            and has_strong_operator_gambling_promo(page_text, page_title)):
        return "high_risk", "strong_operator_gambling_promotion", text_hits

    if safe_product_context and not real_gambling_context and not contextual_phrase_hit:

        return "low_risk", "safe_product_or_industrial_context", text_hits



    # ML sangat yakin gambling, tetapi jangan kirim tanpa konteks gambling eksplisit.

    if ml_pred == "gambling" and ml_conf >= strong_threshold:

        if not REQUIRE_GAMBLING_CONTEXT_FOR_ML:

            return "high_risk", "ml_strong_confidence", text_hits



        if real_gambling_context or contextual_phrase_hit or hard_count >= 1:

            return "high_risk", "ml_strong_with_real_gambling_context", text_hits



        if has_slot_thailand_registration(page_title, page_html, current_url):
            return "high_risk", "ml_strong_slot_thailand_registration", text_hits + ["slot thailand", "registration link"]

        return "low_risk", "ml_strong_but_no_real_gambling_context", text_hits



    if ml_pred == "gambling" and ml_conf >= threshold and (real_gambling_context or hard_count >= 1):

        return "high_risk", "ml_and_real_gambling_context", text_hits



    if hard_count >= 4 and ml_conf >= 0.45 and real_gambling_context:

        return "high_risk", "many_hard_gambling_keywords", text_hits



    if contextual_phrase_hit and (hard_count >= 1 or weak_count >= 2):


        # A business anti-judol policy is not an advertisement when the
        # classifier itself predicts non-gambling and no promo/hidden link is
        # present. This guard applies ONLY to the contextual medium-risk rule.
        if (ml_pred == "non-gambling" and is_anti_gambling_rejection_context(
                page_text, page_title, page_html, current_url)):
            return "low_risk", "anti_gambling_rejection_context", text_hits
        return "medium_risk", "contextual_gambling_phrase_detected", text_hits



    if hard_count >= 2 and real_gambling_context:

        return "medium_risk", "gambling_keywords_or_links_detected", text_hits



    if hard_count >= 1 and weak_count >= 3 and real_gambling_context:

        return "medium_risk", "mixed_hard_and_weak_gambling_keywords", text_hits



    if ml_pred == "gambling" and ml_conf >= 0.50 and contextual_phrase_hit:

        return "medium_risk", "ml_gambling_with_contextual_phrase", text_hits



    if ml_pred == "gambling" and ml_conf >= 0.65 and hard_count >= 2 and real_gambling_context:

        return "medium_risk", "ml_gambling_with_multiple_hard_keywords", text_hits



    if ml_pred == "gambling" and ml_conf >= threshold and hard_count >= 1 and weak_count >= 2 and real_gambling_context:

        return "medium_risk", "ml_gambling_with_hard_and_weak_keywords", text_hits



    if hard_count == 0 and weak_count == 0 and payment_count > 0:

        return "low_risk", "payment_keywords_without_gambling_context", text_hits



    if hard_count == 0 and weak_count > 0 and ml_pred != "gambling":

        return "low_risk", "weak_keywords_without_gambling_context", text_hits



    return "low_risk", "low_signal", text_hits



def make_low_risk_result(captcha_hit, kw_hit, reason):

    return {

        "captcha_hit": captcha_hit,

        "kw_hit": kw_hit,

        "ml_hit": False,

        "ml_conf": 0.0,

        "ml_pred": "non-gambling",

        "status": "low_risk",

        "reason": reason,

        "strong_hits": [],

    }





def classify_text_content(source, url, page_text, page_title, current_url, page_html=None):

    captcha_hit = is_captcha_or_antibot_page(page_text, page_title)

    kw_hit = is_gambling_content_keywords(page_text)



    full_context_text = f"{url or ''} {current_url or ''} {page_title or ''} {page_text or ''}"



    if has_safe_news_or_factcheck_context(full_context_text):

        log(

            f"[DEBUG] source={source}, safe_context=safe_news_or_factcheck, "

            f"title={page_title}, current_url={current_url}, text_len={len(page_text or '')}"

        )

        return make_low_risk_result(

            captcha_hit,

            kw_hit,

            reason="safe_news_or_factcheck_context",

        )



    safe_checks = [

        ("news_anti_gambling", is_safe_news_anti_gambling_context, "safe_news_anti_gambling_context"),

        ("weton_primbon", is_safe_weton_context, "safe_weton_primbon_context"),

        ("store_topup", is_safe_store_context, "safe_store_topup_context"),

        ("general_ecommerce", is_safe_general_ecommerce_context, "safe_general_ecommerce_context"),

        ("industrial_automation_product", is_safe_industrial_automation_product_context, "safe_industrial_automation_product_context"),

        ("airport_government", is_safe_airport_government_context, "safe_airport_government_context"),

        ("hr_payroll", is_safe_hr_payroll_context, "safe_hr_payroll_context"),

        ("rental_transport", is_safe_rental_transport_context, "safe_rental_transport_context"),

        ("digital_project", is_safe_digital_project_context, "safe_digital_project_context"),

        ("anime_comic_popculture", is_safe_anime_comic_popculture_context, "safe_anime_comic_popculture_context"),

        ("news_media", is_safe_news_media_context, "safe_news_media_context"),

    ]



    for safe_name, checker, reason in safe_checks:

        if checker(page_text, page_title):

            log(

                f"[DEBUG] source={source}, safe_context={safe_name}, "

                f"title={page_title}, current_url={current_url}, text_len={len(page_text or '')}"

            )

            return make_low_risk_result(captcha_hit, kw_hit, reason)



    classic_ml_hit, classic_ml_conf, classic_ml_pred = is_gambling_content_ml(

        page_text,

        threshold=ML_THRESHOLD,

    )



    bert_hit, bert_conf, bert_pred = is_gambling_content_indobert(page_text)



    ml_hit = classic_ml_hit

    ml_conf = classic_ml_conf

    ml_pred = classic_ml_pred

    ml_source = "classic_ml"



    if ENABLE_INDO_BERT and bert_pred == "gambling" and bert_conf > ml_conf:

        ml_hit = bert_hit

        ml_conf = bert_conf

        ml_pred = bert_pred

        ml_source = "indobert"

    elif classic_ml_pred == "gambling":

        ml_source = "classic_ml"

    elif ENABLE_INDO_BERT and bert_pred != "disabled":

        ml_source = "classic_ml"



    log(

        f"[DEBUG] ML combine source={ml_source}, "

        f"classic_pred={classic_ml_pred}, classic_p={classic_ml_conf:.2f}, "

        f"bert_pred={bert_pred}, bert_p={bert_conf:.2f}, "

        f"final_pred={ml_pred}, final_p={ml_conf:.2f}"

    )



    status, reason, strong_hits = classify_risk(

        page_text=page_text,

        ml_hit=ml_hit,

        ml_conf=ml_conf,

        ml_pred=ml_pred,

        captcha_hit=captcha_hit,

        ml_source=ml_source,
        page_title=page_title,
        page_html=page_html,
        current_url=current_url,

    )



    log(

        f"[DEBUG] source={source}, ml_source={ml_source}, ml_pred={ml_pred}, "

        f"ml_hit={ml_hit}, p={ml_conf:.2f}, kw_hit={kw_hit}, "

        f"real_gambling_context={has_real_gambling_context(page_text)}, "

        f"safe_product_context={has_safe_product_or_industrial_context(page_text)}, "

        f"status={status}, reason={reason}, captcha={captcha_hit}, "

        f"strong_hits={strong_hits[:10]}, title={page_title}, "

        f"current_url={current_url}, text_len={len(page_text or '')}"

    )



    if captcha_hit or status in ["low_risk", "blocked_or_captcha"]:

        log(f"[DEBUG] source={source}, sample_text={(page_text or '')[:300]}")



    return {

        "captcha_hit": captcha_hit,

        "kw_hit": kw_hit,

        "ml_hit": ml_hit,

        "ml_conf": ml_conf,

        "ml_pred": ml_pred,

        "ml_source": ml_source,

        "status": status,

        "reason": reason,

        "strong_hits": strong_hits,

    }





# =========================

# Requests HTML Fetch

# =========================

def extract_text_from_html(html, page_url=None):

    soup = BeautifulSoup(html or "", "html.parser")



    title = ""

    if soup.title and soup.title.string:

        title = soup.title.string.strip()



    hidden_gambling_hit, hidden_gambling_hits, hidden_gambling_evidence = has_hidden_gambling_links(html, page_url=page_url)



    for tag in soup(["script", "style", "noscript"]):

        tag.decompose()



    text = soup.get_text(separator=" ", strip=True)



    return text, title, hidden_gambling_hit, hidden_gambling_hits, hidden_gambling_evidence





def fetch_html_with_requests(url):

    headers = {

        "User-Agent": (

            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "

            "AppleWebKit/537.36 (KHTML, like Gecko) "

            "Chrome/120.0.0.0 Safari/537.36"

        ),

        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",

        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",

        "Cache-Control": "no-cache",

        "Pragma": "no-cache",

        "Connection": "close",

    }



    resp = None



    try:

        resp = session_http.get(

            url,

            headers=headers,

            timeout=(3, 8),

            allow_redirects=True,

        )



        html = resp.text or ""

        final_url = resp.url

        status_code = resp.status_code



        (

            text,

            title,

            hidden_gambling_hit,

            hidden_gambling_hits,

            hidden_gambling_evidence,

        ) = extract_text_from_html(html, page_url=final_url)



        return {

            "ok": True,

            "status_code": status_code,

            "final_url": final_url,

            "html": html,

            "text": text,

            "title": title,

            "error": "",

            "dns_error": False,

            "hidden_gambling_hit": hidden_gambling_hit,

            "hidden_gambling_hits": hidden_gambling_hits,

            "hidden_gambling_evidence": hidden_gambling_evidence,

        }



    except UrlProcessingTimeout:

        log(f"[WARN] requests fetch interrupted by URL watchdog: {url}")

        raise



    except requests.exceptions.Timeout as e:

        log(f"[WARN] requests timeout: {url} - {e}")



        return {

            "ok": False,

            "status_code": None,

            "final_url": url,

            "html": "",

            "text": "",

            "title": "",

            "error": str(e),

            "dns_error": False,

            "hidden_gambling_hit": False,

            "hidden_gambling_hits": [],

            "hidden_gambling_evidence": [],

        }



    except OSError as e:

        if getattr(e, "errno", None) == 24 or "too many open files" in str(e).lower():

            log(

                f"[ERROR] requests fetch hit too many open files: "

                f"open_fds={get_open_fd_count()}, fd_limit={get_fd_limit_text()}"

            )

            reset_http_session()



        log(f"[WARN] requests fetch fail: {url} - {e}")



        return {

            "ok": False,

            "status_code": None,

            "final_url": url,

            "html": "",

            "text": "",

            "title": "",

            "error": str(e),

            "dns_error": is_dns_error_text(e),

            "hidden_gambling_hit": False,

            "hidden_gambling_hits": [],

            "hidden_gambling_evidence": [],

        }



    except Exception as e:

        log(f"[WARN] requests fetch fail: {url} - {e}")



        return {

            "ok": False,

            "status_code": None,

            "final_url": url,

            "html": "",

            "text": "",

            "title": "",

            "error": str(e),

            "dns_error": is_dns_error_text(e),

            "hidden_gambling_hit": False,

            "hidden_gambling_hits": [],

            "hidden_gambling_evidence": [],

        }



    finally:

        if resp is not None:

            try:

                resp.close()

            except Exception:

                pass





# =========================

# Watermark Helper

# =========================

def add_watermark(image, text1, text2):

    im = image.copy()

    draw = ImageDraw.Draw(im)



    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)



    pad = 12

    box_h = (FONT_SIZE + 6) * 2 + pad * 2



    rect = (

        0,

        im.height - box_h,

        im.width,

        im.height,

    )



    draw.rectangle(rect, fill=(255, 255, 255, 230))



    draw.text((pad, im.height - box_h + pad), text1, font=font, fill="black")

    draw.text((pad, im.height - box_h + pad + FONT_SIZE + 6), text2, font=font, fill="black")



    return im





# =========================

# Resource Helpers

# =========================

def get_chrome_process_count():

    count = 0



    try:

        current_user = os.getenv("USER")



        for proc in psutil.process_iter(["name", "username", "cmdline"]):

            try:

                name = (proc.info.get("name") or "").lower()

                username = proc.info.get("username") or ""

                cmdline = " ".join(proc.info.get("cmdline") or []).lower()



                if current_user and current_user not in username:

                    continue



                is_chrome = (

                    "chrome" in name

                    or "chromedriver" in name

                    or "google-chrome" in cmdline

                    or "chromedriver" in cmdline

                    or "crawler-chrome-" in cmdline

                )



                if is_chrome:

                    count += 1



            except Exception:

                pass



    except Exception:

        return -1



    return count





def log_resource_usage():

    chrome_count = get_chrome_process_count()

    log(

        f"[RESOURCE] chrome_processes={chrome_count}, "

        f"open_fds={get_open_fd_count()}, "

        f"fd_limit={get_fd_limit_text()}"

    )





# =========================

# Selenium Process Helpers

# =========================

def kill_process_tree(pid):

    try:

        parent = psutil.Process(pid)

        children = parent.children(recursive=True)



        for child in children:

            try:

                child.terminate()

            except Exception:

                pass



        try:

            parent.terminate()

        except Exception:

            pass



        gone, alive = psutil.wait_procs(children + [parent], timeout=5)



        for proc in alive:

            try:

                proc.kill()

            except Exception:

                pass



    except Exception:

        pass





def kill_chrome_processes():

    current_user = os.getenv("USER")



    try:

        for proc in psutil.process_iter(["pid", "name", "username", "cmdline"]):

            try:

                name = (proc.info.get("name") or "").lower()

                username = proc.info.get("username") or ""

                cmdline = " ".join(proc.info.get("cmdline") or []).lower()



                if current_user and current_user not in username:

                    continue



                is_chrome = (

                    "chrome" in name

                    or "chromedriver" in name

                    or "google-chrome" in cmdline

                    or "chromedriver" in cmdline

                    or "crawler-chrome-" in cmdline

                )



                if is_chrome:

                    try:

                        proc.terminate()

                    except Exception:

                        pass



            except Exception:

                pass



        time.sleep(3)



        for proc in psutil.process_iter(["pid", "name", "username", "cmdline"]):

            try:

                name = (proc.info.get("name") or "").lower()

                username = proc.info.get("username") or ""

                cmdline = " ".join(proc.info.get("cmdline") or []).lower()



                if current_user and current_user not in username:

                    continue



                is_chrome = (

                    "chrome" in name

                    or "chromedriver" in name

                    or "google-chrome" in cmdline

                    or "chromedriver" in cmdline

                    or "crawler-chrome-" in cmdline

                )



                if is_chrome:

                    try:

                        proc.kill()

                    except Exception:

                        pass



            except Exception:

                pass



    except Exception:

        pass





def cleanup_temp_profiles():

    try:

        for name in os.listdir("/tmp"):

            if name.startswith("crawler-chrome-"):

                path = os.path.join("/tmp", name)

                shutil.rmtree(path, ignore_errors=True)

    except Exception:

        pass





def hard_recovery_cleanup():

    log("[WARN] Running hard recovery cleanup...")



    kill_chrome_processes()

    cleanup_temp_profiles()

    reset_http_session()

    gc.collect()



    log_resource_usage()



    time.sleep(HARD_RECOVERY_SLEEP_SECONDS)





# =========================

# Selenium Driver Helpers

# =========================

def make_driver(max_retry=5):

    last_error = None



    for attempt in range(1, max_retry + 1):

        profile_dir = None



        try:

            profile_dir = tempfile.mkdtemp(prefix="crawler-chrome-")



            options = uc.ChromeOptions()



            options.add_argument("--headless=new")

            options.add_argument("--no-sandbox")

            options.add_argument("--disable-dev-shm-usage")

            options.add_argument("--ignore-certificate-errors")

            options.add_argument("--window-size=1366,2000")

            options.add_argument(f"--user-data-dir={profile_dir}")



            options.add_argument("--disable-gpu")

            options.add_argument("--disable-software-rasterizer")

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

            options.add_argument("--disable-component-update")

            options.add_argument("--disable-domain-reliability")

            options.add_argument("--metrics-recording-only")

            options.add_argument("--no-first-run")

            options.add_argument("--no-default-browser-check")

            options.add_argument("--password-store=basic")

            options.add_argument("--use-mock-keychain")

            options.add_argument("--mute-audio")

            options.add_argument("--remote-debugging-port=0")

            options.add_argument("--blink-settings=imagesEnabled=true")



            options.binary_location = "/usr/bin/google-chrome"

            options.page_load_strategy = "eager"



            driver = uc.Chrome(

                options=options,

                use_subprocess=True,

                browser_executable_path="/usr/bin/google-chrome",

            )



            driver.set_page_load_timeout(35)

            driver.set_script_timeout(20)



            driver._crawler_profile_dir = profile_dir

            driver._created_at = time.time()



            log("[INFO] Selenium driver started OK")

            return driver



        except Exception as e:

            last_error = e

            log(f"[WARN] Failed to start Chrome attempt {attempt}/{max_retry}: {e}")



            if profile_dir:

                shutil.rmtree(profile_dir, ignore_errors=True)



            if "too many open files" in str(e).lower() or getattr(e, "errno", None) == 24:

                log(

                    f"[WARN] Open FD pressure before retry: "

                    f"open_fds={get_open_fd_count()}, fd_limit={get_fd_limit_text()}"

                )

                reset_http_session()



            kill_chrome_processes()

            cleanup_temp_profiles()

            gc.collect()



            sleep_seconds = 5 + (attempt * 3)

            time.sleep(sleep_seconds)



    raise last_error





def safe_quit(driver):

    profile_dir = None

    service_pid = None



    try:

        if driver:

            profile_dir = getattr(driver, "_crawler_profile_dir", None)



            try:

                service = getattr(driver, "service", None)

                process = getattr(service, "process", None)



                if process:

                    service_pid = process.pid

            except Exception:

                service_pid = None



            try:

                driver.quit()

            except Exception:

                pass



    except Exception:

        pass

    finally:

        if service_pid:

            kill_process_tree(service_pid)



        if profile_dir:

            shutil.rmtree(profile_dir, ignore_errors=True)



        gc.collect()





def is_url_access_error(error):

    msg = str(error).lower()



    url_error_keywords = [

        "err_name_not_resolved", "err_connection_timed_out",

        "err_connection_refused", "err_connection_reset", "err_connection_closed",

        "err_address_unreachable", "err_network_changed",

        "err_internet_disconnected", "err_timed_out", "err_ssl_protocol_error",

        "err_ssl_version_or_cipher_mismatch", "err_cert_authority_invalid",

        "err_cert_common_name_invalid", "err_cert_date_invalid",

        "err_tunnel_connection_failed", "dns_probe_finished", "net::",

    ]



    return any(keyword in msg for keyword in url_error_keywords)





def is_driver_dead_error(error):

    msg = str(error).lower()



    fatal_keywords = [

        "connection refused", "max retries exceeded", "invalid session id",

        "chrome not reachable", "disconnected", "session deleted",

        "no such window", "target window already closed",

        "cannot determine loading status", "failed to establish a new connection",

        "connection aborted", "remote end closed connection", "read timed out",

        "tab crashed", "web view not found",

        "timeout: timed out receiving message from renderer",

        "remote end closed connection without response",

    ]



    return any(keyword in msg for keyword in fatal_keywords)





def restart_driver(driver, reason="-", hard=False):

    log(f"[WARN] Restarting Selenium driver. Reason: {reason}")



    safe_quit(driver)



    time.sleep(3)



    kill_chrome_processes()



    if hard:

        hard_recovery_cleanup()

    else:

        cleanup_temp_profiles()

        gc.collect()

        time.sleep(5)



    return make_driver(max_retry=5)





def recover_driver_and_continue(current_index):

    log(

        "[WARN] Too many consecutive driver restarts. "

        "Running hard recovery and continuing from next URL..."

    )



    log_resource_usage()

    hard_recovery_cleanup()



    try:

        new_driver = make_driver(max_retry=5)

        save_progress(current_index)

        return new_driver

    except Exception as recovery_error:

        log(f"[ERROR] Failed to create driver after hard recovery: {recovery_error}")

        save_progress(current_index)

        time.sleep(HARD_RECOVERY_SLEEP_SECONDS)

        return None





def driver_age_seconds(driver):

    try:

        return time.time() - getattr(driver, "_created_at", time.time())

    except Exception:

        return 0





def should_restart_by_age(driver, max_age_seconds=600):

    return driver_age_seconds(driver) >= max_age_seconds





def wait_page_loaded(driver, timeout=15):

    WebDriverWait(driver, timeout).until(

        lambda d: d.execute_script("return document.readyState") in ["interactive", "complete"]

    )



    WebDriverWait(driver, timeout).until(

        EC.presence_of_element_located((By.TAG_NAME, "body"))

    )





def stop_page_loading(driver):

    try:

        driver.execute_script("window.stop();")

    except Exception:

        pass





def selenium_get_with_scheme_fallback(driver, url):

    """

    Buka URL dengan Selenium.



    - URL tanpa scheme otomatis diberi https://

    - Kalau gagal, coba scheme kebalikannya.

      Contoh:

      ptberdikari.co.id -> https://ptberdikari.co.id -> http://ptberdikari.co.id

      http://example.id -> jika gagal coba https://example.id

    """

    active_url = ensure_url_scheme(url, default_scheme="https")



    try:

        driver.get(active_url)

        return active_url



    except TimeoutException as first_timeout:

        stop_page_loading(driver)

        alt_url = alternate_scheme_url(active_url)



        if alt_url and alt_url != active_url:

            log(f"[INFO] Selenium open timeout, retrying alternate scheme: {alt_url}")



            try:

                driver.get(alt_url)

                return alt_url

            except TimeoutException:

                log("[WARN] Alternate scheme page load timeout, stopping load and continuing")

                stop_page_loading(driver)

                return alt_url

            except WebDriverException as e2:

                log(f"[ERROR] Alternate scheme Selenium failed: {e2}")

                raise first_timeout



        raise



    except WebDriverException as first_error:

        alt_url = alternate_scheme_url(active_url)



        if alt_url and alt_url != active_url:

            log(f"[INFO] Selenium open failed, retrying alternate scheme: {alt_url}")



            try:

                driver.get(alt_url)

                return alt_url

            except TimeoutException:

                log("[WARN] Alternate scheme page load timeout, stopping load and continuing")

                stop_page_loading(driver)

                return alt_url

            except WebDriverException as e2:

                log(f"[ERROR] Alternate scheme Selenium failed: {e2}")

                raise first_error



        raise





def get_page_text(driver):

    html = driver.page_source



    soup = BeautifulSoup(html, "html.parser")



    for tag in soup(["script", "style", "noscript"]):

        tag.decompose()



    return soup.get_text(separator=" ", strip=True)





def wait_until_antibot_finished(driver, max_wait=None):

    if max_wait is None:

        max_wait = ANTIBOT_MAX_WAIT_SECONDS



    start = time.time()



    last_signature = None

    stable_count = 0



    while time.time() - start < max_wait:

        try:

            page_text = get_page_text(driver)

            page_title = driver.title or ""

            current_url = driver.current_url or ""



            captcha_hit = is_captcha_or_antibot_page(page_text, page_title)



            signature = (

                page_title,

                current_url,

                len(page_text or ""),

                (page_text or "")[:120],

            )



            if signature == last_signature:

                stable_count += 1

            else:

                stable_count = 0

                last_signature = signature



            log(

                f"[INFO] Waiting anti-bot: captcha={captcha_hit}, "

                f"title={page_title}, current_url={current_url}, "

                f"text_len={len(page_text or '')}, stable_count={stable_count}"

            )



            if not captcha_hit and len(page_text or "") >= 50:

                log("[INFO] Anti-bot page seems finished")

                return True



            if captcha_hit and stable_count >= ANTIBOT_MAX_STABLE_COUNT:

                log(

                    f"[WARN] Anti-bot page is stable for {stable_count} checks, "

                    "stop waiting early"

                )

                return False



            time.sleep(ANTIBOT_WAIT_INTERVAL_SECONDS)



        except Exception as e:

            log(f"[WARN] Failed while waiting anti-bot page: {e}")

            time.sleep(ANTIBOT_WAIT_INTERVAL_SECONDS)



    log("[WARN] Anti-bot/CAPTCHA wait timeout")

    return False





def fullpage_screenshot_png(driver):

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

        log(f"[WARN] CDP screenshot fail, fallback normal screenshot: {e}")

        return driver.get_screenshot_as_png()





def take_screenshot_png(driver):

    """

    Compatibility wrapper.



    Beberapa versi crawler memakai nama fullpage_screenshot_png(),

    sedangkan alert-safe wrapper memakai take_screenshot_png().

    """

    return fullpage_screenshot_png(driver)





# =========================

# Evidence Screenshot Helpers

# =========================

def inject_hidden_evidence_panel(driver, url, evidence_items):

    safe_items = evidence_items[:30]



    rows_html = ""



    for idx, item in enumerate(safe_items, 1):

        href = str(item.get("href", ""))[:500]

        anchor = str(item.get("anchor", ""))[:300]

        matched = ", ".join(item.get("matched_terms", []))[:200]



        rows_html += f"""

        <tr>

            <td style="border:1px solid #999;padding:6px;vertical-align:top;">{idx}</td>

            <td style="border:1px solid #999;padding:6px;vertical-align:top;word-break:break-all;">{escape_html(anchor)}</td>

            <td style="border:1px solid #999;padding:6px;vertical-align:top;word-break:break-all;">{escape_html(href)}</td>

            <td style="border:1px solid #999;padding:6px;vertical-align:top;">{escape_html(matched)}</td>

        </tr>

        """



    panel_html = f"""

    <div id="bima-hidden-evidence-panel" style="

        position: fixed;

        z-index: 2147483647;

        top: 20px;

        left: 20px;

        right: 20px;

        max-height: 85vh;

        overflow: auto;

        background: #fff3cd;

        color: #111;

        border: 4px solid #d00000;

        box-shadow: 0 0 20px rgba(0,0,0,0.45);

        padding: 16px;

        font-family: Arial, sans-serif;

        font-size: 14px;

        line-height: 1.4;

    ">

        <div style="font-size:22px;font-weight:bold;color:#b00000;margin-bottom:8px;">

            BIMA Evidence: Hidden Gambling Links Detected

        </div>

        <div style="margin-bottom:10px;">

            <b>URL:</b> {escape_html(url)}<br>

            <b>Total evidence shown:</b> {len(safe_items)}<br>

            <b>Reason:</b> Hidden HTML element contains gambling-related outbound links/anchors.

        </div>

        <table style="border-collapse:collapse;width:100%;background:white;color:#111;">

            <thead>

                <tr style="background:#ffd6d6;">

                    <th style="border:1px solid #999;padding:6px;">#</th>

                    <th style="border:1px solid #999;padding:6px;">Anchor Text</th>

                    <th style="border:1px solid #999;padding:6px;">Href</th>

                    <th style="border:1px solid #999;padding:6px;">Matched Terms</th>

                </tr>

            </thead>

            <tbody>

                {rows_html}

            </tbody>

        </table>

    </div>

    """



    driver.execute_script(

        """

        const oldPanel = document.getElementById('bima-hidden-evidence-panel');

        if (oldPanel) oldPanel.remove();



        const wrapper = document.createElement('div');

        wrapper.innerHTML = arguments[0];

        document.body.appendChild(wrapper.firstElementChild);



        window.scrollTo(0, 0);

        """,

        panel_html,

    )



    time.sleep(1)





# =========================

# Progress Helper

# =========================

def save_progress(index):

    try:

        with open(PROGRESS_FILE, "w") as f:

            f.write(str(index))



    except Exception as e:

        log(f"[WARN] Failed to save progress: {e}")





def load_progress():

    try:

        if os.path.exists(PROGRESS_FILE):

            with open(PROGRESS_FILE, "r") as f:

                value = f.read().strip()



                if value.isdigit():

                    return int(value)



    except Exception as e:

        log(f"[WARN] Failed to load progress: {e}")



    return 0





# =========================

# Sender

# Payload tidak diubah

# =========================

def send_alert(url, screenshot_png_b64, confidence):

    """

    Kirim alert ke endpoint.



    Fungsi ini sengaja tidak melempar exception secara default.

    Kalau endpoint alert error/down/timeout, crawler tetap lanjut ke URL berikutnya.

    """

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")



    safe_confidence = max(float(confidence or 0), float(MIN_ALERT_CONFIDENCE))

    confidence_level = int(round(safe_confidence * 100))



    if confidence_level <= 50:

        confidence_level = 60



    payload = {

        "url": url,

        "brand": "-",

        "discovered": timestamp,

        "confidence_level": confidence_level,

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

        "Connection": "close",

    }



    response = None



    try:

        if not is_id_tld_url(url):

            log(f"[WARN] Alert skipped: endpoint only accepts .id URL, url={url}")

            return False



        if not ENDPOINT_URL:

            log("[ERROR] Alert not sent: ENDPOINT_URL is empty")

            return False



        if not X_API_KEY:

            log("[WARN] X_API_KEY is empty; sending alert may be rejected")



        response = session_http.post(

            ENDPOINT_URL,

            headers=headers,

            json=payload,

            timeout=(ALERT_CONNECT_TIMEOUT, ALERT_READ_TIMEOUT),

        )



        response_text = response.text[:300]



        if response.status_code != 200:

            log(

                f"[ERROR] Alert send failed: status={response.status_code}, "

                f"body={response_text}"

            )

            return False



        log("[INFO] Alert sent OK")

        return True



    except UrlProcessingTimeout as e:

        log(f"[ERROR] Alert send interrupted by URL watchdog, skip alert and continue: {e}")

        if ALERT_RAISE_ON_FAILURE:

            raise

        return False



    except requests.exceptions.Timeout as e:

        log(f"[ERROR] Alert send timeout, skip alert and continue: {e}")

        if ALERT_RAISE_ON_FAILURE:

            raise

        return False



    except requests.RequestException as e:

        log(f"[ERROR] Alert send request failed, skip alert and continue: {e}")

        if "too many open files" in str(e).lower():

            reset_http_session()

        if ALERT_RAISE_ON_FAILURE:

            raise

        return False



    except OSError as e:

        if getattr(e, "errno", None) == 24 or "too many open files" in str(e).lower():

            log(

                f"[ERROR] Alert send hit too many open files: "

                f"open_fds={get_open_fd_count()}, fd_limit={get_fd_limit_text()}"

            )

            reset_http_session()



        if ALERT_RAISE_ON_FAILURE:

            raise

        return False



    except Exception as e:

        log(f"[ERROR] Alert send unexpected error, skip alert and continue: {e}")

        if ALERT_RAISE_ON_FAILURE:

            raise

        return False



    finally:

        if response is not None:

            try:

                response.close()

            except Exception:

                pass





def capture_and_send_alert(driver, url, confidence):

    """

    Ambil screenshot, beri watermark, lalu kirim alert.



    Semua error di screenshot/watermark/upload ditangkap agar crawler tidak stop.

    """

    try:

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



        return send_alert(url, img_str, confidence)



    except UrlProcessingTimeout as e:

        log(f"[ERROR] Alert screenshot/send interrupted by URL watchdog, skip alert and continue: {e}")

        if ALERT_RAISE_ON_FAILURE:

            raise

        return False



    except Exception as e:

        log(f"[ERROR] Failed to capture/send alert, skip alert and continue: {e}")

        if ALERT_RAISE_ON_FAILURE:

            raise

        return False





def capture_and_send_hidden_evidence_alert(driver, url, confidence, evidence_items):

    """

    Inject evidence panel lalu kirim alert.



    Error inject/screenshot/send tidak boleh menghentikan crawler.

    """

    try:

        inject_hidden_evidence_panel(

            driver=driver,

            url=url,

            evidence_items=evidence_items,

        )



        return capture_and_send_alert(

            driver=driver,

            url=url,

            confidence=confidence,

        )



    except UrlProcessingTimeout as e:

        log(f"[ERROR] Hidden evidence alert interrupted by URL watchdog, skip alert and continue: {e}")

        if ALERT_RAISE_ON_FAILURE:

            raise

        return False



    except Exception as e:

        log(f"[ERROR] Failed to capture/send hidden evidence alert, skip alert and continue: {e}")

        if ALERT_RAISE_ON_FAILURE:

            raise

        return False





# =========================

# Process Single URL

# =========================

def process_url(driver, url):

    log(f"[DEBUG] Start process_url: {url}")



    original_url = ensure_url_scheme(url, default_scheme="https")



    cached_record = get_non_abuse_cache_record(original_url)



    if cached_record:

        log(

            f"[INFO] Skip already checked non-abuse URL: {original_url}, "

            f"reason={cached_record.get('reason')}, checked_at={cached_record.get('checked_at')}"

        )

        return



    active_url = original_url

    report_url = choose_alert_url_for_id_endpoint(original_url, active_url)



    if ENABLE_DNS_PRECHECK:

        log(f"[DEBUG] DNS check start: {active_url}")

        if not is_dns_resolvable(active_url, timeout=DNS_CHECK_TIMEOUT_SECONDS):

            log(f"[INFO] DNS not resolved, skip without requests/Selenium: {active_url}")

            return

        log(f"[DEBUG] DNS check OK: {active_url}")



    log(f"[DEBUG] requests fetch start: {active_url}")

    pre = fetch_html_with_requests(active_url)

    log(f"[DEBUG] requests fetch done: {active_url}")



    if not pre["ok"]:

        alt_url = alternate_scheme_url(active_url)



        if alt_url and alt_url != active_url:

            log(f"[INFO] requests fetch failed, retrying alternate scheme: {alt_url}")

            alt_pre = fetch_html_with_requests(alt_url)



            if alt_pre["ok"]:

                log(f"[INFO] Alternate scheme requests fallback OK: {alt_url}")

                active_url = alt_url

                pre = alt_pre

            else:

                log(f"[INFO] Alternate scheme requests fallback also failed: {alt_url} - {alt_pre.get('error')}")



    if pre["ok"]:

        pre_final_url = pre.get("final_url") or active_url

        if pre_final_url:

            active_url = pre_final_url



        report_url = choose_alert_url_for_id_endpoint(

            original_url=locals().get('original_url', url),

            current_url=active_url,

            final_url=pre_final_url,

        )



    if not pre["ok"] and pre.get("dns_error"):

        log("[INFO] DNS error from requests, skip Selenium")

        return



    if pre["ok"]:

        pre_text = pre["text"]

        pre_title = pre["title"]

        pre_final_url = pre["final_url"]



        pre_captcha = is_captcha_or_antibot_page(pre_text, pre_title)



        log(

            f"[DEBUG] requests_fetch status={pre['status_code']}, "

            f"captcha={pre_captcha}, title={pre_title}, "

            f"final_url={pre_final_url}, text_len={len(pre_text or '')}"

        )



        if pre.get("hidden_gambling_hit"):

            evidence_items = pre.get("hidden_gambling_evidence") or []



            log(

                f"[INFO] Hidden gambling links detected from requests HTML, "

                f"hits={pre.get('hidden_gambling_hits')}, "

                f"evidence_count={len(evidence_items)}"

            )



            log(f"[DEBUG] Selenium open for hidden evidence screenshot: {active_url}")

            active_url = selenium_get_with_scheme_fallback(driver, active_url)



            try:

                wait_page_loaded(driver, timeout=15)

            except TimeoutException:

                stop_page_loading(driver)



            alert_url = choose_alert_url_for_id_endpoint(original_url, active_url)



            if not alert_url:

                log(f"[WARN] Skip alert because no .id URL candidate. original={locals().get('original_url', url)}, active={active_url}")

                return



            capture_and_send_hidden_evidence_alert(

                driver=driver,

                url=alert_url,

                confidence=0.60,

                evidence_items=evidence_items,

            )



            return



        if pre_text and len(pre_text) >= 50 and not pre_captcha:

            result = classify_text_content(

                source="requests",

                url=active_url,

                page_text=pre_text,

                page_title=pre_title,

                current_url=pre_final_url,
                page_html=pre.get("html"),

            )



            if result["status"] in ["high_risk", "medium_risk"]:

                log(

                    f"[INFO] Gambling signal detected from requests HTML, "

                    f"status={result['status']}, reason={result['reason']}, continue Selenium screenshot"

                )



                log(f"[DEBUG] Selenium open for normal alert screenshot: {active_url}")

                active_url = selenium_get_with_scheme_fallback(driver, active_url)



                try:

                    wait_page_loaded(driver, timeout=15)

                except TimeoutException:

                    stop_page_loading(driver)



                alert_url = choose_alert_url_for_id_endpoint(original_url, active_url)



                if not alert_url:

                    log(f"[WARN] Skip alert because no .id URL candidate. original={locals().get('original_url', url)}, active={active_url}")

                    return



                capture_and_send_alert(

                    driver=driver,

                    url=alert_url,

                    confidence=max(result["ml_conf"], MIN_ALERT_CONFIDENCE),

                )



                return



            log("[INFO] Requests HTML not high/medium risk, continue Selenium if needed")



    log(f"[DEBUG] Selenium direct open start: {active_url}")

    active_url = selenium_get_with_scheme_fallback(driver, active_url)

    log(f"[DEBUG] Selenium direct open done: {active_url}")



    try:

        wait_page_loaded(driver, timeout=15)

    except TimeoutException:

        log("[WARN] Page not fully loaded, stopping load and continuing with available HTML")

        stop_page_loading(driver)



    page_text = get_page_text(driver)

    page_title = driver.title or ""

    current_url = driver.current_url or active_url



    captcha_hit = is_captcha_or_antibot_page(page_text, page_title)



    if captcha_hit:

        log("[INFO] CAPTCHA/anti-bot detected, waiting for automatic verification")



        finished = wait_until_antibot_finished(

            driver,

            max_wait=ANTIBOT_MAX_WAIT_SECONDS,

        )



        page_text = get_page_text(driver)

        page_title = driver.title or ""

        current_url = driver.current_url or active_url

        captcha_hit = is_captcha_or_antibot_page(page_text, page_title)



        if not finished or captcha_hit:

            sample_text = (page_text or "")[:300]



            log(

                f"[INFO] CAPTCHA/anti-bot still active, skip alert because original content is not accessible, "

                f"title={page_title}, current_url={current_url}, text_len={len(page_text or '')}"

            )

            log(f"[DEBUG] sample_text={sample_text}")



            return



    if not page_text or len(page_text) < 30:

        log(

            f"[WARN] Empty or very short page text, "

            f"title={page_title}, current_url={current_url}, "

            f"text_len={len(page_text or '')}"

        )

        log("[INFO] Empty text, skip alert")

        return



    result = classify_text_content(

        source="selenium",

        url=active_url,

        page_text=page_text,

        page_title=page_title,

        current_url=current_url,
        page_html=lambda: driver.page_source,

    )



    if result["status"] in ["low_risk", "blocked_or_captcha"]:

        log("[INFO] Not high/medium risk gambling, skip alert")



        if result["status"] == "low_risk":

            save_non_abuse_checked_url(

                url=original_url,

                final_url=current_url or active_url,

                reason=result.get("reason", "low_risk"),

                source="selenium",

            )



        return



    if result["status"] == "medium_risk":

        log(f"[INFO] Medium risk detected, reason={result['reason']}, continue screenshot and alert")



    if result["status"] == "high_risk":

        log(f"[INFO] High risk gambling detected, reason={result['reason']}, continue screenshot and alert")



    alert_url = choose_alert_url_for_id_endpoint(

        original_url=locals().get('original_url', url),

        current_url=current_url or active_url,

    )



    if not alert_url:

        log(f"[WARN] Skip alert because no .id URL candidate. original={locals().get('original_url', url)}, current={current_url}, active={active_url}")

        return



    capture_and_send_alert(

        driver=driver,

        url=alert_url,

        confidence=max(result["ml_conf"], MIN_ALERT_CONFIDENCE),

    )





def retry_current_url_after_driver_restart(driver, url, index, reason, hard=False):

    """

    Restart Selenium driver lalu retry URL yang sama sekali lagi.



    Tujuannya: kalau requests sudah mendeteksi judol, tapi Selenium driver mati

    saat buka halaman untuk screenshot, URL tidak langsung ter-skip.

    """

    new_driver = driver



    try:

        new_driver = restart_driver(

            driver,

            reason=reason,

            hard=hard,

        )



        log(f"[INFO] Retrying same URL after Selenium driver restart: {url}")



        with url_timeout(MAX_SECONDS_PER_URL):

            process_url(new_driver, url)



        save_progress(index)

        return new_driver, True



    except UrlProcessingTimeout:

        log(f"[ERROR] Retry URL timeout after driver restart: {url}")

        log("[INFO] Skip current URL and continue to next domain")

        save_progress(index)

        return new_driver, False



    except TimeoutException as e:

        log(f"[ERROR] Retry URL timeout after driver restart: {e}")

        save_progress(index)



        try:

            stop_page_loading(new_driver)

        except Exception:

            pass



        return new_driver, False



    except Exception as e:

        log(f"[ERROR] Retry after Selenium driver restart failed, skip current URL: {e}")

        save_progress(index)

        return new_driver, False









# =========================

# Crawl URL list

# =========================

def crawl_urls(urls):

    start_index = load_progress()



    if start_index > 0:

        log(f"[INFO] Resume from index {start_index + 1}")



    try:

        cleanup_temp_profiles()

        kill_chrome_processes()

        gc.collect()

        time.sleep(5)



        driver = make_driver(max_retry=5)



    except Exception as e:

        log(f"[WARN] Initial Selenium driver failed: {e}")

        log_resource_usage()

        hard_recovery_cleanup()



        try:

            driver = make_driver(max_retry=5)

        except Exception as e2:

            log(f"[FATAL] Failed to initialize Selenium driver after hard recovery: {e2}")

            return



    consecutive_driver_errors = 0

    consecutive_timeouts = 0

    consecutive_restarts = 0



    try:

        for i, url in enumerate(urls[start_index:], start_index + 1):

            log(f"[INFO] [{i}/{len(urls)}] {url}")



            if i % 10 == 0:

                log_resource_usage()



            if i > 1 and i % RESTART_DRIVER_EVERY == 0:

                try:

                    driver = restart_driver(

                        driver,

                        reason=f"periodic restart every {RESTART_DRIVER_EVERY} urls",

                        hard=False,

                    )



                    consecutive_restarts = 0

                    consecutive_driver_errors = 0

                    consecutive_timeouts = 0



                except Exception as e:

                    log(f"[ERROR] Failed to periodic restart driver: {e}")



                    new_driver = recover_driver_and_continue(i)



                    if new_driver is None:

                        continue



                    driver = new_driver

                    consecutive_restarts = 0

                    consecutive_driver_errors = 0

                    consecutive_timeouts = 0

                    continue



            if should_restart_by_age(

                driver,

                max_age_seconds=RESTART_DRIVER_MAX_AGE_SECONDS,

            ):

                try:

                    driver = restart_driver(

                        driver,

                        reason=f"driver age exceeded {RESTART_DRIVER_MAX_AGE_SECONDS} seconds",

                        hard=False,

                    )



                    consecutive_restarts = 0

                    consecutive_driver_errors = 0

                    consecutive_timeouts = 0



                except Exception as e:

                    log(f"[ERROR] Failed to restart old driver: {e}")



                    new_driver = recover_driver_and_continue(i)



                    if new_driver is None:

                        continue



                    driver = new_driver

                    consecutive_restarts = 0

                    consecutive_driver_errors = 0

                    consecutive_timeouts = 0

                    continue



            try:

                with url_timeout(MAX_SECONDS_PER_URL):

                    process_url(driver, url)



                consecutive_driver_errors = 0

                consecutive_timeouts = 0

                consecutive_restarts = 0



                save_progress(i)



            except UrlProcessingTimeout:

                log(f"[ERROR] URL processing timeout after {MAX_SECONDS_PER_URL}s: {url}")

                log("[INFO] Skip current URL and continue to next domain")



                save_progress(i)



                try:

                    stop_page_loading(driver)

                except Exception:

                    pass



                consecutive_timeouts += 1



                if consecutive_timeouts >= 3:

                    try:

                        driver = restart_driver(

                            driver,

                            reason=f"{consecutive_timeouts} consecutive URL processing timeouts",

                            hard=False,

                        )



                        consecutive_timeouts = 0

                        consecutive_driver_errors = 0

                        consecutive_restarts = 0



                    except Exception as restart_error:

                        log(f"[ERROR] Failed to restart driver after URL timeout: {restart_error}")



                        new_driver = recover_driver_and_continue(i)



                        if new_driver is not None:

                            driver = new_driver

                            consecutive_timeouts = 0

                            consecutive_driver_errors = 0

                            consecutive_restarts = 0



                continue



            except TimeoutException as e:

                log(f"[ERROR] Timeout URL fail: {e}")

                log("[INFO] Timeout treated as URL-level failure, skip without restarting driver")



                consecutive_timeouts += 1

                save_progress(i)



                stop_page_loading(driver)



                continue



            except WebDriverException as e:

                log(f"[ERROR] Selenium URL fail: {e}")



                save_progress(i)



                if is_url_access_error(e):

                    log("[INFO] URL access error, skip without restarting driver")

                    consecutive_driver_errors = 0

                    consecutive_timeouts = 0

                    continue



                consecutive_driver_errors += 1



                if is_driver_dead_error(e) or consecutive_driver_errors >= 2:

                    if consecutive_restarts >= MAX_CONSECUTIVE_RESTARTS:

                        new_driver = recover_driver_and_continue(i)



                        if new_driver is None:

                            consecutive_restarts = 0

                            consecutive_driver_errors = 0

                            consecutive_timeouts = 0

                            continue



                        driver = new_driver

                        consecutive_restarts = 0

                        consecutive_driver_errors = 0

                        consecutive_timeouts = 0

                        continue



                    try:

                        hard = consecutive_restarts >= 2



                        driver, retry_ok = retry_current_url_after_driver_restart(

                            driver=driver,

                            url=url,

                            index=i,

                            reason="selenium driver dead/error",

                            hard=hard,

                        )



                        consecutive_restarts += 1

                        consecutive_driver_errors = 0

                        consecutive_timeouts = 0



                    except Exception as restart_error:

                        log(f"[ERROR] Failed to recover driver: {restart_error}")



                        new_driver = recover_driver_and_continue(i)



                        if new_driver is None:

                            continue



                        driver = new_driver

                        consecutive_restarts = 0

                        consecutive_driver_errors = 0

                        consecutive_timeouts = 0



                continue



            except Exception as e:

                log(f"[ERROR] URL fail: {e}")



                save_progress(i)



                if is_url_access_error(e):

                    log("[INFO] URL access error, skip without restarting driver")

                    consecutive_driver_errors = 0

                    consecutive_timeouts = 0

                    continue



                if is_driver_dead_error(e):

                    consecutive_driver_errors += 1



                    if consecutive_restarts >= MAX_CONSECUTIVE_RESTARTS:

                        new_driver = recover_driver_and_continue(i)



                        if new_driver is None:

                            consecutive_restarts = 0

                            consecutive_driver_errors = 0

                            consecutive_timeouts = 0

                            continue



                        driver = new_driver

                        consecutive_restarts = 0

                        consecutive_driver_errors = 0

                        consecutive_timeouts = 0

                        continue



                    try:

                        hard = consecutive_restarts >= 2



                        driver, retry_ok = retry_current_url_after_driver_restart(

                            driver=driver,

                            url=url,

                            index=i,

                            reason="general fatal driver error",

                            hard=hard,

                        )



                        consecutive_restarts += 1

                        consecutive_driver_errors = 0

                        consecutive_timeouts = 0



                    except Exception as restart_error:

                        log(f"[ERROR] Failed to recover driver: {restart_error}")



                        new_driver = recover_driver_and_continue(i)



                        if new_driver is None:

                            continue



                        driver = new_driver

                        consecutive_restarts = 0

                        consecutive_driver_errors = 0

                        consecutive_timeouts = 0



                continue



    finally:

        safe_quit(driver)





# =========================

# Main

# =========================

if __name__ == "__main__":

    load_non_abuse_cache()

    crawl_urls(urls_all)