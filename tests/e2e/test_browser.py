"""End-to-end checks of the web interface in a real browser."""
from pathlib import Path

import pytest

from browser_fixtures import browser, page, site  # noqa: F401,E402  (fixtures)

pytestmark = pytest.mark.e2e


def files(site):
    return sorted(p for p in site.library.rglob("*.jpg") if ".photo-sorter-trash" not in p.parts)


def open_photos(page, site, query=""):
    page.goto(f"{site.url}/#/photos{query}")
    page.wait_for_selector(".photo")
    page.wait_for_timeout(500)


def test_home_shows_the_overview_and_navigation(page, site):
    page.goto(site.url)
    page.wait_for_selector("#rail a")
    nav = page.inner_text("#rail")
    for label in ("Photos", "People"):
        assert label in nav


def test_photos_page_lists_every_photo_grouped_by_month(page, site):
    open_photos(page, site)
    assert page.locator(".photo").count() > 0
    assert page.locator("h2.month").count() >= 1
    assert page.locator(".page-head .count").inner_text()


def test_name_a_person_then_filter_by_their_chip(page, site):
    page.goto(f"{site.url}/#/people")
    page.wait_for_selector("button.strip")
    page.locator("button.strip").first.click()
    page.wait_for_selector("input.name")
    page.locator("input.name").fill("Ann")
    page.keyboard.press("Enter")
    page.wait_for_timeout(800)
    open_photos(page, site)
    chip = page.locator("button.chip", has_text="Ann")
    assert chip.count() == 1
    total = page.locator(".photo").count()
    chip.click()
    page.wait_for_timeout(800)
    assert 0 < page.locator(".photo").count() < total
    assert chip.get_attribute("aria-pressed") == "true"


def test_viewer_opens_navigates_and_closes_with_the_keyboard(page, site):
    open_photos(page, site)
    page.locator(".photo").first.locator(".pbody").click()
    page.wait_for_selector(".vimg")
    first = page.inner_text(".vside h2")
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(300)
    assert page.inner_text(".vside h2") != first
    page.keyboard.press("ArrowLeft")
    page.wait_for_timeout(300)
    assert page.inner_text(".vside h2") == first
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    assert page.locator(".vimg").count() == 0


def test_write_a_note_then_find_the_photo_by_it(page, site):
    open_photos(page, site)
    page.locator(".photo").first.locator(".pbody").click()
    page.wait_for_selector(".vimg")
    name = page.inner_text(".vside h2")
    assert "Camera details not read yet" in page.inner_text(".vside")
    note = page.get_by_label("Your note on this photo")
    note.fill("Grandma's birthday at Lake Placid")
    page.keyboard.press("ArrowRight")                    # arrow keys type in the note; they don't change the photo
    assert page.inner_text(".vside h2") == name
    page.get_by_role("button", name="Save note").click()
    page.wait_for_selector(".vnotes :text('Saved')")
    page.get_by_role("button", name="Next →").click()   # leaving and coming back shows the saved note
    page.wait_for_timeout(300)
    page.get_by_role("button", name="← Previous").click()
    page.wait_for_function("document.querySelector('.vnote') && document.querySelector('.vnote').value.includes('Placid')")
    note = page.get_by_label("Your note on this photo")
    note.fill(note.input_value() + " 2024")              # an unsaved edit is saved when the viewer closes
    page.get_by_role("button", name="Close").click()
    page.wait_for_timeout(500)
    search = page.get_by_label("Search file names, folders and notes")
    search.fill("placid")
    search.press("Enter")
    page.wait_for_function("document.querySelectorAll('.photos .photo').length === 1")
    page.locator(".photo").first.locator(".pbody").click()
    page.wait_for_selector(".vimg")
    assert page.inner_text(".vside h2") == name
    assert page.get_by_label("Your note on this photo").input_value() == "Grandma's birthday at Lake Placid 2024"
    page.get_by_role("button", name="Close").click()
    page.goto(f"{site.url}/#/photos")
    page.wait_for_selector(".photo")
    assert "Has notes 1" in page.inner_text(".seg")


