"""
image_scraper.py
----------------
A reusable Google Images scraper module.
Supports x86_64 and ARM64 Linux servers (including snap-installed Chromium).

Usage (import into any project):
    from image_scraper import create_driver, scrape_thumbnails, download_thumbnails, shutdown_driver

    driver = create_driver()
    links  = scrape_thumbnails(driver, "sunflower")
    saved  = download_thumbnails(links, "sunflower", base_url="http://192.168.1.10:5000")
    # → ["http://192.168.1.10:5000/static/plants/sunflower_1.webp", ...]
    shutdown_driver(driver)
"""

import os
import re
import glob
import base64
import shutil
import subprocess
import time
import logging
import threading
from io import BytesIO
from typing import Optional

import requests
from PIL import Image
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.chrome.service import Service

try:
    from xvfbwrapper import Xvfb
    XVFB_AVAILABLE = True
except ImportError:
    XVFB_AVAILABLE = False

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------
_vdisplay: Optional[object] = None
_driver_lock = threading.Lock()

PLANTS_DIR = "static/plants"
_CANDIDATE_MULTIPLIER = 5


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_chromium_binary() -> Optional[str]:
    candidates = [
        "google-chrome", "google-chrome-stable",
        "chromium-browser", "chromium",
        "/snap/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for c in candidates:
        path = shutil.which(c) or (c if os.path.isfile(c) else None)
        if path:
            return path
    return None


def _detect_chrome_version(binary: str) -> int:
    try:
        raw = subprocess.check_output(
            [binary, "--version"], stderr=subprocess.DEVNULL
        ).decode().strip()
        for token in reversed(raw.split()):
            if token[0].isdigit():
                version = int(token.split(".")[0])
                logger.info("Auto-detected browser version: %d (from '%s')", version, raw)
                return version
        raise ValueError(f"No version token found in: {raw}")
    except Exception as exc:
        logger.warning("Could not detect browser version: %s — falling back to 134", exc)
        return 134


def _find_chromedriver(chrome_version: int) -> Optional[str]:
    snap_pattern = "/home/chromedriver_copy/chromedriver"
    snap_matches = sorted(glob.glob(snap_pattern))
    if snap_matches:
        path = snap_matches[-1]
        logger.info("Found snap chromedriver: %s", path)
        return path
    system_cd = shutil.which("chromedriver")
    if system_cd:
        logger.info("Found system chromedriver: %s", system_cd)
        return system_cd
    logger.info("No local chromedriver found; undetected_chromedriver will download one.")
    return None



def _query_to_prefix(query: str) -> str:
    prefix = query.lower().strip()
    prefix = re.sub(r"[^\w\s]", "", prefix)
    prefix = re.sub(r"\s+", "_", prefix)
    return prefix


def _bytes_to_webp(image_bytes: bytes, quality: int = 85) -> bytes:
    """
    Convert raw image bytes (any format) to WebP bytes using Pillow.
    RGBA images are flattened to RGB so WebP saves cleanly.
    """
    img = Image.open(BytesIO(image_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="WEBP", quality=quality)
    return buf.getvalue()


def _decode_data_uri(data_uri: str) -> bytes:
    """Decode a base64 data-URI to raw bytes."""
    _, encoded = data_uri.split(",", 1)
    return base64.b64decode(encoded)


def _build_url(base_url: Optional[str], filepath: str) -> str:
    """
    Combine base_url with a file path.
    e.g. base_url="http://192.168.1.10:5000", filepath="static/plants/sunflower_1.webp"
    → "http://192.168.1.10:5000/static/plants/sunflower_1.webp"

    If base_url is None, returns the bare file path unchanged.
    """
    if not base_url:
        return filepath
    return f"{base_url.rstrip('/')}/{filepath.lstrip('/')}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_driver(
    chrome_version: Optional[int] = None,
    window_size: tuple = (1280, 720),
    start_xvfb: bool = True,
) -> uc.Chrome:
    """
    Create and return a reusable Chrome/Chromium driver.

    Parameters
    ----------
    chrome_version : int or None  – auto-detected if None
    window_size    : tuple        – (width, height)
    start_xvfb     : bool         – start Xvfb on headless Linux servers
    """
    global _vdisplay

    if start_xvfb:
        if XVFB_AVAILABLE:
            _vdisplay = Xvfb(width=window_size[0], height=window_size[1])
            _vdisplay.start()
            logger.info("Xvfb virtual display started.")
        else:
            logger.warning("xvfbwrapper not installed. Run: pip install xvfbwrapper")

    binary = _find_chromium_binary()
    if not binary:
        raise FileNotFoundError(
            "Could not find Chrome or Chromium. "
            "Install: sudo apt-get install -y chromium-browser"
        )
    logger.info("Using browser binary: %s", binary)

    if chrome_version is None:
        chrome_version = _detect_chrome_version(binary)

    chromedriver_path = _find_chromedriver(chrome_version)

    options = uc.ChromeOptions()
    options.binary_location = binary
    options.add_argument(f"--window-size={window_size[0]},{window_size[1]}")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-plugins")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--dns-prefetch-disable")

    if chromedriver_path:
        service = Service(executable_path=chromedriver_path)
        driver = uc.Chrome(
            version_main=chrome_version,
            options=options,
            service=service,
            driver_executable_path=chromedriver_path,
        )
    else:
        driver = uc.Chrome(version_main=chrome_version, options=options)

    driver.execute_cdp_cmd("Network.setBlockedURLs", {
        "urls": ["*.css", "*.woff", "*.woff2", "*.ttf", "*.eot", "*.mp4", "*.webm", "*.avi"]
    })
    driver.execute_cdp_cmd("Network.enable", {})

    logger.info("Chrome driver created successfully (version %d).", chrome_version)
    return driver


