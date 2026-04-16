# Scraper using uc (undetected chromedriver) for Windows

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
import time
import os

def create_driver():
    options = uc.ChromeOptions()

    # --- Position off-screen ---
    options.add_argument("--window-position=2000,2000")
    options.add_argument("--window-size=1280,720")

    # --- Reduce startup time ---
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-plugins")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")

    # --- Reduce network overhead ---
    options.add_argument("--dns-prefetch-disable")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")

    # --- Block CSS/fonts via CDP (works with undetected_chromedriver) ---
    driver = uc.Chrome(version_main=146, options=options)

    # Block stylesheets and fonts using CDP — does NOT affect image src/dimensions
    driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": [
        "*.css", "*.woff", "*.woff2", "*.ttf", "*.eot",
        "*.mp4", "*.webm", "*.avi"
    ]})
    driver.execute_cdp_cmd("Network.enable", {})

    return driver


def scrape_top_6_big_thumbnails(query, driver, min_width=150, min_height=150):
    start_time = time.time()

    try:
        driver.get(f"https://www.google.com/search?q={query}&tbm=isch")

        # Wait until at least 6 images with src are present
        WebDriverWait(driver, 10).until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR, "img[src]")) >= 6
        )

        # Small scroll to trigger lazy loading
        driver.execute_script("window.scrollTo(0, 600);")

        # Wait until we have enough candidate images
        WebDriverWait(driver, 5).until(
            lambda d: len(d.find_elements(
                By.CSS_SELECTOR, "img[src^='http'], img[src^='data:image']"
            )) >= 6
        )

        img_elements = driver.find_elements(
            By.CSS_SELECTOR, "img[src^='http'], img[src^='data:image']"
        )

        # Single batched JS call for all dimensions
        image_data = driver.execute_script("""
            var imgs = arguments[0];
            var results = [];
            for (var i = 0; i < imgs.length; i++) {
                results.push({
                    src: imgs[i].src,
                    w: imgs[i].naturalWidth,
                    h: imgs[i].naturalHeight
                });
            }
            return results;
        """, img_elements)

        filtered_links = [
            d['src'] for d in image_data
            if d['src'] and d['w'] >= min_width and d['h'] >= min_height
        ][:6]

        end_time = time.time()
        print(f"Scraping complete in {end_time - start_time:.2f}s")
        print(f"Found {len(filtered_links)} large thumbnails.")
        return filtered_links

    except Exception as e:
        print(f"An error occurred: {e}")
        return []


if __name__ == "__main__":
    driver = create_driver()  # Pay startup cost once

    try:
        # First request
        results = scrape_top_6_big_thumbnails("Rosa chinensis Jacq", driver)
        for link in results:
            print(f"{link}\n")

    finally:
        try:
            driver.quit()
        except:
            pass

    os._exit(0)
