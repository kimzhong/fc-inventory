"""
Helper script to take UI screenshots for README documentation.
Run while the FC Inventory Tool dev server is running on http://localhost:5000
"""
import os

from playwright.sync_api import sync_playwright

OUT = os.path.join(os.path.dirname(__file__), "images")
os.makedirs(OUT, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 900, "height": 720})

    # 1. Login screen (empty)
    page.goto("http://localhost:5000")
    page.wait_for_timeout(500)
    page.evaluate("localStorage.clear(); location.reload();")
    page.wait_for_timeout(800)
    page.screenshot(path=os.path.join(OUT, "01-login.png"))
    print("Saved 01-login.png")

    # 2. Login screen filled
    page.fill("#host", "vrm.example.com")
    page.fill("#username", "admin")
    page.fill("#password", "********")
    page.wait_for_timeout(200)
    page.screenshot(path=os.path.join(OUT, "02-login-filled.png"))
    print("Saved 02-login-filled.png")

    # 3. Progress state (simulated by toggling DOM)
    page.goto("http://localhost:5000")
    page.wait_for_timeout(500)
    page.evaluate("""
        document.getElementById('form-section').style.display = 'none';
        document.getElementById('progress-section').style.display = 'block';
        document.getElementById('progress-bar').style.width = '62%';
        document.getElementById('percent-text').textContent = '62%';
        document.getElementById('step-text').textContent = 'Fetching VM detail (53/86): web-server-01...';
    """)
    page.wait_for_timeout(300)
    page.screenshot(path=os.path.join(OUT, "03-progress.png"))
    print("Saved 03-progress.png")

    # 4. Success state
    page.evaluate("""
        document.getElementById('progress-section').style.display = 'none';
        document.getElementById('result-section').style.display = 'block';
        document.getElementById('success-box').style.display = 'block';
    """)
    page.wait_for_timeout(300)
    page.screenshot(path=os.path.join(OUT, "04-success.png"))
    print("Saved 04-success.png")

    # 5. Changelog page
    page.goto("http://localhost:5000/changelog")
    page.wait_for_timeout(800)
    page.screenshot(path=os.path.join(OUT, "05-changelog.png"))
    print("Saved 05-changelog.png")

    browser.close()
    print("All screenshots saved to", OUT)