def scrape_thumbnails(
    driver: uc.Chrome,
    query: str,
    count: int = 3,
    min_width: int = 150,
    min_height: int = 150,
    page_load_timeout: int = 15,
    lazy_load_timeout: int = 10,
) -> list[str]:
    """
    Scrape Google Images and return thumbnail URLs / data-URIs for *query*.

    Parameters
    ----------
    driver            : uc.Chrome – driver from create_driver()
    query             : str       – search term e.g. "sunflower"
    count             : int       – thumbnails to return (default 3)
    min_width/height  : int       – minimum pixel dimensions to accept
    page_load_timeout : int       – seconds to wait for initial DOM images
    lazy_load_timeout : int       – seconds to wait for lazy-loaded images

    Returns
    -------
    list[str]  – up to `count` src values (http URLs or data-URIs)
    """
    start_time = time.time()
    candidate_count = count * _CANDIDATE_MULTIPLIER

    with _driver_lock:
        try:
            driver.get(f"https://www.google.com/search?q={query}&tbm=isch")

            WebDriverWait(driver, page_load_timeout).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "img[src]")) >= count
            )

            driver.execute_script("window.scrollTo(0, 800);")
            time.sleep(1)
            
            try:
                WebDriverWait(driver, lazy_load_timeout).until(
                    lambda d: len(d.find_elements(
                        By.CSS_SELECTOR, "img[src^='http'], img[src^='data:image']"
                    )) >= candidate_count
                )
            except Exception:
                logger.info("Timeout waiting for %d candidates; using available images.", candidate_count)

            img_elements = driver.find_elements(
                By.CSS_SELECTOR, "img[src^='http'], img[src^='data:image']"
            )

            image_data = driver.execute_script("""
                var imgs = arguments[0];
                var out  = [];
                for (var i = 0; i < imgs.length; i++) {
                    out.push({ src: imgs[i].src, w: imgs[i].naturalWidth, h: imgs[i].naturalHeight });
                }
                return out;
            """, img_elements)

            valid     = [d for d in image_data if d["src"] and d["w"] >= min_width and d["h"] >= min_height]
            http_srcs = [d["src"] for d in valid if d["src"].startswith("http")]
            data_srcs = [d["src"] for d in valid if d["src"].startswith("data:")]

            if len(http_srcs) >= count:
                selected = http_srcs[:count]
                logger.info("Using %d HTTP URL(s) for '%s'.", count, query)
            else:
                needed   = count - len(http_srcs)
                selected = http_srcs + data_srcs[:needed]
                logger.info(
                    "Only %d HTTP URL(s) for '%s'; padding with %d data-URI(s).",
                    len(http_srcs), query, len(selected) - len(http_srcs),
                )

            elapsed = time.time() - start_time
            logger.info("Query '%s' — %d thumbnail(s) in %.2fs.", query, len(selected), elapsed)
            return selected

        except Exception as exc:
            logger.error("scrape_thumbnails failed for '%s': %s", query, exc)
            return []


