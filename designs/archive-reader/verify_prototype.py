"""HTTP-only visual smoke test for the mock Archive Reader prototype."""

from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import sync_playwright


PROJECT_DIR = Path(__file__).resolve().parent
URL = os.environ.get("ARCHIVE_READER_URL", "http://127.0.0.1:4311/archive-reader/")
KNOWN_CHROMIUM = Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"


def main() -> None:
    errors: list[str] = []
    with sync_playwright() as playwright:
        launch_kwargs: dict[str, object] = {"headless": True}
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        if not executable and KNOWN_CHROMIUM.is_file():
            executable = str(KNOWN_CHROMIUM)
        if executable:
            launch_kwargs["executable_path"] = executable

        browser = playwright.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={"width": 1440, "height": 960}, device_scale_factor=1)
        page.set_default_timeout(10_000)
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: errors.append(str(error)))

        page.goto(URL, wait_until="domcontentloaded")
        page.locator("[data-screen-label='Archive Reader desktop prototype']").wait_for()
        assert page.locator(".conversation-item").count() == 4
        assert page.locator(".metadata-drawer").is_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(PROJECT_DIR / "preview-light.png"), full_page=True)

        branch_toggle = page.get_by_role("button", name="2 个替代回答 未选中的会话分支")
        branch_toggle.click()
        assert page.locator(".branch-option").count() == 2

        page.get_by_role("tab", name="快照").click()
        assert page.locator(".snapshot-card").count() == 3

        page.get_by_role("button", name="切换到深色主题").click()
        assert page.locator("html").get_attribute("data-theme") == "dark"
        page.wait_for_timeout(220)
        active_background = page.locator(".conversation-item.is-active").evaluate(
            "element => getComputedStyle(element).backgroundColor"
        )
        assert active_background not in {"rgb(255, 255, 255)", "oklab(1 0 0)"}
        page.screenshot(path=str(PROJECT_DIR / "preview-dark.png"), full_page=True)

        search = page.get_by_role("searchbox", name="搜索会话")
        search.fill("附件")
        assert page.locator(".conversation-item").count() == 2
        search.fill("")

        page.get_by_role("button", name="筛选", exact=True).click()
        page.locator(".filter-option").filter(has_text="有分支").click()
        assert page.locator(".conversation-item").count() == 2
        page.locator(".filter-option").filter(has_text="有分支").click()
        assert page.locator(".conversation-item").count() == 4

        page.get_by_role("tab", name="洞察", exact=True).click()
        page.locator("[data-screen-label='Archive deterministic insights dashboard']").wait_for()
        assert page.locator("[data-zone='deterministic']").count() >= 4
        assert page.locator("[data-zone='experimental']").count() == 1
        assert page.locator("[data-zone='deterministic'] [data-zone='experimental']").count() == 0
        assert page.locator(".experiment-card").count() == 3
        assert page.get_by_text("候选洞察，不是个人事实", exact=True).is_visible()

        page.get_by_role("button", name="选择 2025 年").click()
        assert page.get_by_role("button", name="选择 2025 年").get_attribute("aria-pressed") == "true"
        page.get_by_role("button", name="选择 2026 年").click()

        page.get_by_role("button", name="切换到浅色主题").click()
        page.wait_for_timeout(220)
        assert page.locator("html").get_attribute("data-theme") == "light"
        page.locator(".insights-scroll").evaluate("element => { element.scrollTop = 0; }")
        page.screenshot(path=str(PROJECT_DIR / "preview-insights-light.png"), full_page=True)

        page.locator(".insights-scroll").evaluate("element => { element.scrollTop = element.scrollHeight; }")
        page.wait_for_timeout(120)
        page.get_by_role("button", name="切换到深色主题").click()
        page.wait_for_timeout(220)
        assert page.locator("html").get_attribute("data-theme") == "dark"
        page.screenshot(path=str(PROJECT_DIR / "preview-insights-dark.png"), full_page=True)

        page.get_by_role("button", name="查看待审核队列").click()
        page.locator(".toast").filter(has_text="3 个 LLM 候选").wait_for()

        mobile = browser.new_page(viewport={"width": 720, "height": 900}, device_scale_factor=1)
        mobile.set_default_timeout(10_000)
        mobile.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        mobile.on("pageerror", lambda error: errors.append(str(error)))
        mobile.goto(URL, wait_until="domcontentloaded")
        mobile.locator("[data-screen-label='Archive Reader desktop prototype']").wait_for()
        mobile.get_by_role("button", name="打开会话列表").click()
        mobile.wait_for_function("document.querySelector('.sidebar').getBoundingClientRect().left >= -1")
        mobile.screenshot(path=str(PROJECT_DIR / "preview-mobile.png"), full_page=True)

        browser.close()

    if errors:
        raise RuntimeError("Browser console/runtime errors:\n" + "\n".join(errors))

    print("PASS: reader and insights light/dark, deterministic/experimental boundary, charts, filters, and mobile drawer checks")


if __name__ == "__main__":
    main()
