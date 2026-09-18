"""
Configuration for the Financial Terminal app.

SEC EDGAR requires every request to identify who's making it, via a
User-Agent header with a name and an email address. This is NOT an API
key -- you don't need to sign up for anything. You just have to tell the
truth about who you are, per SEC's fair-access rules:
https://www.sec.gov/os/webmaster-faq#developers

Fill in EDGAR_CONTACT_NAME and EDGAR_CONTACT_EMAIL below with your own
name and email (or set them as environment variables of the same name --
see the .env.example file for how that works). Don't leave the
placeholder values in place; SEC does rate-limit / block generic or
fake-looking User-Agents.
"""

import os

from dotenv import load_dotenv

# Loads variables from a local .env file (if one exists) into the
# environment, so you can keep your contact info out of version control.
load_dotenv()

EDGAR_CONTACT_NAME = os.getenv("EDGAR_CONTACT_NAME", "Your Name")
EDGAR_CONTACT_EMAIL = os.getenv("EDGAR_CONTACT_EMAIL", "you@example.com")

USER_AGENT = f"{EDGAR_CONTACT_NAME} {EDGAR_CONTACT_EMAIL}"

# Where we cache SEC responses on disk so we don't re-download the same
# data every time the app reruns (Streamlit reruns your whole script
# constantly -- caching keeps this app fast and polite to SEC's servers).
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
CACHE_TTL_SECONDS = 12 * 60 * 60  # 12 hours