def test_search_by_description(page, site):
    pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    import sqlite3

    import clip_search
    import photo_meta
    from clip_fakes import make_model

    open_photos(page, site)
    box = page.get_by_label("Search by description")
    assert box.is_disabled() and "option 5" in box.get_attribute("placeholder")   # nothing prepared yet

    make_model(clip_search.default_model_root() / "test-model")   # the private app-data folder the server also uses
    conn = sqlite3.connect(site.env.db)
    ids = [r[0] for r in conn.execute("SELECT id FROM photos ORDER BY id")]
    picks = {ids[0]: [1, 0, 0], ids[1]: [0, 1, 0], ids[2]: [0, 0, 1]}
    for photo_id in ids[:-1]:                                     # the last photo gets no vector
        photo_meta.save_embedding(conn, photo_id, "test-model", picks.get(photo_id, [-0.6, -0.6, -0.5]), half=True)
    conn.commit()
    conn.close()

    page.reload()                                                 # same address, so load the page again
    page.wait_for_selector(".photo")
    box = page.get_by_label("Search by description")
    page.wait_for_function("!document.querySelector('.field.describe').disabled")
    box.fill("green")
    box.press("Enter")
    page.wait_for_function("location.hash.includes('d=green')")
    page.wait_for_selector(".describe-note:not([hidden])")
    assert page.locator(".photos .photo").first.get_attribute("data-id") == str(ids[1])
    assert page.locator(".photos h2.month").count() == 0                         # ranked, not grouped by month
    assert "Best matches for “green” first" in page.inner_text(".page-head")
    assert "1 photo here has no search vector yet" in page.inner_text(".describe-note")
    assert page.locator(".photos .photo").count() == len(ids) - 1

    page.get_by_label("How many matches").select_option("50")
    page.wait_for_function("location.hash.includes('top=50')")
    page.locator(".photos .photo").first.locator(".pbody").click()                # the viewer works on the results
    page.wait_for_selector(".vimg")
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Clear filters").click()
    page.wait_for_selector(".photos h2.month")


def test_rename_asks_first_and_can_be_cancelled(page, site):
    open_photos(page, site)
    page.locator(".photo").first.locator(".pbody").click()
    page.wait_for_selector(".vimg")
    name = page.inner_text(".vside h2")
    before = files(site)
    page.get_by_role("button", name="Rename").click()
    page.wait_for_selector("dialog[open] .dlg:has-text('Rename this photo?')")
    page.keyboard.type("zzz")
    page.get_by_role("dialog").last.get_by_role("button", name="Cancel").click()
    page.wait_for_timeout(300)
    assert files(site) == before and page.inner_text(".vside h2") == name


def test_rename_for_real_and_a_clash_is_explained(page, site):
    open_photos(page, site)
    page.locator(".photo").first.locator(".pbody").click()
    page.wait_for_selector(".vimg")
    name = page.inner_text(".vside h2")
    folder = next(p.parent for p in files(site) if p.name == name)
    clash = next(p.name for p in folder.iterdir() if p.name != name) if len(list(folder.iterdir())) > 1 else None
    page.get_by_role("button", name="Rename").click()
    field = page.locator("dialog[open] input.field").last
    if clash:
        field.fill(clash)
        page.get_by_role("dialog").last.get_by_role("button", name="Rename").click()
        page.wait_for_timeout(500)
        assert "already" in page.locator("dialog[open] .err").last.inner_text().lower()
    field.fill("Family picnic")
    field.press("Enter")
    page.wait_for_timeout(900)
    assert (folder / "Family picnic.jpg").exists() and not (folder / name).exists()
    assert page.inner_text(".vside h2") == "Family picnic.jpg"


