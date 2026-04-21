"""
image_scraper.py
----------------
A reusable Google Images scraper module.
Supports x86_64 and ARM64 Linux servers (including snap-installed Chromium).

Usage (import into any project):
    from image_scraper import create_driver, scrape_thumbnails, shutdown_driver

    driver = create_driver()                        # Call ONCE at server startup
    links  = scrape_thumbnails(driver, "Fagus grandiflora")
    links2 = scrape_thumbnails(driver, "Quercus robur")
    shutdown_driver(driver)                         # Call ONCE at server shutdown
"""

import os
import glob
import shutil
import subprocess
import time
import logging
import threading
from typing import Optional

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.chrome.service import Service

# Optional: Xvfb is only needed on headless Linux servers.
# If not installed, the module still works on systems with a real display.
try:
    from xvfbwrapper import Xvfb
    XVFB_AVAILABLE = True
except ImportError:
    XVFB_AVAILABLE = False

# ---------------------------------------------------------------------------
# Module-level logger — callers can configure this however they like.
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# Internal state: virtual display + a lock for thread-safe scraping
# ---------------------------------------------------------------------------
_vdisplay: Optional[object] = None
_driver_lock = threading.Lock()  # Ensures one scrape runs at a time


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_chromium_binary() -> Optional[str]:
    """
    Return the path to a Chrome/Chromium binary, or None if not found.
    Checks PATH and common fixed locations including snap.
    """
    candidates = [
        "google-chrome",
        "google-chrome-stable",
        "chromium-browser",
        "chromium",
        "/snap/bin/chromium",           # snap install (symlink)
        "/usr/bin/chromium-browser",    # apt install
        "/usr/bin/chromium",
    ]
    for c in candidates:
        path = shutil.which(c) or (c if os.path.isfile(c) else None)
        if path:
            return path
    return None


