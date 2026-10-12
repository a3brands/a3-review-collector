"""Sorting the reviews list by newest, against a simulated Google Maps page.

Before 2026-10-12 one failed sort attempt meant: press Escape (with no menu
open, which closes the reviews list), then read the Overview panel's 5 "most
relevant" preview cards as the whole check. 126 checks did exactly that.
"""
from app.collector.google_maps import GoogleMapsBackend

MENU = "[role='menuitemradio'], [role='menuitem']"


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    def count(self):
        if self.selector == GoogleMapsBackend.SIGNIN_PROMPT_DISMISS:
            return 1 if self.page.signin_prompt else 0
        if self.selector == MENU:
            return 3 if self.page.menu_open else 0
        if "Sort" in self.selector:
            return 1
        if self.selector == "div[data-review-id]":
            return self.page.cards
        if "Reviews" in self.selector or "moreReviews" in self.selector:
            return 1
        return 0

    def is_visible(self):
        return True

    def click(self, timeout=None):
        if self.selector == GoogleMapsBackend.SIGNIN_PROMPT_DISMISS:
            self.page.signin_prompt = False
            return
        if self.page.signin_prompt:
            return  # the pop-up covers the page; the click lands on it
        if "Sort" in self.selector:
            self.page.sort_clicks += 1
            if self.page.limited:
                self.page.signin_prompt = True  # Google gates sorting behind sign-in
                return
            if self.page.sort_clicks >= self.page.menu_opens_on_click:
                self.page.menu_open = True
        elif "Reviews" in self.selector or "moreReviews" in self.selector:
            self.page.tab_clicks += 1
            self.page.cards = 20

    def nth(self, index):
        page = self.page

        class Item:
            def inner_text(self):
                return ["Most relevant", "Newest", "Highest rating"][index]

            def click(self, timeout=None):
                page.menu_open = False
                if index == 1:
                    page.sorted_newest = True
        return Item()


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key):
        assert key == "Escape"
        self.page.escapes += 1
        if self.page.menu_open:
            self.page.menu_open = False
        else:
            # Google Maps: Escape with no menu open leaves the reviews list.
            self.page.cards = 5


class FakePage:
    def __init__(self, menu_opens_on_click=1):
        self.menu_opens_on_click = menu_opens_on_click
        self.menu_open = False
        self.sort_clicks = self.tab_clicks = self.escapes = 0
        self.cards = 20
        self.sorted_newest = False
        self.signin_prompt = False
        self.limited = False
        self.keyboard = FakeKeyboard(self)
        self.mouse = type("M", (), {"click": lambda *a, **k: None})()

    def locator(self, selector):
        return FakeLocator(self, selector)

    def wait_for_selector(self, selector, timeout=None):
        if selector == MENU and not self.menu_open:
            raise TimeoutError("menu did not render")

    def wait_for_timeout(self, ms):
        pass


def backend():
    b = GoogleMapsBackend.__new__(GoogleMapsBackend)
    return b


def test_sorts_on_the_first_try_without_touching_escape():
    page, notes = FakePage(menu_opens_on_click=1), []
    assert backend()._sort_by_newest(page, notes) is True
    assert page.sorted_newest and page.escapes == 0 and page.cards == 20
    assert notes == []


def test_a_menu_that_opens_on_the_second_click_still_sorts():
    page, notes = FakePage(menu_opens_on_click=2), []
    assert backend()._sort_by_newest(page, notes) is True
    assert page.sort_clicks == 2 and page.sorted_newest
    assert page.cards == 20, "the reviews list must still be open"
    assert notes == ["Sorted by newest on try 2."]


def test_a_menu_that_never_opens_gives_up_after_three_tries_and_keeps_the_list_open():
    page, notes = FakePage(menu_opens_on_click=99), []
    assert backend()._sort_by_newest(page, notes) is False
    assert page.sort_clicks == 3
    assert page.escapes == 0, "no Escape with no menu open"
    assert page.cards == 20
    assert "3 tries" in notes[-1]


def test_dismiss_menu_closes_an_open_menu_and_ignores_a_closed_one():
    page = FakePage()
    GoogleMapsBackend._dismiss_menu(page)
    assert page.escapes == 0 and page.cards == 20
    page.menu_open = True
    GoogleMapsBackend._dismiss_menu(page)
    assert page.escapes == 1 and not page.menu_open and page.cards == 20


def test_open_reviews_reopens_the_list_if_a_failed_sort_left_the_overview():
    page, notes = FakePage(menu_opens_on_click=99), []
    b = backend()
    original = b._sort_by_newest

    def sort_then_lose_list(p, n):
        ok = original(p, n)
        p.cards = 5  # as if Google had dropped back to the Overview panel
        return ok
    b._sort_by_newest = sort_then_lose_list
    b._open_reviews(page, type("B", (), {"name": "BMW"})(), notes)
    assert page.tab_clicks == 2 and page.cards == 20
    assert "Re-opened the reviews list after the sort attempt closed it." in notes


def test_googles_sign_in_pop_up_is_dismissed_so_the_sort_can_open():
    page, notes = FakePage(menu_opens_on_click=1), []
    page.signin_prompt = True
    assert backend()._sort_by_newest(page, notes) is True
    assert page.sorted_newest and not page.signin_prompt
    assert notes == ["Closed Google's sign-in prompt."]


def test_a_limited_listing_stops_after_one_click_and_says_why():
    page, notes = FakePage(), []
    page.limited = True
    assert backend()._sort_by_newest(page, notes) is False
    assert page.sort_clicks == 1 and not page.signin_prompt and page.cards == 20
    assert GoogleMapsBackend.SORT_GATED_NOTE in notes


class _Result:
    def __init__(self, n, gated):
        self.reviews = list(range(n))
        self.notes = [GoogleMapsBackend.SORT_GATED_NOTE] if gated else []


def _collect_with(results, monkeypatch):
    import app.collector.google_maps as gm
    monkeypatch.setattr(gm.time, "sleep", lambda s: None)
    b = backend()
    calls = iter(results)
    b._collect_attempt = lambda *a, **k: next(calls)
    return b.collect(object(), 20)


def test_a_limited_first_load_is_loaded_again(monkeypatch):
    res = _collect_with([_Result(5, True), _Result(20, False)], monkeypatch)
    assert len(res.reviews) == 20
    assert res.notes[0].startswith("An earlier page load was a limited listing")


def test_a_third_load_is_tried_when_the_second_is_limited_too(monkeypatch):
    res = _collect_with([_Result(5, True), _Result(5, True), _Result(20, False)], monkeypatch)
    assert len(res.reviews) == 20


def test_three_limited_loads_keep_the_first_and_stop(monkeypatch):
    res = _collect_with([_Result(5, True), _Result(5, True), _Result(5, True)], monkeypatch)
    assert len(res.reviews) == 5 and res.notes.count("Another page load was limited too.") == 2


def test_a_full_first_load_is_not_repeated(monkeypatch):
    res = _collect_with([_Result(20, False)], monkeypatch)
    assert len(res.reviews) == 20
