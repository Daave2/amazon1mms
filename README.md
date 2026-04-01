# Amazon 1MMS Dashboard Scraper

A robust, headless Playwright scraper that bypasses Amazon's Google login requirements to accurately aggregate 'Late Picks', 'UPH', and volume data directly from internal metric API endpoints (or dynamically rendered UI panels) without breaking on regional variation.

## Architecture Structure

The application has been overhauled for extreme maintainability, strictly following a core-services separation.

- `scraper.py` — The primary orchestrator. Bootstraps Chromium, creates the async workload pool, dynamically loads URLs, and waits for successful completion.
- `core/config.py` — Defines configuration schemas (using `pydantic-settings`). Exits loudly if critical environment variables are missing.
- `core/logger.py` — Handles rotating local file logging and automatic timezone localization (Europe/London).
- `core/state.py` — A thread-safe container storing queue lengths, error messages, and dynamic state cache without utilizing unprotected globals.
- `core/schemas.py` — Extremely strict `pydantic` schemas for Amazon's HTTP JSON payloads, preventing Silent 0 errors if an API property drops.
- `services/auth_service.py` — Encapsulates the Google Bypass, email/password entry, and 1MMS Merchant picker selection loop.
- `services/metrics_service.py` — Injects Amazon's internal APIs natively. Intercepts dashboard data or forces UI refreshes for target data collection natively.
- `services/forms_service.py` — Transforms normalized metric objects and pushes POST requests to the downstream Google Forms spreadsheet hook asynchronously.
- `services/chat_service.py` — Orchestrates dynamic Google Workspace Chat Cards outlining queue duration and error breakpoints seamlessly.

## Setting Up Locally

You do not need to use `config.json` manually anymore.
1. Install requirements: `pip install -r requirements.txt`
2. Create your environment configuration based on the example:
   ```bash
   cp .env.example .env
   ```
3. Update `.env` with your Amazon credentials and Google Webhook URLs.
4. Execute:
   ```bash
   python scraper.py
   ```

## Development & Code Quality

This project enforces `ruff` logic. 

**Before pushing changes** to this repository, ensure your code matches style semantics by typing:
```bash
ruff check .
ruff format .
```
The automated CI checks in GitHub Actions perform lint validations natively and will abort bad formatting requests before wasting Playwright run cycles.

## GitHub Actions

This system is configured using `.github/workflows/run-scraper.yml`.
The job is separated into two blocks:
1. `lint-code`: Verifies the code hasn't been improperly written.
2. `scrape-and-submit`: Collects variables safely masked locally securely within the Github Secrets repository and maps them automatically onto the `scraper.py` context via native Environment Variable mappings bypassing explicit config.json drops.

The persistent state (like login cookie sessions and mid-discovery arrays) rotates across artifacts automatically.
