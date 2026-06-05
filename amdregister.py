#!/usr/bin/env python3
"""
AMD Developer Cloud — Automated Registration & Credit Request Pipeline
======================================================================

Pipeline Steps:
    1. Register account on www.amd.com (CloakBrowser + proxy)
    2. Fetch activation token from email (IMAP polling)
    3. Activate account with token + set password (CloakBrowser)
    4. Login via Okta PKCE flow → obtain Bearer token (HTTP)
    5. Submit free-credit request via Marketo form (CloakBrowser)

Usage:
    python3 amdregister.py --count 5
    python3 amdregister.py --email user@domain.com --name "John Doe" --company "MIT"
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import codecs
import email as email_lib
import hashlib
import html as html_lib
import imaplib
import json
import logging
import os
import random
import re
import string
import sys
import time
import urllib.parse
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

import requests
import cloakbrowser


# ─────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
SUCCESS_FILE = BASE_DIR / "success.txt"
PLUS_COUNTER_FILE = BASE_DIR / ".plus_counter"
LOG_FILE = DATA_DIR / "run.log"


# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────
logger = logging.getLogger("amd")
logger.setLevel(logging.DEBUG)
_console = logging.StreamHandler(sys.stdout)
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("  [%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_console)
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
logger.addHandler(_fh)

_ICON_LEVELS = {"❌": logging.ERROR, "⚠️": logging.WARNING}


def log(msg: str, icon: str = "•") -> None:
    """Emit a log message with an optional icon prefix."""
    level = _ICON_LEVELS.get(icon, logging.INFO)
    logger.log(level, f"{icon} {msg}")


# ─────────────────────────────────────────────────────────────
# Config loading & validation
# ─────────────────────────────────────────────────────────────
_REQUIRED_CONFIG_KEYS = ["password", "imap_host", "imap_user", "imap_password"]


def load_config(path: Path) -> dict[str, Any]:
    """Load and validate ``config.json``."""
    if not path.exists():
        sys.exit(f"ERROR: Config file not found: {path}\nCopy config.example.json → config.json and fill in values.")
    with open(path) as fh:
        cfg = json.load(fh)
    missing = [k for k in _REQUIRED_CONFIG_KEYS if not cfg.get(k)]
    if missing:
        sys.exit(f"ERROR: Missing required config keys: {', '.join(missing)}")
    return cfg


CFG = load_config(CONFIG_FILE)
PASSWORD: str = CFG["password"]
IMAP_HOST: str = CFG["imap_host"]
IMAP_USER: str = CFG["imap_user"]
IMAP_PW: str = CFG["imap_password"]
PROXY_LIST: list[dict] = CFG.get("proxy_list", [])
GMAIL_PLUS_BASE: str = CFG.get("gmail_plus_base", "")
EMAIL_DOMAIN: str = CFG.get("email_domain", "")


# ─────────────────────────────────────────────────────────────
# AMD / Okta Constants
# ─────────────────────────────────────────────────────────────
OKTA_BASE = "https://login.amd.com"
DEV_AMD = "https://developer.amd.com"
OKTA_CLIENT_ID = "0oa10nnl4wplbzM16698"
OKTA_REDIRECT_URI = "https://developer.amd.com/auth/callback"
OKTA_EMAIL_AUTH_ID = "aut1uh73i3v040uEU697"

REGISTER_URL = "https://www.amd.com/en/registration/ai-dev-program-sign-up-form.html"
CUSTTARG = "aHR0cHM6Ly9kZXZlbG9wZXIuYW1kLmNvbT9SZWxheVN0YXRlPQ=="
ACTIVATE_URL = "https://www.amd.com/en/registration/activate-account.html"
CREDIT_FORM_URL = "https://anchor.digitalocean.com/amd-cloud-free-credit.html"

OKTA_HEADERS = {
    "accept": "application/json; okta-version=1.0.0",
    "content-type": "application/json",
    "x-okta-user-agent-extended": "okta-auth-js/6.9.0 okta-signin-widget-6.9.0 okta-hosted",
}


# ─────────────────────────────────────────────────────────────
# Timeouts (seconds / milliseconds where noted)
# ─────────────────────────────────────────────────────────────
PAGE_LOAD_TIMEOUT_MS = 60_000
MARKETO_READY_TIMEOUT_MS = 30_000
REDIRECT_TIMEOUT_MS = 30_000
TOKEN_WAIT_TIMEOUT = 90          # seconds — IMAP poll for activation token
OTP_WAIT_TIMEOUT = 120           # seconds — IMAP poll for OTP
IMAP_POLL_INTERVAL = 10          # seconds
OTP_POLL_INTERVAL = 5            # seconds
REGISTER_POLL_ROUNDS = 8         # rounds × 5s each
ACTIVATE_POLL_ROUNDS = 10


# ─────────────────────────────────────────────────────────────
# Browser Fingerprints
# ─────────────────────────────────────────────────────────────
FINGERPRINTS = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0", "platform": "Windows"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36", "platform": "macOS"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36", "platform": "Windows"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36", "platform": "Linux"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0", "platform": "Windows"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15", "platform": "macOS"},
]


# ─────────────────────────────────────────────────────────────
# Persona Data
# ─────────────────────────────────────────────────────────────
EMAIL_DOMAINS = ["richardsheingold.com"]

NAMES: list[tuple[str, str, str, str]] = [
    # Asia
    ("Hiroshi", "Tanaka", "JP", "University of Tokyo"),
    ("Yuki", "Watanabe", "JP", "Kyoto University"),
    ("Wei", "Chen", "CN", "Tsinghua University"),
    ("Li", "Zhang", "CN", "Peking University"),
    ("Priya", "Sharma", "IN", "IIT Bombay"),
    ("Ravi", "Kumar", "IN", "IISc Bangalore"),
    ("Min-Jun", "Kim", "KR", "KAIST"),
    ("Ji-Hoon", "Lee", "KR", "Seoul National University"),
    ("Putu", "Wijaya", "ID", "ITB Bandung"),
    ("Made", "Suryana", "ID", "Universitas Indonesia"),
    ("Budi", "Santoso", "ID", "UGM Yogyakarta"),
    ("Rizki", "Pratama", "ID", "ITS Surabaya"),
    ("Dewi", "Lestari", "ID", "Universitas Gadjah Mada"),
    ("Siti", "Rahmawati", "ID", "UI Jakarta"),
    ("Ahmad", "Hidayat", "ID", "ITB Bandung"),
    ("Thanh", "Nguyen", "VN", "VNU Hanoi"),
    # Europe
    ("Erik", "Hansen", "DK", "Technical University of Denmark"),
    ("Lars", "Eriksson", "SE", "KTH Royal Institute"),
    ("Henrik", "Johansson", "SE", "Uppsala University"),
    ("Sophie", "Dubois", "FR", "Sorbonne University"),
    ("Marco", "Bianchi", "IT", "Politecnico di Milano"),
    ("Lena", "Muller", "DE", "TU Munich"),
    ("Felix", "Schmidt", "DE", "RWTH Aachen"),
    ("Pablo", "Garcia", "ES", "Universidad Complutense"),
    ("Emma", "Wilson", "GB", "University of Oxford"),
    ("James", "Thompson", "GB", "Imperial College London"),
    ("Isabella", "De Jong", "NL", "TU Delft"),
    ("Viktor", "Petrov", "RU", "Skoltech"),
    ("Stefan", "Kowalski", "PL", "University of Warsaw"),
    ("Nikolai", "Andersen", "FI", "Aalto University"),
    # Africa / Middle-East
    ("Ahmed", "Mansour", "EG", "Cairo University"),
    ("Fatima", "Zahra", "EG", "AUC"),
    ("Ali", "Reza", "SA", "KAUST"),
    ("Aisha", "Okafor", "NG", "University of Lagos"),
    ("Lerato", "Ndlovu", "ZA", "University of Cape Town"),
    ("Samir", "Benali", "MA", "Mohammed V University"),
    # Americas
    ("Michael", "Johnson", "US", "MIT"),
    ("Sarah", "Williams", "US", "Stanford University"),
    ("David", "Martinez", "US", "Carnegie Mellon University"),
    ("Roberto", "Ferreira", "BR", "UNICAMP"),
    ("Sofia", "Mendez", "MX", "UNAM"),
    ("Chloe", "Tremblay", "CA", "University of Toronto"),
]

COUNTRY_LABELS: dict[str, str] = {
    "JP": "Japan", "CN": "China", "IN": "India", "KR": "Korea, Republic of",
    "ID": "Indonesia", "VN": "Vietnam", "DK": "Denmark", "SE": "Sweden",
    "FR": "France", "IT": "Italy", "DE": "Germany", "ES": "Spain",
    "GB": "United Kingdom", "NL": "Netherlands", "RU": "Russian Federation",
    "PL": "Poland", "FI": "Finland", "EG": "Egypt", "SA": "Saudi Arabia",
    "NG": "Nigeria", "ZA": "South Africa", "MA": "Morocco", "US": "United States",
    "BR": "Brazil", "MX": "Mexico", "CA": "Canada",
}

USE_CASES: list[str] = [
    "I am building an AI-powered medical imaging diagnostic tool. We need GPU compute to train and serve our models.",
    "Working on large language model fine-tuning for low-resource languages. Need AMD GPU access to evaluate ROCm compatibility.",
    "Developing open-source computer vision framework for autonomous navigation. Testing inference performance on AMD hardware.",
    "Building a real-time speech-to-text system for accessibility. Need GPU cloud for model serving and benchmarking.",
    "Research on diffusion models for scientific simulation. Evaluating AMD GPU performance for our training pipeline.",
    "Creating an AI-powered code review tool. Need GPU compute for running inference on large code models.",
    "Working on multimodal AI research. Testing AMD ROCm support for our custom kernels.",
    "Building a recommendation engine. Need GPU cloud for training and A/B testing different architectures.",
    "Developing NLP tools for sentiment analysis. Evaluating AMD cloud for batch inference workloads.",
    "Research on federated learning. Testing AMD GPU compatibility with our federated framework.",
]

OUTCOMES: list[str] = [
    "Evaluate AMD GPU performance for training and inference workloads",
    "Benchmark AMD ROCm against CUDA for our deep learning pipeline",
    "Deploy production AI models on AMD cloud infrastructure",
    "Test compatibility of our ML framework with AMD GPUs",
    "Migrate AI workloads from NVIDIA to AMD ecosystem",
    "Validate AMD GPU cloud for research computing needs",
]


# ═════════════════════════════════════════════════════════════
# Helpers — Crypto / PKCE
# ═════════════════════════════════════════════════════════════

def generate_code_verifier(length: int = 64) -> str:
    """RFC 7636 code verifier for PKCE."""
    return "".join(random.choices(string.ascii_letters + string.digits + "-._~", k=length))


def generate_code_challenge(verifier: str) -> str:
    """S256 code challenge from verifier."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def generate_random_id(length: int = 16) -> str:
    """Random lowercase alphanumeric identifier (state / nonce)."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def find_state_token(html_body: str) -> Optional[str]:
    """Extract Okta stateToken from rendered HTML."""
    idx = html_body.find('"stateToken":"')
    if idx < 0:
        return None
    start = html_body.index('"', html_body.index(":", idx)) + 1
    i = start
    while i < len(html_body):
        if html_body[i] == "\\":
            i += 2
            continue
        if html_body[i] == '"':
            break
        i += 1
    return codecs.decode(html_body[start:i], "unicode_escape")


# ═════════════════════════════════════════════════════════════
# Helpers — Proxy
# ═════════════════════════════════════════════════════════════

def pick_proxy() -> Optional[dict[str, Any]]:
    """Select a random proxy from the configured list.

    Returns a dict with ``playwright``, ``requests``, and ``display`` keys,
    or ``None`` when no proxies are configured.
    """
    if not PROXY_LIST:
        return None
    p = random.choice(PROXY_LIST)
    return {
        "playwright": {
            "server": f"http://{p['host']}:{p['port']}",
            "username": p["user"],
            "password": p["pass"],
        },
        "requests": {
            "http": f"http://{p['user']}:{p['pass']}@{p['host']}:{p['port']}",
            "https": f"http://{p['user']}:{p['pass']}@{p['host']}:{p['port']}",
        },
        "display": f"{p['host']}:{p['port']}",
    }


# ═════════════════════════════════════════════════════════════
# Helpers — Gmail Dot Trick
# ═════════════════════════════════════════════════════════════

def _next_plus_counter() -> int:
    """Increment and return the persistent sequential counter for email variants."""
    counter = 1
    if PLUS_COUNTER_FILE.exists():
        try:
            counter = int(PLUS_COUNTER_FILE.read_text().strip()) + 1
        except (ValueError, OSError):
            counter = 1
    PLUS_COUNTER_FILE.write_text(str(counter))
    return counter


def gmail_dot_variant(base: str, index: int) -> Optional[str]:
    """Generate a unique Gmail dot variant from *base* username.

    Gmail ignores dots in the local part, so ``a.ngelabriptu`` delivers to the
    same inbox as ``angelabriptu``.  AMD treats them as distinct addresses.

    The *index* is interpreted as a bitmask controlling dot placement between
    each pair of characters, yielding ``2^(len(base)-1) - 1`` unique variants.
    """
    if index <= 0:
        return base
    positions = len(base) - 1
    if index >= (2 ** positions):
        return None
    result = [base[0]]
    for i in range(1, len(base)):
        if index & (1 << (i - 1)):
            result.append(".")
        result.append(base[i])
    return "".join(result)


# ═════════════════════════════════════════════════════════════
# Helpers — IMAP
# ═════════════════════════════════════════════════════════════

@contextmanager
def imap_connection() -> Iterator[imaplib.IMAP4_SSL]:
    """Context manager for a connected, authenticated IMAP session."""
    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        mail.login(IMAP_USER, IMAP_PW)
        mail.select("INBOX")
        yield mail
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def extract_body(msg: email_lib.message.Message, prefer: tuple[str, ...] = ("text/html", "text/plain")) -> str:
    """Return the decoded body of *msg*, preferring content-types in *prefer* order."""
    if msg.is_multipart():
        found: dict[str, str] = {}
        for part in msg.walk():
            ct = part.get_content_type()
            if ct in prefer and ct not in found:
                try:
                    found[ct] = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except Exception:
                    pass
        for ct in prefer:
            if ct in found:
                return found[ct]
        return ""
    try:
        return msg.get_payload(decode=True).decode("utf-8", errors="ignore")
    except Exception:
        return ""


# ═════════════════════════════════════════════════════════════
# Helpers — Person Generation
# ═════════════════════════════════════════════════════════════

def generate_person() -> dict[str, str]:
    """Build a randomized persona for registration."""
    first, last, country, company = random.choice(NAMES)
    num = random.randint(10, 99)
    github = f"{first.lower()}{last.lower()}{num}"

    if GMAIL_PLUS_BASE:
        seq = _next_plus_counter()
        variant = gmail_dot_variant(GMAIL_PLUS_BASE, seq)
        if variant is None:
            log(f"Exhausted all dot variants for {GMAIL_PLUS_BASE} — falling back to + trick", "⚠️")
            variant = f"{GMAIL_PLUS_BASE}+amd{seq:03d}"
        email_addr = f"{variant}@{EMAIL_DOMAIN}"
        log(f"Email (dot variant #{seq}): {email_addr}", "📧")
    else:
        domain = random.choice(EMAIL_DOMAINS)
        email_addr = f"{first.lower()}.{last.lower()}{num}@{domain}"

    return {
        "first": first,
        "last": last,
        "email": email_addr,
        "github": github,
        "company": company,
        "country_code": country,
        "country_label": COUNTRY_LABELS.get(country, "United States"),
        "use_case": random.choice(USE_CASES),
        "outcome": random.choice(OUTCOMES),
    }


# ═════════════════════════════════════════════════════════════
# Step 1 — Register (CloakBrowser)
# ═════════════════════════════════════════════════════════════

# AMD registration form element IDs
_REG_FIRST_NAME = "#form-text-1444782869"
_REG_LAST_NAME = "#form-text-1891447162"
_REG_EMAIL = "#form-text-1830320319"
_REG_COMPANY = "#form-text-417351009"
_REG_COUNTRY = "#country-dropdown-1299606956"
_REG_LANGUAGE = "#language-dropdown-765462299"
_REG_SUBMIT = "form-button-1857186030"


async def _fill_registration_form(page: Any, person: dict[str, str]) -> None:
    """Populate all fields of the AMD registration form."""
    await page.fill(_REG_FIRST_NAME, person["first"])
    await page.fill(_REG_LAST_NAME, person["last"])
    await page.fill(_REG_EMAIL, person["email"])
    await page.fill(_REG_COMPANY, person["company"])
    await page.select_option(_REG_COUNTRY, label=person["country_label"])
    try:
        await page.select_option(_REG_LANGUAGE, label="English")
    except Exception:
        logger.debug("Language dropdown not available — skipping")
    await page.evaluate(
        '() => document.querySelectorAll("#new_form input[type=checkbox]")'
        ".forEach(cb => { if(!cb.checked) cb.click(); })"
    )
    await page.wait_for_timeout(1000)


async def _click_register_submit(page: Any) -> None:
    """Click the registration form submit button."""
    await page.evaluate(
        f'() => document.getElementById("{_REG_SUBMIT}").click()'
    )


async def step1_register(page: Any, person: dict[str, str]) -> bool:
    """Step 1: Register a new AMD account.

    Returns ``True`` when the page redirects to the activation URL.
    """
    log(f"Registering: {person['email']}")

    await page.goto(
        f"{REGISTER_URL}?custtarg={CUSTTARG}",
        wait_until="domcontentloaded",
        timeout=PAGE_LOAD_TIMEOUT_MS,
    )
    await page.wait_for_timeout(5000)

    # Dismiss cookie banner
    await page.evaluate(
        '() => { const b = document.getElementById("onetrust-accept-btn-handler"); if(b) b.click(); }'
    )
    await page.wait_for_timeout(2000)

    await _fill_registration_form(page, person)
    await _click_register_submit(page)

    for attempt in range(REGISTER_POLL_ROUNDS):
        await page.wait_for_timeout(5000)
        url = page.url
        log(f"Poll {attempt + 1}/{REGISTER_POLL_ROUNDS} — {url[:80]}")

        if "activate" in url.lower():
            log("Registration complete", "✅")
            return True

        # Check if form is still visible (submission may not have fired)
        body_text = ""
        try:
            body_text = await page.inner_text("body")
        except Exception:
            pass

        if attempt == 0:
            logger.debug("Page preview: %s", body_text[:200])

        if "First Name" in body_text and len(body_text) > 500:
            log("Form still visible — retrying submission", "⚠️")
            await _fill_registration_form(page, person)
            await _click_register_submit(page)
            await page.wait_for_timeout(15000)
            if "activate" in page.url.lower():
                log("Registration complete (retry)", "✅")
                return True
            break

    # Save debug artifacts
    try:
        slug = person["email"].replace("@", "_")
        await page.screenshot(path=str(DATA_DIR / f"reg_fail_{slug}.png"))
        html_content = await page.content()
        (DATA_DIR / f"reg_fail_{slug}.html").write_text(html_content)
        logger.debug("Debug artifacts saved for %s — URL: %s", person["email"], page.url)
    except Exception:
        pass

    log("Registration failed", "❌")
    return False


# ═════════════════════════════════════════════════════════════
# Step 2 — Fetch Activation Token (IMAP)
# ═════════════════════════════════════════════════════════════

def step2_fetch_token(email_addr: str, timeout: int = TOKEN_WAIT_TIMEOUT) -> Optional[str]:
    """Step 2: Poll IMAP for the activation token sent to *email_addr*.

    Searches for emails with subject containing "activate" addressed to the
    target.  Returns the token string or ``None`` on timeout.
    """
    log(f"Waiting for activation email (timeout {timeout}s)...")
    start = time.time()
    seen: set[bytes] = set()

    while time.time() - start < timeout:
        try:
            with imap_connection() as mail:
                _, nums = mail.search(None, "TO", f'"{email_addr}"', "SUBJECT", '"activate"')
                if nums[0]:
                    for n in nums[0].split():
                        if n in seen:
                            continue
                        seen.add(n)
                        _, data = mail.fetch(n, "(RFC822)")
                        msg = email_lib.message_from_bytes(data[0][1])
                        body = extract_body(msg, prefer=("text/html", "text/plain"))
                        clean = re.sub(r"<[^>]+>", "|||", html_lib.unescape(body))
                        match = re.search(r"Access Token is:[\s|]+([A-Za-z0-9_\-]{5,30})", clean)
                        if match:
                            token = match.group(1)
                            log(f"Token found: {token}", "✅")
                            return token
        except Exception as exc:
            logger.debug("IMAP poll error: %s", exc)

        time.sleep(IMAP_POLL_INTERVAL)

    log("Activation token not received within timeout", "❌")
    return None


# ═════════════════════════════════════════════════════════════
# Step 3 — Activate Account (CloakBrowser)
# ═════════════════════════════════════════════════════════════

_ACT_TOKEN = "#form-text-30246375"
_ACT_PASSWORD = "#form-text-766004985"
_ACT_CONFIRM = "#form-text-766004985_confirm"
_ACT_SUBMIT = "form-button-531128439"

_ACTIVATE_SUBMIT_JS = (
    f'() => {{ const btn = document.getElementById("{_ACT_SUBMIT}"); '
    "if(btn) btn.click(); "
    'else { const btns = document.querySelectorAll(".cmp-form-button"); '
    "for (const b of btns) { if (b.textContent.trim()) { b.click(); break; } } } }"
)


async def _fill_activation_form(page: Any, token: str) -> None:
    """Populate the activation form fields."""
    await page.fill(_ACT_TOKEN, token)
    await page.fill(_ACT_PASSWORD, PASSWORD)
    await page.fill(_ACT_CONFIRM, PASSWORD)


async def step3_activate(page: Any, token: str) -> bool:
    """Step 3: Activate the AMD account using *token* and set the password.

    Returns ``True`` when the page redirects to ``developer.amd.com`` or shows
    a success message.
    """
    log("Activating account...")
    await page.goto(ACTIVATE_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
    await page.wait_for_timeout(5000)

    await page.evaluate(
        '() => { const b = document.getElementById("onetrust-accept-btn-handler"); if(b) b.click(); }'
    )
    await page.wait_for_timeout(2000)

    await _fill_activation_form(page, token)
    await page.wait_for_timeout(1000)
    await page.evaluate(_ACTIVATE_SUBMIT_JS)

    for attempt in range(ACTIVATE_POLL_ROUNDS):
        await page.wait_for_timeout(5000)
        url = page.url

        if "developer.amd.com" in url:
            log("Account activated", "✅")
            return True

        body_text = ""
        try:
            body_text = await page.inner_text("body")
        except Exception:
            pass

        if any(kw in body_text.lower() for kw in ("success", "activated", "congratulations")):
            log("Account activated", "✅")
            return True

        if "Access Token" in body_text and len(body_text) > 500:
            log("Activation form still visible — retrying", "⚠️")
            await _fill_activation_form(page, token)
            await page.evaluate(_ACTIVATE_SUBMIT_JS)
            await page.wait_for_timeout(15000)
            if "developer.amd.com" in page.url:
                log("Account activated (retry)", "✅")
                return True
            break

    log("Activation failed", "❌")
    return False


# ═════════════════════════════════════════════════════════════
# Step 4 — Login via Okta → Bearer Token (HTTP)
# ═════════════════════════════════════════════════════════════

def _fetch_otp(email_addr: str) -> Optional[str]:
    """Poll IMAP for a 6-digit OTP sent to *email_addr* by Okta."""
    log("OTP sent — polling IMAP...")
    start = time.time()
    seen: set[bytes] = set()

    while time.time() - start < OTP_WAIT_TIMEOUT:
        try:
            with imap_connection() as mail:
                _, nums = mail.search(None, "FROM", '"account.help@amd.com"', "UNSEEN")
                if nums[0]:
                    for n in nums[0].split():
                        if n in seen:
                            continue
                        seen.add(n)
                        _, data = mail.fetch(n, "(RFC822)")
                        msg = email_lib.message_from_bytes(data[0][1])
                        if email_addr.lower() not in msg.get("To", "").lower():
                            continue
                        body = extract_body(msg, prefer=("text/plain",))
                        match = re.search(r"(\d{6})", body)
                        if match:
                            otp = match.group(1)
                            log(f"OTP: {otp}")
                            return otp
        except Exception as exc:
            logger.debug("IMAP OTP poll error: %s", exc)

        time.sleep(OTP_POLL_INTERVAL)

    log("OTP not received within timeout", "❌")
    return None


def step4_login(email_addr: str, proxy: Optional[dict] = None) -> Optional[str]:
    """Step 4: Authenticate via Okta PKCE flow and return a Bearer token.

    Uses the Okta ``/idp/idx`` API to identify, trigger email OTP challenge,
    verify the OTP, and exchange the authorization code for an access token.
    """
    log("Logging in via Okta...")
    session = requests.Session()
    if proxy:
        session.proxies.update(proxy["requests"])
        log(f"HTTP proxy: {proxy['display']}")

    fp = random.choice(FINGERPRINTS)
    session.headers.update({"user-agent": fp["ua"]})
    verifier = generate_code_verifier()

    # 1. Initiate authorization
    resp = session.get(
        f"{OKTA_BASE}/oauth2/default/v1/authorize",
        params={
            "client_id": OKTA_CLIENT_ID,
            "redirect_uri": OKTA_REDIRECT_URI,
            "response_type": "code",
            "scope": "openid profile email",
            "state": generate_random_id(),
            "nonce": generate_random_id(),
            "code_challenge": generate_code_challenge(verifier),
            "code_challenge_method": "S256",
            "response_mode": "query",
        },
    )
    state_token = find_state_token(resp.text)
    if not state_token:
        log("stateToken not found in authorize response", "❌")
        return None

    # 2. Identify (email + password)
    resp_identify = session.post(
        f"{OKTA_BASE}/idp/idx/identify",
        json={
            "identifier": email_addr,
            "credentials": {"passcode": PASSWORD},
            "stateHandle": state_token,
        },
        headers=OKTA_HEADERS,
    )
    identify_data = resp_identify.json()

    redirect_href = identify_data.get("success", {}).get("href", "")
    if redirect_href:
        log("Authenticated without OTP", "✅")
    else:
        # 3. Trigger email OTP challenge
        state_handle = identify_data.get("stateHandle", "")
        if not state_handle:
            log("Login failed — no stateHandle in identify response", "❌")
            logger.debug("Identify response: %s", json.dumps(identify_data, indent=2)[:500])
            return None

        resp_challenge = session.post(
            f"{OKTA_BASE}/idp/idx/challenge",
            json={
                "authenticator": {"id": OKTA_EMAIL_AUTH_ID, "methodType": "email"},
                "stateHandle": state_handle,
            },
            headers=OKTA_HEADERS,
        )
        challenge_data = resp_challenge.json()
        state_handle2 = challenge_data.get("stateHandle", "")
        if not state_handle2:
            log("Challenge trigger failed", "❌")
            return None

        # 4. Fetch OTP from email
        otp = _fetch_otp(email_addr)
        if not otp:
            return None

        # 5. Verify OTP
        resp_answer = session.post(
            f"{OKTA_BASE}/idp/idx/challenge/answer",
            json={
                "credentials": {"passcode": otp},
                "stateHandle": state_handle2,
            },
            headers=OKTA_HEADERS,
        )
        redirect_href = resp_answer.json().get("success", {}).get("href", "")
        if not redirect_href:
            log("OTP verification failed — no redirect", "❌")
            return None

    # 6. Exchange auth code for access token
    resp_redirect = session.get(redirect_href, allow_redirects=False)
    location = resp_redirect.headers.get("Location", "")
    code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("code", [None])[0]
    if not code:
        log("Authorization code not found in redirect", "❌")
        return None

    resp_token = requests.post(
        f"{OKTA_BASE}/oauth2/default/v1/token",
        data={
            "grant_type": "authorization_code",
            "redirect_uri": OKTA_REDIRECT_URI,
            "code": code,
            "code_verifier": verifier,
            "client_id": OKTA_CLIENT_ID,
        },
        headers={
            "accept": "application/json",
            "content-type": "application/x-www-form-urlencoded",
        },
    )
    bearer = resp_token.json().get("access_token", "")
    if bearer:
        log("Bearer token obtained", "✅")
        return bearer

    log("Token exchange failed", "❌")
    return None


# ═════════════════════════════════════════════════════════════
# Step 5 — Submit Credit Request (Marketo Form)
# ═════════════════════════════════════════════════════════════

_MARKETO_SELECT_FIELDS = ("Country", "Type__c", "technicalteam", "h100sUseCase")


def _build_credit_form_values(person: dict[str, str]) -> dict[str, str]:
    """Build the Marketo form payload from persona data."""
    dev_type = random.choice([
        "Independent developer",
        "Member of opensource project",
        "Member of a corporation",
    ])
    values: dict[str, str] = {
        "FirstName": person["first"],
        "Email": person["email"],
        "githubHandle": person["github"],
        "company_linkedin_handle__c_lead": "",
        "Country": person["country_code"],
        "PostalCode": str(random.randint(10000, 99999)),
        "Type__c": dev_type,
        "Company": person["company"],
        "Company__c": person["company"],
        "DaScoopComposer__Email_2__c": person["email"],
        "Contact_Sales_Use_Case__c_lead": person["use_case"],
        "technicalteam": random.choice(["No", "Yes, I am a beginner", "Yes, I am an advanced user"]),
        "h100sUseCase": random.choice(["Inference end point only", "Inference", "Finetuning", "Training"]),
        "Desired_Outcome__c": person["outcome"],
        "Marketing_Comments__c": person["use_case"],
    }

    if dev_type == "Member of opensource project":
        values["openText"] = (
            f"Contributing to {person['company']} open-source projects. "
            "Working on GPU optimization and ROCm compatibility."
        )
    if dev_type == "Member of a corporation":
        values["Company__c"] = person["company"]
        values["DaScoopComposer__Email_2__c"] = person["email"]

    return values


async def step5_credit(page: Any, person: dict[str, str]) -> bool:
    """Step 5: Submit a free-credit request via the Marketo form.

    Returns ``True`` when the form redirects to ``devcloud.amd.com``.
    """
    log("Submitting credit request...")

    await page.goto(CREDIT_FORM_URL, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT_MS)
    await page.wait_for_function(
        'typeof MktoForms2 !== "undefined" && MktoForms2.allForms().length > 0',
        timeout=MARKETO_READY_TIMEOUT_MS,
    )
    await page.wait_for_timeout(2000)

    form_values = _build_credit_form_values(person)

    # Set Type__c first to trigger conditional field visibility
    await page.evaluate(
        '(vals) => { MktoForms2.allForms()[0].setValues({"Type__c": vals.Type__c}); }',
        form_values,
    )
    await page.evaluate(
        """(val) => {
            const el = document.getElementById("Type__c");
            if (el) { el.value = val; el.dispatchEvent(new Event("change", {bubbles: true})); }
        }""",
        form_values["Type__c"],
    )
    await page.wait_for_timeout(1500)

    # Set all values via Marketo JS API
    await page.evaluate(
        "(vals) => { MktoForms2.allForms()[0].setValues(vals); }",
        form_values,
    )
    await page.wait_for_timeout(1000)

    # Explicitly set <select> elements via DOM to ensure they fire change events
    for sel_id in _MARKETO_SELECT_FIELDS:
        await page.evaluate(
            f"""(val) => {{
                const el = document.getElementById("{sel_id}");
                if (el) {{ el.value = val; el.dispatchEvent(new Event("change", {{bubbles: true}})); }}
            }}""",
            form_values[sel_id],
        )
    await page.wait_for_timeout(500)

    # Validate; auto-fix any invalid fields
    is_valid = await page.evaluate("() => MktoForms2.allForms()[0].validate()")
    if not is_valid:
        invalid_fields = await page.evaluate(
            '() => Array.from(document.querySelectorAll(".mktoInvalid")).map(e => e.id || e.name)'
        )
        log(f"Fixing invalid fields: {invalid_fields}", "⚠️")
        for field_id in invalid_fields:
            if field_id not in form_values:
                continue
            tag = await page.evaluate(f'() => document.getElementById("{field_id}")?.tagName')
            if tag == "SELECT":
                await page.select_option(f"#{field_id}", form_values[field_id])
            else:
                await page.fill(f"#{field_id}", form_values[field_id])
        await page.wait_for_timeout(500)
        await page.evaluate(
            "(vals) => { MktoForms2.allForms()[0].setValues(vals); }",
            form_values,
        )

    # Submit
    await page.evaluate("() => MktoForms2.allForms()[0].submit()")

    # Wait for redirect to devcloud.amd.com
    try:
        await page.wait_for_url("**/devcloud.amd.com/**", timeout=REDIRECT_TIMEOUT_MS)
        log("Credit request submitted", "✅")
        return True
    except Exception:
        if "devcloud" in page.url:
            log("Credit request submitted", "✅")
            return True

    log("Credit request may have failed — no redirect detected", "⚠️")
    return False


# ═════════════════════════════════════════════════════════════
# Pipeline Orchestrator
# ═════════════════════════════════════════════════════════════

async def run_pipeline(person: dict[str, str]) -> dict[str, str]:
    """Execute the full 5-step pipeline for one persona.

    Returns a dict with ``email`` and ``status`` keys.
    """
    email = person["email"]
    log(f"{'=' * 60}")
    log(f"{person['first']} {person['last']} | {email}")
    log(f"{person['company']} | {person['country_label']}")
    log(f"{'=' * 60}")

    proxy = pick_proxy()
    if proxy:
        log(f"Proxy: {proxy['display']}", "🌐")

    launch_opts: dict[str, Any] = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
    }
    if proxy:
        launch_opts["proxy"] = proxy["playwright"]

    browser = await cloakbrowser.launch_async(**launch_opts)
    page = await browser.new_page()

    try:
        # Step 1: Register
        if not await step1_register(page, person):
            return {"email": email, "status": "REGISTER_FAILED"}

        # Step 2: Fetch activation token
        await asyncio.sleep(15)
        token = step2_fetch_token(email, timeout=TOKEN_WAIT_TIMEOUT)
        if not token:
            return {"email": email, "status": "NO_TOKEN"}

        # Step 3: Activate
        if not await step3_activate(page, token):
            return {"email": email, "status": "ACTIVATE_FAILED"}

        # Step 4: Login via Okta (HTTP)
        await asyncio.sleep(5)
        bearer = step4_login(email, proxy=proxy)
        if not bearer:
            return {"email": email, "status": "LOGIN_FAILED"}

        # Step 5: Credit request
        if not await step5_credit(page, person):
            return {"email": email, "status": "CREDIT_FAILED"}

        # Record success
        with open(SUCCESS_FILE, "a") as fh:
            fh.write(f"{email}:{PASSWORD}:{datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

        return {"email": email, "status": "SUCCESS"}

    finally:
        await browser.close()


# ═════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="AMD Developer Cloud — automated registration & credit request pipeline",
    )
    parser.add_argument("--count", type=int, default=1, help="Number of accounts to register")
    parser.add_argument("--email", help="Specific email address to register")
    parser.add_argument("--name", help="Full name (first last) for the account")
    parser.add_argument("--company", default="MIT", help="Company / institution name")
    parser.add_argument("--country", default="US", help="ISO 3166-1 alpha-2 country code")
    args = parser.parse_args()

    if args.email:
        parts = (args.name or args.email.split("@")[0]).split()
        person = {
            "first": parts[0],
            "last": parts[-1] if len(parts) > 1 else "User",
            "email": args.email,
            "github": args.email.split("@")[0].replace(".", ""),
            "company": args.company,
            "country_code": args.country,
            "country_label": COUNTRY_LABELS.get(args.country, "United States"),
            "use_case": random.choice(USE_CASES),
            "outcome": random.choice(OUTCOMES),
        }
        result = await run_pipeline(person)
        print(f"\n  {result['status']}: {result['email']}")
    else:
        print(f"\n  AMD Full Pipeline — {args.count} account(s)\n")
        results: list[dict[str, str]] = []
        for i in range(args.count):
            log(f"Account {i + 1}/{args.count}")
            person = generate_person()
            result = await run_pipeline(person)
            results.append(result)
            if i < args.count - 1:
                delay = random.uniform(3, 7)
                log(f"Cooldown {delay:.1f}s before next account...")
                await asyncio.sleep(delay)

        # Summary
        print(f"\n{'=' * 60}")
        ok = sum(1 for r in results if r["status"] == "SUCCESS")
        for r in results:
            icon = "✅" if r["status"] == "SUCCESS" else "❌"
            print(f"  {icon} {r['email']:45s} → {r['status']}")
        print(f"\n  Success: {ok}/{len(results)}")
        print(f"{'=' * 60}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Interrupted by user")
        sys.exit(130)