def _detect_chrome_version(binary: str) -> int:
    """
    Auto-detect the major version number from the browser binary.
    Handles both:
      "Google Chrome 134.0.6998.165"
      "Chromium 147.0.7727.55 snap"
    Falls back to 134 if detection fails.
    """
    try:
        raw = subprocess.check_output(
            [binary, "--version"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        # Find the first token that starts with a digit — that is the version string
        tokens = raw.split()
        for token in reversed(tokens):
            if token[0].isdigit():
                version = int(token.split(".")[0])
                logger.info("Auto-detected browser version: %d (from '%s')", version, raw)
                return version
        raise ValueError(f"No version token found in: {raw}")
    except Exception as exc:
        logger.warning("Could not detect browser version: %s — falling back to 134", exc)
        return 134


def _find_chromedriver(chrome_version: int) -> Optional[str]:
    """
    Find an architecture-compatible chromedriver binary.

    Search order:
      1. snap Chromium's bundled chromedriver  (ARM64 / snap Chromium)
      2. System PATH chromedriver              (apt install chromium-chromedriver)
      3. None → let undetected_chromedriver download one (x86_64 only)
    """
    # -- Snap-bundled chromedriver -------------------------------------------
    snap_pattern = "/home/chromedriver_copy/chromedriver"
    snap_matches = sorted(glob.glob(snap_pattern))  # latest revision last
    if snap_matches:
        path = snap_matches[-1]
        logger.info("Found snap chromedriver: %s", path)
        return path

    # -- System PATH chromedriver --------------------------------------------
    system_cd = shutil.which("chromedriver")
    if system_cd:
        logger.info("Found system chromedriver: %s", system_cd)
        return system_cd

    # -- Let undetected_chromedriver handle it (x86_64 only) ----------------
    logger.info("No local chromedriver found; undetected_chromedriver will download one.")
    return None


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
        Major version of Chrome/Chromium installed on the machine.
        If None (default), the version is auto-detected.
        Run `google-chrome --version` or `chromium-browser --version` to check.
    window_size : tuple
        (width, height) for the browser window.
    start_xvfb : bool
        Start a virtual display (Xvfb) automatically on headless Linux servers.
        Set to False if you are on a desktop OS or managing Xvfb yourself.

    Returns
    -------
    uc.Chrome
        A configured, ready-to-use driver instance.
    """
    global _vdisplay

    # -- Start virtual display on headless Linux if requested ----------------
    if start_xvfb:
        if XVFB_AVAILABLE:
            _vdisplay = Xvfb(width=window_size[0], height=window_size[1])
            _vdisplay.start()
            logger.info("Xvfb virtual display started.")
        else:
            logger.warning(
                "xvfbwrapper is not installed. "
                "If this is a headless server, install it: pip install xvfbwrapper"
            )

    # -- Locate browser binary -----------------------------------------------
    binary = _find_chromium_binary()
    if not binary:
        raise FileNotFoundError(
            "Could not find Chrome or Chromium. "
            "Install with:  sudo apt-get install -y chromium-browser  "
            "            or sudo snap install chromium"
        )
    logger.info("Using browser binary: %s", binary)

    # -- Auto-detect version if not provided ---------------------------------
    if chrome_version is None:
        chrome_version = _detect_chrome_version(binary)

    # -- Locate an architecture-compatible chromedriver ----------------------
    chromedriver_path = _find_chromedriver(chrome_version)

    # -- Chrome options ------------------------------------------------------
    options = uc.ChromeOptions()
    options.binary_location = binary
    options.add_argument(f"--window-size={window_size[0]},{window_size[1]}")

    # Stability on servers
    options.add_argument("--no-sandbox")               # Required when running as root
    options.add_argument("--disable-dev-shm-usage")    # Avoids /dev/shm size issues
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")

    # Reduce startup time
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-plugins")

    # Reduce background network noise
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--dns-prefetch-disable")

    # -- Build driver --------------------------------------------------------
    if chromedriver_path:
        # Use the local ARM64-compatible chromedriver directly.
        # driver_executable_path tells uc not to download/patch its own binary.
        service = Service(executable_path=chromedriver_path)
        driver = uc.Chrome(
            version_main=chrome_version,
            options=options,
            service=service,
            driver_executable_path=chromedriver_path,
        )
    else:
        # x86_64: let undetected_chromedriver download the matching binary.
        driver = uc.Chrome(version_main=chrome_version, options=options)

    # -- Block CSS / fonts / video via CDP -----------------------------------
    # Does NOT affect naturalWidth / naturalHeight of thumbnail images.
    driver.execute_cdp_cmd("Network.setBlockedURLs", {
        "urls": ["*.css", "*.woff", "*.woff2", "*.ttf", "*.eot", "*.mp4", "*.webm", "*.avi"]
    })
    driver.execute_cdp_cmd("Network.enable", {})

    logger.info("Chrome driver created successfully (version %d).", chrome_version)
    return driver


def scrape_thumbnails(
    driver: uc.Chrome,
    query: str,
    count: int = 6,
    min_width: int = 150,
    min_height: int = 150,
    page_load_timeout: int = 10,
    lazy_load_timeout: int = 5,
) -> list[str]:
    """
    Scrape Google Images and return thumbnail URLs / data-URIs for *query*.

    Thread-safe: concurrent calls are serialised by an internal lock so the
    shared driver is never used by two threads simultaneously.

    Parameters
    ----------
    driver : uc.Chrome
        The driver returned by create_driver().
    query : str
        Search term, e.g. "Fagus grandiflora".
    count : int
        Maximum number of thumbnails to return (default 6).
    min_width : int
        Minimum pixel width a thumbnail must have to be included.
    min_height : int
        Minimum pixel height a thumbnail must have to be included.
    page_load_timeout : int
        Seconds to wait for the first images to appear after navigation.
    lazy_load_timeout : int
        Seconds to wait for lazy-loaded images to reach `count` after scrolling.

    Returns
    -------
    list[str]
        Up to `count` thumbnail src values (http URLs or data-URIs).
        Returns an empty list on failure.
    """
    start_time = time.time()

    with _driver_lock:
        try:
            driver.get(f"https://www.google.com/search?q={query}&tbm=isch")

            # Wait until at least `count` images with any src are in the DOM
            WebDriverWait(driver, page_load_timeout).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "img[src]")) >= count
            )

            # Small scroll to trigger lazy loading of remaining thumbnails
            driver.execute_script("window.scrollTo(0, 600);")

            # Wait until we have enough candidate images with usable srcs
            WebDriverWait(driver, lazy_load_timeout).until(
                lambda d: len(d.find_elements(
                    By.CSS_SELECTOR, "img[src^='http'], img[src^='data:image']"
                )) >= count
            )

            img_elements = driver.find_elements(
                By.CSS_SELECTOR, "img[src^='http'], img[src^='data:image']"
            )

      
            # Single batched JS call — much faster than one call per element
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

            filtered = [
                d["src"] for d in image_data
                if d["src"] and d["w"] >= min_width and d["h"] >= min_height
            ][:count]

            elapsed = time.time() - start_time
            logger.info(
                "Query '%s' — found %d thumbnails in %.2fs.",
                query, len(filtered), elapsed,
            )
            return filtered

        except Exception as exc:
            logger.error("scrape_thumbnails failed for query '%s': %s", query, exc)
            return []


def shutdown_driver(driver: uc.Chrome) -> None:
    """
    Quit the Chrome driver and stop the virtual display (if one was started).
    Call this once when your server / process is shutting down.
    """
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
# Quick self-test — run this file directly to verify the setup
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    driver = create_driver()

    try:
        queries = ["Fagus grandiflora", "Quercus robur", "Betula pendula"]

        for q in queries:
            results = scrape_thumbnails(driver, q)
            print(f"\n--- {q} ({len(results)} results) ---")
            for link in results:
                # Truncate data-URIs for readability in terminal
                print(link if link.startswith("http") else link + "\n\n")
    finally:
        shutdown_driver(driver)
        os._exit(0)
