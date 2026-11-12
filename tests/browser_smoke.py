"""Browser integration scenarios with local fixture services; no paid API calls.

Install requirements-dev.txt and run `python -m playwright install chromium` first.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright
from test_research import FOCAL, QUOTE, ResearchTest


def run():
    fixture = ResearchTest("test_uploaded_pdf_extraction_sources_and_matching_markdown")
    fixture.setUp()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(fixture.url)
            expect(page.locator("#connection")).to_contain_text("Сервер доступен")
            page.locator('nav a[data-page="settings"]').click()
            expect(page.locator("#catalog-state")).to_contain_text("Список получен")
            page.locator("#model-picker").focus()
            page.keyboard.press("ArrowDown")
            page.locator("#model-search").fill("luna")
            expect(page.locator(".model-option")).to_have_count(1)
            assert page.locator(".model-option").bounding_box()["height"] < 60
            assert page.locator(".model-option small").count() == 0
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
            expect(page.locator("#model-popup")).to_be_hidden()
            page.locator("#reasoning_effort").select_option("medium")
            page.locator("#max_api_calls").fill("1")
            page.locator('#settings-form button[type="submit"]').click()
            expect(page.locator("#settings-state")).to_contain_text("Сохранено")
            page.screenshot(path=str(Path(tempfile.gettempdir()) / "research-agent-030-settings.png"), full_page=True)
            page.locator('nav a[data-page="new"]').click()
            expect(page.locator("#new-model")).to_have_value("gpt-6-luna")
            expect(page.locator("#new-effort")).to_have_value("medium")
            page.locator("#new-model").select_option("gpt-6.1-sol")
            page.locator("#new-effort").select_option("high")
            page.locator("#source").fill(FOCAL)
            page.locator("#questions").fill("Что измерено?")
            page.locator("#start-button").click()
            expect(page.locator(".job-item")).to_have_count(1)
            first = fixture.call("/api/jobs")[0]
            result = fixture.settled(first)
            assert result["status"] == "completed", result
            assert result["settings"]["model"] == "gpt-6.1-sol"
            assert result["settings"]["reasoning_effort"] == "high"
            assert fixture.call("/api/settings")["model"] == "gpt-6-luna"
            expect(page.locator("#job-detail .result-state")).to_contain_text("Основной профиль готов")
            expect(page.locator("#job-detail")).to_contain_text("Расчётная стоимость")
            page.locator('#job-detail a[href^="#paper/"]').click()
            expect(page.locator("#paper-profile h1")).to_contain_text("Fixture focal article")
            page.locator('[data-section="answers"] > summary').click()
            page.locator('[data-section="answers"] .profile-entry-summary').click()
            expect(page.locator('[data-section="answers"]')).to_contain_text(QUOTE)
            page.get_by_role("button", name="Разобрать заново", exact=True).click()
            expect(page.locator("#analysis-dialog")).to_be_visible()
            page.locator("#analysis-model").fill("gpt-6.1-sol")
            page.locator("#analysis-effort").select_option("high")
            page.locator("#analysis-submit").click()
            expect(page.locator("#analysis-dialog")).to_be_hidden()
            expect(page.locator(".job-item")).to_have_count(2)
            second = fixture.call("/api/jobs")[0]
            result = fixture.settled(second)
            assert result["settings"]["reasoning_effort"] == "high", result
            assert fixture.call("/api/settings")["model"] == "gpt-6-luna"
            expect(page.locator("#job-detail")).to_contain_text("gpt-6.1-sol")
            page.locator("#job-detail").get_by_role("button", name="Продолжить разбор", exact=True).click()
            expect(page.locator(".job-item")).to_have_count(3)
            third = fixture.call("/api/jobs")[0]
            result = fixture.settled(third)
            assert result["parent_job_id"] == second["id"]
            assert result["api_usage"]["calls"] == 1, result
            assert not result["retry"]["allowed"], result
            page.locator('nav a[data-page="settings"]').click()
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(Path(tempfile.gettempdir()) / "research-agent-030-mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile horizontal overflow"
            page.locator('nav a[data-page="new"]').click()
            expect(page.locator("#new-model")).to_have_value("gpt-6-luna")
            expect(page.locator("#new-effort")).to_have_value("medium")
            assert not errors, errors
            browser.close()
        print("Browser checks passed: models, keyboard, settings, analysis, citations, reanalysis, continuation, mobile.")
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    run()
