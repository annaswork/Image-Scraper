# Scraper using Selenium for windows

import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium_stealth import stealth
from webdriver_manager.chrome import ChromeDriverManager

def scrape_google_images_stealth(query):
    options = Options()
    options.add_argument("--start-maximized")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    # Apply Stealth to hide Selenium fingerprints
    stealth(driver,
        languages=["en-US", "en"],
        vendor="Google Inc.",
        platform="Win32",
        webgl_vendor="Intel Inc.",
        renderer="Intel Iris OpenGL Engine",
        fix_hairline=True,
    )

    try:
        url = f"https://www.google.com/search?q={query}&tbm=isch"
        driver.get(url)

        # 1. Manual CAPTCHA Pause (The most reliable free way)
        if "sorry" in driver.current_url:
            print("Action Required: Solve the CAPTCHA in the browser window!")
            while "sorry" in driver.current_url:
                time.sleep(2)
        
        # 2. Wait for the page to actually render
        time.sleep(3)

        # 3. Improved Selector
        # We look for <img> tags that have an 'alt' attribute or specific parent structures
        # Google's current thumbnail structure often uses 'img.ln8vrb' or just generic thumbnails
        thumbnails = driver.find_elements(By.CSS_SELECTOR, "img[src^='data:image'], img[src^='http']")
        
        image_urls = []
        for img in thumbnails:
            src = img.get_attribute('src')
            if src:
                # Filter out small icons like the Google logo (usually < 2000 chars if base64)
                if len(src) > 500: 
                    image_urls.append(src)

        print(f"Found {len(image_urls)} potential images!")
        return image_urls

    finally:
        driver.quit()

if __name__ == "__main__":
    results = scrape_google_images_stealth("sunflower")
    for link in results[:5]:
        print(link[:70] + "...")
