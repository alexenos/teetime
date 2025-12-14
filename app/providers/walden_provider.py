import asyncio
from datetime import date, datetime, time, timedelta

from playwright.async_api import Browser, Page, Playwright, async_playwright

from app.config import settings
from app.providers.base import BookingResult, ReservationProvider


class WaldenGolfProvider(ReservationProvider):
    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None
        self._logged_in: bool = False

    async def _ensure_browser(self) -> Page:
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        if self._browser is None:
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )

        if self._page is None:
            self._page = await self._browser.new_page()

        return self._page

    async def login(self) -> bool:
        if self._logged_in:
            return True

        page = await self._ensure_browser()

        try:
            await page.goto(f"{settings.walden_base_url}/web/pages/login")
            await page.wait_for_load_state("networkidle")

            member_input = page.locator(
                'input[name="_com_liferay_login_web_portlet_LoginPortlet_login"]'
            )
            password_input = page.locator(
                'input[name="_com_liferay_login_web_portlet_LoginPortlet_password"]'
            )

            await member_input.fill(settings.walden_member_number)
            await password_input.fill(settings.walden_password)

            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle")

            if "login" not in page.url.lower():
                self._logged_in = True
                return True

            return False

        except Exception as e:
            print(f"Login error: {e}")
            return False

    async def book_tee_time(
        self,
        date: date,
        time: time,
        num_players: int,
        fallback_window_minutes: int = 30,
    ) -> BookingResult:
        if not await self.login():
            return BookingResult(
                success=False,
                error_message="Failed to log in to Walden Golf",
            )

        page = await self._ensure_browser()

        try:
            tee_times_url = f"{settings.walden_base_url}/web/pages/golf"
            await page.goto(tee_times_url)
            await page.wait_for_load_state("networkidle")

            date_str = date.strftime("%Y-%m-%d")
            time_str = time.strftime("%H:%M")

            print(f"Attempting to book: {date_str} at {time_str} for {num_players} players")

            return BookingResult(
                success=False,
                error_message="Booking flow not yet implemented - requires site-specific selectors",
            )

        except Exception as e:
            return BookingResult(
                success=False,
                error_message=f"Booking error: {str(e)}",
            )

    async def get_available_times(self, date: date) -> list[time]:
        if not await self.login():
            return []

        return []

    async def cancel_booking(self, confirmation_number: str) -> bool:
        return False

    async def close(self) -> None:
        if self._page:
            await self._page.close()
            self._page = None

        if self._browser:
            await self._browser.close()
            self._browser = None

        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

        self._logged_in = False


class MockWaldenProvider(ReservationProvider):
    def __init__(self) -> None:
        self._logged_in = False

    async def login(self) -> bool:
        self._logged_in = True
        return True

    async def book_tee_time(
        self,
        date: date,
        time: time,
        num_players: int,
        fallback_window_minutes: int = 30,
    ) -> BookingResult:
        await asyncio.sleep(0.5)

        return BookingResult(
            success=True,
            booked_time=time,
            confirmation_number=f"MOCK-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        )

    async def get_available_times(self, date: date) -> list[time]:
        base_time = datetime.combine(date, datetime.min.time().replace(hour=7))
        times = []
        for i in range(20):
            slot_time = (base_time + timedelta(minutes=i * 10)).time()
            times.append(slot_time)
        return times

    async def cancel_booking(self, confirmation_number: str) -> bool:
        return True

    async def close(self) -> None:
        pass