def download_thumbnails(
    urls: list[str],
    query: str,
    save_dir: str = PLANTS_DIR,
    base_url: Optional[str] = None,
    webp_quality: int = 85,
    timeout: int = 10,
) -> list[str]:
    """
    Download / decode thumbnails, convert to WebP, and save to *save_dir*.

    Naming:  <query_prefix>_1.webp,  _2.webp,  _3.webp
    e.g. query="sunflower"  →  sunflower_1.webp, sunflower_2.webp, sunflower_3.webp

    Parameters
    ----------
    urls         : list[str]      – srcs from scrape_thumbnails()
    query        : str            – search query (used as filename prefix)
    save_dir     : str            – destination folder (auto-created if absent)
    base_url     : str or None    – prepend this to every returned path, e.g.
                                    "http://192.168.1.10:5000"
                                    → "http://192.168.1.10:5000/static/plants/sunflower_1.webp"
                                    Leave None to return bare file paths.
    webp_quality : int            – WebP compression quality 1-100 (default 85)
    timeout      : int            – HTTP download timeout in seconds

    Returns
    -------
    list[str]
        Full URLs (if base_url given) or bare file paths of saved .webp files.
        e.g. ["http://192.168.1.10:5000/static/plants/sunflower_1.webp", ...]
    """
    os.makedirs(save_dir, exist_ok=True)
    prefix = _query_to_prefix(query)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    result_urls: list[str] = []

    for index, url in enumerate(urls, start=1):
        filename = f"{prefix}_{index}.webp"           # always .webp
        filepath = os.path.join(save_dir, filename)

        try:
            # ── Step 1: get raw bytes (HTTP or data-URI) ──────────────────
            if url.startswith("data:"):
                raw_bytes = _decode_data_uri(url)
                source    = "data-URI"
            else:
                response  = requests.get(url, headers=headers, timeout=timeout)
                response.raise_for_status()
                raw_bytes = response.content
                source    = "HTTP"
                
            # ── Step 2: convert to WebP via Pillow ────────────────────────
            webp_bytes = _bytes_to_webp(raw_bytes, quality=webp_quality)

            # ── Step 3: write to disk ─────────────────────────────────────
            with open(filepath, "wb") as f:
                f.write(webp_bytes)

            logger.info("Saved (%s → WebP): %s  (%d bytes)", source, filepath, len(webp_bytes))

            # ── Step 4: build return URL ──────────────────────────────────
            result_urls.append(_build_url(base_url, filepath))

        except Exception as exc:
            logger.error("Failed to process image %d for '%s': %s", index, query, exc)

    return result_urls


def shutdown_driver(driver: uc.Chrome) -> None:
    """Quit the Chrome driver and stop Xvfb (if started)."""
    global _vdisplay
    try:
        driver.quit()
        logger.info("Chrome driver shut down.")
    except Exception as exc:
        logger.warning("Error quitting driver: %s", exc)

    if _vdisplay is not None:
        try:
            _vdisplay.stop()
            logger.info("Xvfb virtual display stopped.")
        except Exception as exc:
            logger.warning("Error stopping Xvfb: %s", exc)
        _vdisplay = None


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    BASE_URL = "http://127.0.0.1:5000"   # ← change to your server IP:port

    driver = create_driver()
    try:
        queries = ["Fagus grandiflora", "Quercus robur", "Betula pendula"]
        for q in queries:
            results = scrape_thumbnails(driver, q)
            saved   = download_thumbnails(results, q, base_url=BASE_URL)
            print(f"\n--- {q} ---")
            for url in saved:
                print(url)
    finally:
        shutdown_driver(driver)
        os._exit(0)
