#!/usr/bin/env python3
"""Auto-login to Google via Playwright Firefox persistent context.

Usage:
    python3 auto-login.py <profile-name> <target-url>

Examples:
    python3 auto-login.py firefox-gemini https://gemini.google.com
    python3 auto-login.py firefox-gemini-chat https://gemini.google.com
    python3 auto-login.py firefox-notebooklm https://notebooklm.google.com

Reads credentials from .env file (GEMGATE_GOOGLE_EMAIL, GEMGATE_GOOGLE_PASS).
"""
import asyncio
import os
import sys
from pathlib import Path

# Load .env
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

PROFILE_BASE = os.path.join(os.path.dirname(__file__), "state", "firefox-profiles")
EMAIL = os.environ.get("GEMGATE_GOOGLE_EMAIL", "")
PASSWORD = os.environ.get("GEMGATE_GOOGLE_PASS", "")


async def main():
    if not EMAIL or not PASSWORD:
        print("ERROR: Set GEMGATE_GOOGLE_EMAIL and GEMGATE_GOOGLE_PASS in .env")
        sys.exit(1)

    profile = sys.argv[1] if len(sys.argv) > 1 else "firefox-gemini"
    target = sys.argv[2] if len(sys.argv) > 2 else "https://gemini.google.com"
    profile_path = f"{PROFILE_BASE}/{profile}"

    print(f"Auto-login: profile={profile}, target={target}")

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.firefox.launch_persistent_context(
            profile_path,
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        # Go to Google login
        await page.goto("https://accounts.google.com/signin", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # Check if already logged in
        if "myaccount.google.com" in page.url or target.split("/")[2] in page.url:
            print("Already logged in!")
            await page.goto(target, wait_until="domcontentloaded")
            await asyncio.sleep(3)
            print(f"Final URL: {page.url}")
            await browser.close()
            return

        # Enter email
        print("Entering email...")
        try:
            email_input = page.locator('input[type="email"]')
            await email_input.fill(EMAIL)
            await asyncio.sleep(1)
            next_btn = page.locator('#identifierNext button, button:has-text("Next"), button:has-text("下一步")')
            await next_btn.first.click()
            await asyncio.sleep(4)
        except Exception as e:
            print(f"Email step error: {e}")

        # Enter password
        print("Entering password...")
        try:
            pwd_input = page.locator('input[type="password"]')
            await pwd_input.fill(PASSWORD)
            await asyncio.sleep(1)
            pwd_next = page.locator('#passwordNext button, button:has-text("Next"), button:has-text("下一步")')
            await pwd_next.first.click()
            await asyncio.sleep(5)
        except Exception as e:
            print(f"Password step error: {e}")

        # Check result
        print(f"After login URL: {page.url}")

        if "challenge" not in page.url and "signin" not in page.url:
            print("Login successful! Navigating to target...")
            await page.goto(target, wait_until="domcontentloaded")
            await asyncio.sleep(5)
            if target.split("/")[2] in page.url:
                print(f"SUCCESS: {page.url}")
            else:
                print(f"WARNING: Redirected to {page.url}")
        else:
            print(f"WARNING: May need manual verification at {page.url}")

        await browser.close()

    print("Done.")


asyncio.run(main())