def test_delete_asks_first_then_moves_to_trash(page, site):
    open_photos(page, site)
    page.locator(".photo").first.locator(".pbody").click()
    page.wait_for_selector(".vimg")
    before = len(files(site))
    page.get_by_role("button", name="Delete photo").click()
    page.wait_for_selector(".dlg:has-text('Delete this photo?')")
    page.get_by_role("dialog").last.get_by_role("button", name="Cancel").click()
    page.wait_for_timeout(300)
    assert len(files(site)) == before
    page.get_by_role("button", name="Delete photo").click()
    page.get_by_role("dialog").last.get_by_role("button", name="Delete photo").click()
    page.wait_for_timeout(1000)
    assert len(files(site)) == before - 1
    assert len(list((site.library / ".photo-sorter-trash").glob("*.jpg"))) == 1


def test_select_several_and_delete_them(page, site):
    open_photos(page, site)
    bar = page.locator("#selbar")
    assert bar.is_hidden()
    ticks = page.locator(".ptick")
    ticks.nth(0).click()
    ticks.nth(2).click(modifiers=["Shift"])
    assert "3" in page.inner_text("#selbar .n")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    assert bar.is_hidden()
    before = len(files(site))
    for i in range(3):
        ticks.nth(i).click()
    page.get_by_role("button", name="Delete 3 photos").click()
    page.wait_for_selector(".dlg:has-text('Delete 3 photos?')")
    page.get_by_role("dialog").get_by_role("button", name="Cancel").click()
    page.wait_for_timeout(300)
    assert len(files(site)) == before and "3" in page.inner_text("#selbar .n")
    page.get_by_role("button", name="Delete 3 photos").click()
    page.get_by_role("dialog").get_by_role("button", name="Delete 3 photos").click()
    page.wait_for_timeout(1200)
    assert len(files(site)) == before - 3 and bar.is_hidden()
    # a remaining tile must open the photo it shows (stale-index regression)
    first = page.locator(".photo").first
    want = first.locator(".pbody").get_attribute("aria-label").split(",")[0]
    first.locator(".pbody").click()
    page.wait_for_selector(".vimg")
    assert page.inner_text(".vside h2") == want


def test_people_and_review_pages_load(page, site):
    for route in ("people", "review", "photos", "tasks"):
        page.goto(f"{site.url}/#/{route}")
        page.wait_for_timeout(600)
        assert page.locator("#main").inner_text().strip()


def test_phone_width_has_no_sideways_scroll(browser, site):
    context = browser.new_context(viewport={"width": 390, "height": 800}, is_mobile=True)
    page = context.new_page()
    for route in ("photos", "people", "review", "tasks"):
        page.goto(f"{site.url}/#/{route}")
        page.wait_for_timeout(700)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), route
    context.close()


def test_tasks_page_chooses_folders_previews_then_sorts(page, site, tmp_path):
    from conftest import make_photo
    incoming = tmp_path / "card" / "DCIM"
    for i in range(2):
        make_photo(incoming / f"new{i}.jpg", seed=500 + i, taken=f"2019:07:0{i + 1} 09:00:00")
    before = len(files(site))
    page.goto(f"{site.url}/#/tasks")
    page.get_by_role("heading", name="Sort new photos").wait_for()

    page.get_by_role("button", name="Browse…").first.click()         # choose the folder by clicking through it
    picker = page.locator("dialog")
    picker.get_by_label("Folder path").fill(str(tmp_path / "card"))
    picker.get_by_role("button", name="Go").click()
    picker.get_by_role("listitem").filter(has_text="DCIM").click()
    page.wait_for_function("document.querySelector('dialog input').value.endsWith('DCIM')")
    picker.get_by_role("button", name="Choose this folder").click()
    assert page.get_by_label("New photos", exact=True).input_value() == str(incoming)

    page.get_by_label("Library", exact=True).first.fill(str(site.library))
    page.get_by_role("button", name="Preview").first.click()
    page.get_by_role("button", name="Copy these photos").click()     # offered once the preview has finished
    page.get_by_role("button", name="Find faces in them").wait_for(timeout=60000)
    assert "Photos sorted:" in page.locator("pre.log").inner_text()
    assert len(files(site)) == before + 2 and (site.library / "2019" / "07-jul" / "new0.jpg").exists()
    assert len(list(incoming.iterdir())) == 2                        # copied, the originals stay
