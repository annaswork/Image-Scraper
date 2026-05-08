"""
image_scraper.py
----------------
A reusable Google Images scraper module.
Supports x86_64 and ARM64 Linux servers (including snap-installed Chromium).

Usage (import into any project):
    from image_scraper import create_driver, scrape_thumbnails, download_thumbnails, shutdown_driver

    driver = create_driver()                             # Call ONCE at server startup
    links  = scrape_thumbnails(driver, "sunflower")
    saved  = download_thumbnails(links, "sunflower")     # → static/plants/sunflower_1.jpg …
    shutdown_driver(driver)                              # Call ONCE at server shutdown
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
from typing import Optional

import requests
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.chrome.service import Service

# Optional: Xvfb is only needed on headless Linux servers.
try:
    from xvfbwrapper import Xvfb
    XVFB_AVAILABLE = True
except ImportError:
    XVFB_AVAILABLE = False

# ---------------------------------------------------------------------------
# Module-level logger
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

# How many extra candidates to fetch beyond `count` so we can survive
# data-URIs and filter to real HTTP images when possible.
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


def _ext_from_url(url: str, default: str = ".jpg") -> str:
    known = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
    path = url.split("?")[0]
    _, ext = os.path.splitext(path)
    return ext.lower() if ext.lower() in known else default


def _ext_from_data_uri(data_uri: str) -> str:
    """Extract file extension from a data-URI mime type.
    e.g. 'data:image/jpeg;base64,...' → '.jpg'
    """
    mime_map = {
        "image/jpeg": ".jpg",
        "image/jpg":  ".jpg",
        "image/png":  ".png",
        "image/webp": ".webp",
        "image/gif":  ".gif",
        "image/bmp":  ".bmp",
    }
    try:
        header = data_uri.split(";")[0]          # 'data:image/jpeg'
        mime   = header.split(":")[1].lower()    # 'image/jpeg'
        return mime_map.get(mime, ".jpg")
    except Exception:
        return ".jpg"


def _save_data_uri(data_uri: str, filepath: str) -> bool:
    """Decode a base64 data-URI and write it to *filepath*. Returns True on success."""
    try:
        # Format: data:<mime>;base64,<data>
        _, encoded = data_uri.split(",", 1)
        image_bytes = base64.b64decode(encoded)
        with open(filepath, "wb") as f:
            f.write(image_bytes)
        return True
    except Exception as exc:
        logger.error("Failed to decode data-URI: %s", exc)
        return False


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
    chrome_version : int or None
        Major Chrome/Chromium version. Auto-detected if None.
    window_size : tuple
        (width, height) for the browser window.
    start_xvfb : bool
        Start Xvfb virtual display on headless Linux servers.
    """
    global _vdisplay

    if start_xvfb:
        if XVFB_AVAILABLE:
            _vdisplay = Xvfb(width=window_size[0], height=window_size[1])
            _vdisplay.start()
            logger.info("Xvfb virtual display started.")
        else:
            logger.warning(
                "xvfbwrapper not installed. "
                "If headless, run: pip install xvfbwrapper"
            )

    binary = _find_chromium_binary()
    if not binary:
        raise FileNotFoundError(
            "Could not find Chrome or Chromium. "
            "Install: sudo apt-get install -y chromium-browser  "
            "      or sudo snap install chromium"
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

    Strategy
    --------
    We request _CANDIDATE_MULTIPLIER × count candidates from the DOM so we
    can prefer real HTTP URLs over data-URIs.  If not enough HTTP URLs are
    found we fall back to data-URIs to always return exactly `count` results.

    Parameters
    ----------
    driver            : uc.Chrome  – driver from create_driver()
    query             : str        – search term
    count             : int        – number of thumbnails to return (default 3)
    min_width/height  : int        – minimum pixel dimensions
    page_load_timeout : int        – seconds to wait for initial images
    lazy_load_timeout : int        – seconds to wait for lazy-loaded images

    Returns
    -------
    list[str]  – up to `count` src values (http URLs or data-URIs)
    """
    start_time = time.time()
    # Fetch more candidates than needed so we can prefer HTTP over data-URIs
    candidate_count = count * _CANDIDATE_MULTIPLIER

    with _driver_lock:
        try:
            driver.get(f"https://www.google.com/search?q={query}&tbm=isch")

            # Wait for initial images
            WebDriverWait(driver, page_load_timeout).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "img[src]")) >= count
            )

            # Scroll to trigger lazy loading
            driver.execute_script("window.scrollTo(0, 800);")
            time.sleep(1)  # brief pause for lazy images to begin loading

            # Wait for a good pool of HTTP candidates
            try:
                WebDriverWait(driver, lazy_load_timeout).until(
                    lambda d: len(d.find_elements(
                        By.CSS_SELECTOR, "img[src^='http'], img[src^='data:image']"
                    )) >= candidate_count
                )
            except Exception:
                # If we can't get candidate_count, continue with whatever is there
                logger.info("Timeout waiting for %d candidates; proceeding with available images.", candidate_count)

            img_elements = driver.find_elements(
                By.CSS_SELECTOR, "img[src^='http'], img[src^='data:image']"
            )

            # Batch JS call to read src + dimensions
            image_data = driver.execute_script("""
                var imgs = arguments[0];
                var out  = [];
                for (var i = 0; i < imgs.length; i++) {
                    out.push({
                        src: imgs[i].src,
                        w:   imgs[i].naturalWidth,
                        h:   imgs[i].naturalHeight
                    });
                }
                return out;
            """, img_elements)

            # Filter by minimum dimensions
            valid = [
                d for d in image_data
                if d["src"] and d["w"] >= min_width and d["h"] >= min_height
            ]

            # Prefer real HTTP URLs; fall back to data-URIs if not enough
            http_srcs = [d["src"] for d in valid if d["src"].startswith("http")]
            data_srcs = [d["src"] for d in valid if d["src"].startswith("data:")]

            if len(http_srcs) >= count:
                selected = http_srcs[:count]
                logger.info("Using %d HTTP URL(s) for '%s'.", count, query)
            else:
                # Pad with data-URIs to reach `count`
                needed   = count - len(http_srcs)
                selected = http_srcs + data_srcs[:needed]
                logger.info(
                    "Only %d HTTP URL(s) found for '%s'; padding with %d data-URI(s).",
                    len(http_srcs), query, len(selected) - len(http_srcs),
                )

            elapsed = time.time() - start_time
            logger.info("Query '%s' — returning %d thumbnail(s) in %.2fs.", query, len(selected), elapsed)
            return selected

        except Exception as exc:
            logger.error("scrape_thumbnails failed for query '%s': %s", query, exc)
            return []


def download_thumbnails(
    urls: list[str],
    query: str,
    save_dir: str = PLANTS_DIR,
    timeout: int = 10,
) -> list[str]:
    """
    Download / decode thumbnail images and save them to *save_dir*.

    Naming convention:  <query_prefix>_1.<ext>,  _2.<ext>,  _3.<ext>
    e.g.  query="sunflower"  →  sunflower_1.jpg, sunflower_2.jpg, sunflower_3.jpg

    Both HTTP URLs and data-URIs are handled:
      - HTTP URL  → downloaded with requests
      - data-URI  → base64-decoded and written directly (no network call)

    Parameters
    ----------
    urls     : list[str]  – srcs from scrape_thumbnails()
    query    : str        – original search query (used as filename prefix)
    save_dir : str        – destination directory (created if absent)
    timeout  : int        – per-request HTTP timeout in seconds

    Returns
    -------
    list[str]  – file paths of successfully saved images
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

    saved_paths: list[str] = []

    for index, url in enumerate(urls, start=1):
        if url.startswith("data:"):
            # ── data-URI: decode base64 directly ──────────────────────────
            ext      = _ext_from_data_uri(url)
            filename = f"{prefix}_{index}{ext}"
            filepath = os.path.join(save_dir, filename)

            if _save_data_uri(url, filepath):
                size = os.path.getsize(filepath)
                logger.info("Saved (data-URI): %s  (%d bytes)", filepath, size)
                saved_paths.append(filepath)
            # else: error already logged inside _save_data_uri

        else:
            # ── HTTP URL: download with requests ──────────────────────────
            ext      = _ext_from_url(url)
            filename = f"{prefix}_{index}{ext}"
            filepath = os.path.join(save_dir, filename)

            try:
                response = requests.get(url, headers=headers, timeout=timeout)
                response.raise_for_status()

                with open(filepath, "wb") as f:
                    f.write(response.content)

                logger.info("Saved (HTTP): %s  (%d bytes)", filepath, len(response.content))
                saved_paths.append(filepath)

            except Exception as exc:
                logger.error("Failed to download '%s': %s", url, exc)

    return saved_paths


def shutdown_driver(driver: uc.Chrome) -> None:
    """Quit the Chrome driver and stop the virtual display (if started)."""
    global _vdisplay

    try:
        driver.quit()
        logger.info("Chrome driver shut down.")
    except Exception as exc:
        logger.warning("Error while quitting driver: %s", exc)

    if _vdisplay is not None:
        try:
            _vdisplay.stop()
            logger.info("Xvfb virtual display stopped.")
        except Exception as exc:
            logger.warning("Error while stopping Xvfb: %s", exc)
        _vdisplay = None


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    driver = create_driver()

    try:
        queries = ["Fagus grandiflora", "Quercus robur", "Betula pendula"]

        for q in queries:
            results = scrape_thumbnails(driver, q)
            print(f"\n--- {q} ({len(results)} result(s)) ---")
            for link in results:
                print(link if link.startswith("http") else f"[data-URI  {link[:40]}...]")

            saved = download_thumbnails(results, q)
            print(f"Saved {len(saved)} file(s): {saved}")
    finally:
        shutdown_driver(driver)
        os._exit(0)
