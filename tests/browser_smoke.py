"""Browser integration scenarios with local fixture services; no paid API calls.

Install requirements-dev.txt and run `python -m playwright install chromium` first.
"""
import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright
from test_research import ABSTRACT, FOCAL, QUOTE, ResearchTest, pdf_bytes


def check_layout(page, label):
    overflow = page.evaluate("""() => [...document.querySelectorAll('body *')]
        .filter(node => node.getBoundingClientRect().right > innerWidth + 1)
        .slice(0, 8).map(node => node.tagName + '#' + node.id + '.' + node.className)""")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"{label}: horizontal overflow {overflow}"


def check_design(page, fixture, paper_id, screenshots):
    screenshots.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(fixture.url + "#jobs")
    expect(page.locator("#job-detail .step-list li")).to_have_count(5)
    expect(page.locator("#job-detail .job-history")).not_to_have_attribute("open", "")
    page.locator(".job-history > summary").click()
    expect(page.locator(".events")).to_be_visible()
    page.locator(".job-settings > summary").click()
    expect(page.locator(".snapshot")).to_be_visible()
    # A fresh status response must retain each disclosure independently.
    page.evaluate('async () => { detailKey = ""; await renderDetail(); }')
    expect(page.locator(".events")).to_be_visible()
    expect(page.locator(".snapshot")).to_be_visible()
    page.locator(".job-history > summary").click()
    page.locator(".job-settings > summary").click()
    page.locator(".skip-link").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#main-content")).to_be_focused()
    assert page.url.endswith("#jobs"), "Skip link must not change the current page"

    # Exercise long, genuine API data rather than replacing rendered DOM.
    long_title = "Calibrated microwave spectra and superconducting quantum systems: " * 3
    long_job = fixture.uploaded(pdf_bytes(long_title))
    assert fixture.settled(long_job)["status"] == "completed"
    for width in (1440, 768, 390, 320):
        page.set_viewport_size({"width": width, "height": 1000 if width > 768 else 844})
        for route in ("new", "jobs", "library", "paper/" + paper_id, "settings"):
            print(f"Checking {width}px {route.split('/')[0]}", flush=True)
            page.goto(fixture.url + "#" + route)
            expect(page.locator("#connection")).to_contain_text("Сервер доступен")
            if route == "jobs":
                expect(page.locator("#job-detail .step-list li")).to_have_count(5)
            if route == "library":
                expect(page.locator(".library-row")).to_have_count(2)
                page.locator(".library-abstract > summary").first.click()
                expect(page.locator(".library-abstract p").first).to_contain_text(ABSTRACT)
            if route.startswith("paper/"):
                expect(page.locator(".profile-authors")).to_contain_text("Physicist")
                expect(page.locator(".profile-folder")).to_be_hidden()
                for section in ("references", "analogs"):
                    details = page.locator(f'[data-section="{section}"]')
                    if not details.evaluate("node => node.open"):
                        details.locator(":scope > summary").click()
                analog = page.locator('[data-section="analogs"] .profile-entry-details').first
                if not analog.evaluate("node => node.open"):
                    analog.locator("summary").click()
                page.locator(".reference-citations > summary").first.click()
            if route == "settings":
                expect(page.locator("#catalog-state")).to_contain_text("Список получен")
            page.evaluate("document.fonts.ready")
            assert page.evaluate('document.fonts.check(\'14px "Inter"\') && document.fonts.check(\'700 30px "PT Serif"\')')
            check_layout(page, f"{width}px {route}")
            if width in (1440, 390):
                name = "profile" if route.startswith("paper/") else route
                page.evaluate("window.scrollTo(0, 0)")
                page.screenshot(path=str(screenshots / f"{width}-{name}.png"), full_page=True)
        page.locator("#library-toggle").click()
        expect(page.locator("#library-shortcuts a")).to_have_count(2)
        check_layout(page, f"{width}px library menu")
        page.keyboard.press("Escape")
        expect(page.locator("#library-shortcuts")).to_be_hidden()
        expect(page.locator("#library-toggle")).to_be_focused()
        page.locator("#library-toggle").click()
        page.locator(".app-header").click(position={"x": 3, "y": 3})
        expect(page.locator("#library-shortcuts")).to_be_hidden()
        page.locator("#library-toggle").click()
        page.locator(f'#library-shortcuts a[href="#paper/{paper_id}"]').click()
        expect(page.locator("#paper-profile h1")).to_contain_text("Fixture focal article")
        expect(page.locator("#library-shortcuts")).to_be_hidden()
        page.get_by_role("button", name="Разобрать заново", exact=True).click()
        expect(page.locator("#analysis-dialog")).to_be_visible()
        check_layout(page, f"{width}px analysis dialog")
        if width in (1440, 390):
            page.screenshot(path=str(screenshots / f"{width}-dialog.png"))
        page.keyboard.press("Escape")
        expect(page.locator("#analysis-dialog")).to_be_hidden()

    page.set_viewport_size({"width": 1440, "height": 1000})
    for name, selector in (
        ("document", '#paper-profile a[href*="profile.html"]'),
        ("manuscript", '#paper-profile a[href*="manuscript.html"]'),
        ("citation", '.reference-mention'),
    ):
        if name == "citation":
            citations = page.locator(".reference-citations").first
            if not citations.evaluate("node => node.open"):
                citations.locator("summary").click()
        with page.expect_popup() as opened:
            page.locator(selector).first.click()
        document = opened.value
        document.wait_for_load_state()
        expect(document.locator(".profile-document")).to_be_visible()
        if name == "citation":
            expect(document.locator("mark#citation")).to_be_visible()
        for width in (1440, 390):
            document.set_viewport_size({"width": width, "height": 1000 if width == 1440 else 844})
            document.evaluate("document.fonts.ready")
            check_layout(document, f"{width}px {name}")
            document.screenshot(path=str(screenshots / f"{width}-{name}.png"), full_page=True)
        document.close()


