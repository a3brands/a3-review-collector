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
        if "Sort" in self.selector:
            self.page.sort_clicks += 1
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
