from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", type=Path, required=True)
    parser.add_argument("--extension", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--extension-id", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    smoke_url = f"chrome-extension://{args.extension_id}/smoke/smoke.html"
    browser_args = [
        f"--disable-extensions-except={args.extension}",
        f"--load-extension={args.extension}",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--disable-crash-reporter",
        "--disable-breakpad",
        "--no-first-run",
        "--no-default-browser-check",
        "--password-store=basic",
    ]

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(args.profile),
                executable_path=str(args.chrome),
                headless=True,
                chromium_sandbox=True,
                args=browser_args,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(smoke_url, wait_until="domcontentloaded", timeout=30_000)
                page.locator(
                    'body[data-smoke-status="pass"], body[data-smoke-status="fail"]'
                ).wait_for(state="attached", timeout=60_000)
                status = page.locator("body").get_attribute("data-smoke-status")
                checks = page.locator("#smoke-log li").all_text_contents()
                error = page.locator("#smoke-error").text_content() or ""
            finally:
                context.close()
    except PlaywrightError as error:
        print(
            json.dumps(
                {"status": "blocked", "error": str(error)},
                ensure_ascii=False,
            )
        )
        return 2

    result = {"status": status, "checks": checks}
    if error:
        result["error"] = error
    print(json.dumps(result, ensure_ascii=False))
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