def run(channel=None, screenshots=None):
    fixture = ResearchTest("test_uploaded_pdf_extraction_sources_and_matching_markdown")
    fixture.setUp()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=channel)
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
            page.locator("#depth").select_option("full")
            page.locator("#source").fill(FOCAL)
            page.locator("#questions").fill("Что измерено?")
            presets = page.locator("[data-question]")
            expect(presets).to_have_count(4)
            question = presets.first.inner_text()
            presets.first.click()
            presets.first.click()
            expect(page.locator("#questions")).to_have_value("Что измерено?\n" + question)
            page.locator("#start-button").click()
            expect(page.locator(".job-item")).to_have_count(1)
            first = fixture.call("/api/jobs")[0]
            result = fixture.settled(first)
            assert result["status"] == "completed", result
            assert result["settings"]["model"] == "gpt-6.1-sol"
            assert result["settings"]["reasoning_effort"] == "high"
            assert result["depth"] == "full"
            assert fixture.call("/api/settings")["model"] == "gpt-6-luna"
            expect(page.locator("#job-detail .result-state")).to_contain_text("Основной профиль готов")
            expect(page.locator("#job-detail")).to_contain_text("Расчётная стоимость")
            page.locator('#job-detail a[href^="#paper/"]').click()
            expect(page.locator("#paper-profile h1")).to_contain_text("Fixture focal article")
            coverage = page.locator('[data-section="coverage"]')
            coverage.locator(":scope > summary").click()
            expect(coverage.locator(".step-list li")).to_have_count(5)
            expect(coverage.locator(".coverage-facts")).to_contain_text("Выбран полный текст")
            expect(coverage.locator('[data-section="coverage-details"] .profile-section-content')).to_be_hidden()
            page.locator('[data-section="claims"] > summary').click()
            expect(page.locator('[data-section="claims"] .profile-entry-summary')).to_contain_text("Экспериментальное измерение")
            page.locator('[data-section="answers"] > summary').click()
            page.locator('[data-section="answers"] .profile-entry-summary').first.click()
            expect(page.locator('[data-section="answers"]')).to_contain_text(QUOTE)
            page.get_by_role("button", name="Разобрать заново", exact=True).click()
            expect(page.locator("#analysis-dialog")).to_be_visible()
            expect(page.locator("#analysis-depth")).to_have_value("full")
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
            check_design(page, fixture, first["paper_id"], screenshots or Path(tempfile.gettempdir()) / "research-agent-design")
            assert not errors, errors
            browser.close()
        print("Browser checks passed: models, keyboard, settings, analysis, reanalysis, continuation; all pages at 1440/768/390/320px; menus, disclosures, fonts, Markdown, TeX and citations.")
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", help="Installed browser channel, e.g. msedge; defaults to Playwright Chromium")
    parser.add_argument("--screenshots", type=Path, help="Directory for design review screenshots")
    args = parser.parse_args()
    run(args.channel, args.screenshots)
