import asyncio
from datetime import datetime, timedelta

import pytest

import scraper
from core.state import ScraperState
from core.work_items import WorkItem
from services import metrics_service


class FakeRequestResponse:
    def __init__(self, status, payload=None, url="https://example.com/api/summationMetrics?merchantIds%5B%5D=DISCOVERED&endRange%5Bhour%5D=9"):
        self.status = status
        self._payload = payload or {}
        self.url = url

    async def json(self):
        return self._payload


class FakeRequestClient:
    def __init__(self, responses=None):
        self.responses = list(responses or [500])

    async def get(self, _url, timeout):
        assert timeout == 45_000
        next_response = self.responses.pop(0) if self.responses else 500
        if isinstance(next_response, tuple):
            status, payload = next_response
            return FakeRequestResponse(status, payload, url=_url)
        status = next_response
        return FakeRequestResponse(status, {"metrics": {"OrdersShopped_V2": 8}}, url=_url)


class FakeContext:
    def __init__(self, page=None, fail_on_close=False, request_client=None):
        self.request = request_client or FakeRequestClient()
        self._page = page
        self.fail_on_close = fail_on_close
        self.new_page_calls = 0

    def set_default_navigation_timeout(self, _timeout):
        pass

    def set_default_timeout(self, _timeout):
        pass

    async def new_page(self):
        self.new_page_calls += 1
        if self._page is None:
            raise AssertionError("new_page should not have been called for this context")
        return self._page

    async def close(self):
        if self.fail_on_close:
            raise RuntimeError("context already closed")


class FakeResponse:
    def __init__(self, payload):
        self.status = 200
        self.url = (
            "https://example.com/api/metrics?merchantIds%5B%5D=DISCOVERED&endRange%5Bhour%5D=9"
        )
        self._payload = payload

    async def json(self):
        return self._payload


class FakeExpectResponse:
    def __init__(self, response):
        self.value = asyncio.Future()
        self.value.set_result(response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeLocator:
    @property
    def first(self):
        return self

    async def is_visible(self):
        return True

    async def dispatch_event(self, _event_name):
        return None


class FakeExpectation:
    async def to_be_visible(self, timeout):
        assert timeout
        return None


class FakePage:
    def __init__(self, response_payload=None, fail_on_close=False):
        self.url = "https://example.com/dashboard"
        self.fail_on_close = fail_on_close
        self.context = None
        self._response_payload = response_payload or {
            "metrics": {
                "OrdersShopped_V2": 8,
                "RequestedQuantity_V2": 80,
                "PickedUnits_V2": 75,
                "AverageUPH_V2": 70,
                "LatePicksRate": 1.5,
                "ItemNotFoundRate_V2": 2.0,
                "ItemFoundRate_V2": 98.0,
                "OrderCancellations": 1,
                "TimeAvailable_V2": 3_600_000,
            }
        }

    def locator(self, _selector):
        return FakeLocator()

    async def goto(self, _url, timeout, wait_until):
        assert timeout
        assert wait_until
        return None

    def get_by_role(self, _role, name=None):
        assert name == "Refresh"
        return FakeLocator()

    def expect_response(self, _predicate, timeout):
        assert timeout == 45_000
        return FakeExpectResponse(FakeResponse(self._response_payload))

    def is_closed(self):
        return False

    async def close(self):
        if self.fail_on_close:
            raise RuntimeError("page already closed")


class FakeBrowser:
    def __init__(self, page=None, fail_on_context_close=False, request_client=None):
        self._page = page
        self.fail_on_context_close = fail_on_context_close
        self.request_client = request_client
        self.created_contexts: list[FakeContext] = []

    async def new_context(self, storage_state):
        assert storage_state == {"cookies": [{"name": "session"}]}
        context = FakeContext(
            self._page,
            fail_on_close=self.fail_on_context_close,
            request_client=self.request_client,
        )
        self.created_contexts.append(context)
        if self._page is not None:
            self._page.context = context
        return context


def london_datetime(year, month, day, hour=0, minute=0):
    if hasattr(scraper.LOCAL_TIMEZONE, "localize"):
        return scraper.LOCAL_TIMEZONE.localize(datetime(year, month, day, hour, minute))
    return datetime(year, month, day, hour, minute, tzinfo=scraper.LOCAL_TIMEZONE)


def test_filter_stores_to_live_dropdown_queues_all_live_stores_and_uses_live_merchant_ids():
    urls_data = [
        {"store_name": "Belle Vale Morrisons", "merchant_id": "", "marketplace_id": "", "dropdown_name": "Belle Vale"},
        {
            "store_name": "Morrisons Cardiff Tygals",
            "merchant_id": "",
            "marketplace_id": "",
            "dropdown_name": "Cardiff Tygals",
        },
        {
            "store_name": "Morrisons Welling",
            "merchant_id": "STALE-ID",
            "marketplace_id": "",
            "dropdown_name": "Welling",
        },
        {
            "store_name": "Morrisons Welwyn",
            "merchant_id": "LIVE-WELWYN-ID",
            "marketplace_id": "",
            "dropdown_name": "Welwyn",
        },
        {"store_name": "Morrisons Missing Store", "merchant_id": "", "marketplace_id": "", "dropdown_name": "Missing"},
    ]
    available_stores = [
        {
            "store_name": "Belle Vale",
            "normalized_name": "belle vale",
            "merchant_id": "A1KDGRVT6JAV6B",
        },
        {
            "store_name": "Cardiff Tyglass",
            "normalized_name": "cardiff tyglass",
            "merchant_id": "A3W2L835GZRAX2",
        },
        {
            "store_name": "Welling",
            "normalized_name": "welling",
            "merchant_id": "LIVE-WELLING-ID",
        },
        {
            "store_name": "Welwyn Garden",
            "normalized_name": "welwyn garden",
            "merchant_id": "LIVE-WELWYN-ID",
        },
    ]

    filtered, skipped = scraper.filter_stores_to_live_dropdown(urls_data, available_stores)

    assert [store["store_name"] for store in filtered] == [
        "Belle Vale Morrisons",
        "Morrisons Cardiff Tygals",
        "Morrisons Welling",
        "Morrisons Welwyn",
    ]
    assert [store["merchant_id"] for store in filtered] == [
        "A1KDGRVT6JAV6B",
        "A3W2L835GZRAX2",
        "LIVE-WELLING-ID",
        "LIVE-WELWYN-ID",
    ]
    assert [store["dropdown_name"] for store in filtered] == [
        "Belle Vale",
        "Cardiff Tyglass",
        "Welling",
        "Welwyn Garden",
    ]
    assert [store["store_name"] for store in skipped] == ["Morrisons Missing Store"]


def test_route_store_work_items_prefers_fast_path_when_template_and_merchant_id_exist():
    state = ScraperState()
    state.cache_template_available_at_start = True
    state.cache.merchant_id_cache["Cached Merchant Store"] = "CACHE-MID"

    fast_path_items, ui_items = scraper.route_store_work_items(
        [
            {"store_name": "Ready Store", "dropdown_name": "Ready", "merchant_id": "MID-1", "marketplace_id": ""},
            {"store_name": "Cached Merchant Store", "dropdown_name": "Cached", "merchant_id": "", "marketplace_id": ""},
            {"store_name": "Needs UI Store", "dropdown_name": "Needs UI", "merchant_id": "", "marketplace_id": ""},
        ],
        state,
    )

    assert [item.store_name for item in fast_path_items] == ["Ready Store", "Cached Merchant Store"]
    assert [item.merchant_id for item in fast_path_items] == ["MID-1", "CACHE-MID"]
    assert [item.store_name for item in ui_items] == ["Needs UI Store"]
    assert state.fast_path_eligible_at_start == 2
    assert state.ui_routed_at_start == 1


def test_route_store_work_items_disables_fast_path_routing_when_template_missing_at_start():
    state = ScraperState()
    state.cache_template_available_at_start = False
    state.cache.api_url_template = "https://example.com/metrics?merchantIds%5B%5D={merchant_id}"
    state.cache.merchant_id_cache["Cached Merchant Store"] = "CACHE-MID"

    fast_path_items, ui_items = scraper.route_store_work_items(
        [
            {"store_name": "Ready Store", "dropdown_name": "Ready", "merchant_id": "MID-1", "marketplace_id": ""},
            {"store_name": "Cached Merchant Store", "dropdown_name": "Cached", "merchant_id": "", "marketplace_id": ""},
        ],
        state,
    )

    assert fast_path_items == []
    assert [item.store_name for item in ui_items] == ["Ready Store", "Cached Merchant Store"]
    assert state.fast_path_eligible_at_start == 0
    assert state.ui_routed_at_start == 2


def test_route_store_work_items_recovers_cached_merchant_id_from_dropdown_alias():
    state = ScraperState()
    state.cache_template_available_at_start = True
    state.cache.merchant_id_cache["Jarrow Morrisons"] = "MID-JARROW"

    fast_path_items, ui_items = scraper.route_store_work_items(
        [
            {"store_name": "Jarrow", "dropdown_name": "Jarrow", "merchant_id": "", "marketplace_id": ""},
        ],
        state,
    )

    assert [item.store_name for item in fast_path_items] == ["Jarrow"]
    assert [item.merchant_id for item in fast_path_items] == ["MID-JARROW"]
    assert ui_items == []
    assert state.fast_path_eligible_at_start == 1
    assert state.ui_routed_at_start == 0


def test_should_bypass_auto_concurrency_for_all_fast_path_warm_cache_run():
    state = ScraperState()
    state.fast_path_eligible_at_start = 85
    state.ui_routed_at_start = 0

    assert scraper.should_bypass_auto_concurrency(state) is True


def test_should_not_bypass_auto_concurrency_when_ui_work_exists():
    state = ScraperState()
    state.fast_path_eligible_at_start = 60
    state.ui_routed_at_start = 5

    assert scraper.should_bypass_auto_concurrency(state) is False


def test_should_refresh_live_dropdown_when_manual_override_is_requested():
    state = ScraperState()
    state.previous_live_dropdown_store_names = ["Belle Vale"]
    state.cache.last_updated_at = london_datetime(2026, 4, 1, 9, 0)

    should_refresh, reason, required = scraper.should_refresh_live_dropdown(
        state,
        now=london_datetime(2026, 4, 2, 9, 0),
        force_refresh=True,
    )

    assert (should_refresh, reason, required) == (True, "manual_override", True)


def test_should_skip_live_dropdown_when_cached_snapshot_is_fresh():
    state = ScraperState()
    state.previous_live_dropdown_store_names = ["Belle Vale"]
    state.cache.last_updated_at = london_datetime(2026, 4, 1, 9, 0)

    should_refresh, reason, required = scraper.should_refresh_live_dropdown(
        state,
        now=london_datetime(2026, 4, 2, 9, 0),
        force_refresh=False,
    )

    assert (should_refresh, reason, required) == (False, "cached_snapshot_fresh", False)


def test_should_refresh_live_dropdown_when_cached_snapshot_is_week_old():
    state = ScraperState()
    state.previous_live_dropdown_store_names = ["Belle Vale"]
    now = london_datetime(2026, 4, 2, 9, 0)
    state.cache.last_updated_at = now - timedelta(days=7)

    should_refresh, reason, required = scraper.should_refresh_live_dropdown(
        state,
        now=now,
        force_refresh=False,
    )

    assert (should_refresh, reason, required) == (True, "weekly_refresh_due", False)


def test_load_cached_dropdown_stores_uses_cached_snapshot_for_queue_filtering():
    state = ScraperState()
    state.previous_live_dropdown_store_names = ["Belle Vale", "Oxford"]

    filtered = scraper.load_cached_dropdown_stores(
        [
            {"store_name": "Belle Vale Morrisons", "dropdown_name": "Belle Vale", "merchant_id": "", "marketplace_id": ""},
            {"store_name": "Carterton Morrisons", "dropdown_name": "Carterton", "merchant_id": "", "marketplace_id": ""},
            {"store_name": "Morrisons Welling", "dropdown_name": "Welling", "merchant_id": "", "marketplace_id": ""},
        ],
        state,
        "cached_snapshot_fresh",
    )

    assert [store["store_name"] for store in filtered] == [
        "Belle Vale Morrisons",
        "Carterton Morrisons",
    ]
    assert state.live_dropdown_refresh_mode == "cached"
    assert state.live_dropdown_refresh_reason == "cached_snapshot_fresh"
    assert state.live_dropdown_discovery_attempt == "cached-snapshot"
    assert state.live_dropdown_store_count == 2


def test_build_fast_path_target_url_prefers_summation_metrics_template():
    state = ScraperState()
    fixed_dt = london_datetime(2026, 4, 7, 11, 0)

    target_url = metrics_service._build_fast_path_target_url(
        "https://example.com/api/metrics?merchantIds%5B%5D={merchant_id}&endRange%5Bhour%5D=9",
        "MID123",
        state.settings,
        current_dt=fixed_dt,
    )

    assert "api/summationMetrics" in target_url
    assert "MID123" in target_url
    assert "startRange%5Bmonth%5D=3" in target_url
    assert "startRange%5Bday%5D=6" in target_url
    assert "endRange%5Bmonth%5D=3" in target_url
    assert "endRange%5Bday%5D=7" in target_url
    assert "endRange%5Bhour%5D=11" in target_url


def test_build_fast_path_target_url_respects_explicit_window():
    state = ScraperState()
    start_dt = london_datetime(2026, 4, 1, 6, 0)
    end_dt = london_datetime(2026, 4, 7, 12, 0)

    target_url = metrics_service._build_fast_path_target_url(
        "https://example.com/api/summationMetrics?merchantIds%5B%5D={merchant_id}",
        "MID123",
        state.settings,
        start_dt=start_dt,
        end_dt=end_dt,
    )

    assert "api/summationMetrics" in target_url
    assert "MID123" in target_url
    assert "startRange%5Byear%5D=2026" in target_url
    assert "startRange%5Bmonth%5D=3" in target_url
    assert "startRange%5Bday%5D=1" in target_url
    assert "startRange%5Bhour%5D=6" in target_url
    assert "endRange%5Bmonth%5D=3" in target_url
    assert "endRange%5Bday%5D=7" in target_url
    assert "endRange%5Bhour%5D=12" in target_url


def test_metrics_response_matcher_rejects_wrong_merchant_id():
    class Response:
        status = 200
        url = "https://example.com/api/metrics?merchantIds%5B%5D=OTHER"

    assert metrics_service._is_metrics_response_for_merchant(Response(), "MID123") is False
    assert metrics_service._is_metrics_response_for_merchant(Response(), "") is True


@pytest.mark.asyncio
async def test_fetch_metrics_fast_path_retries_transient_503_then_succeeds(monkeypatch):
    sleep_calls: list[float] = []

    async def no_sleep(seconds):
        sleep_calls.append(seconds)
        return None

    monkeypatch.setattr(metrics_service.asyncio, "sleep", no_sleep)

    request_client = FakeRequestClient([503, 200])
    state = ScraperState()

    payload = await metrics_service.fetch_metrics_fast_path(
        request_client,
        "https://example.com/metrics?merchantIds%5B%5D=MID123",
        "Belle Vale Morrisons",
        state,
        45_000,
    )

    assert payload["metrics"]["OrdersShopped_V2"] == 8
    assert sleep_calls == [pytest.approx(1.5, abs=0.01)]


@pytest.mark.asyncio
async def test_fetch_metrics_fast_path_scales_warmup_to_half_the_worker_pool(monkeypatch):
    sleep_calls: list[float] = []

    async def no_sleep(seconds):
        sleep_calls.append(seconds)
        return None

    monkeypatch.setattr(metrics_service.asyncio, "sleep", no_sleep)

    request_client = FakeRequestClient([200])
    state = ScraperState()
    state.browser_worker_pool_size = 12
    state.fast_path_started_count = 5

    await metrics_service.fetch_metrics_fast_path(
        request_client,
        "https://example.com/metrics?merchantIds%5B%5D=MID123",
        "Belle Vale Morrisons",
        state,
        45_000,
    )

    assert sleep_calls == [pytest.approx(0.75, abs=0.01)]


@pytest.mark.asyncio
async def test_fetch_metrics_fast_path_skips_warmup_after_scaled_window(monkeypatch):
    sleep_calls: list[float] = []

    async def no_sleep(seconds):
        sleep_calls.append(seconds)
        return None

    monkeypatch.setattr(metrics_service.asyncio, "sleep", no_sleep)

    request_client = FakeRequestClient([200])
    state = ScraperState()
    state.browser_worker_pool_size = 12
    state.fast_path_started_count = 6

    await metrics_service.fetch_metrics_fast_path(
        request_client,
        "https://example.com/metrics?merchantIds%5B%5D=MID123",
        "Belle Vale Morrisons",
        state,
        45_000,
    )

    assert sleep_calls == []


@pytest.mark.asyncio
async def test_fetch_metrics_fast_path_honors_shared_backpressure_before_request(monkeypatch):
    sleep_calls: list[float] = []

    async def no_sleep(seconds):
        sleep_calls.append(seconds)
        return None

    monkeypatch.setattr(metrics_service.asyncio, "sleep", no_sleep)

    request_client = FakeRequestClient([200])
    state = ScraperState()
    state.fast_path_backoff_until = asyncio.get_running_loop().time() + 2.0

    await metrics_service.fetch_metrics_fast_path(
        request_client,
        "https://example.com/metrics?merchantIds%5B%5D=MID123",
        "Belle Vale Morrisons",
        state,
        45_000,
    )

    assert sleep_calls == [pytest.approx(2.0, abs=0.01)]


@pytest.mark.asyncio
async def test_process_ui_store_collects_metrics_and_updates_cache(monkeypatch):
    monkeypatch.setattr(metrics_service, "select_store_from_dropdown", lambda *args, **kwargs: asyncio.sleep(0))
    monkeypatch.setattr(metrics_service, "expect", lambda _locator: FakeExpectation())

    page = FakePage()
    page.context = FakeContext(page, request_client=FakeRequestClient([200]))

    state = ScraperState()
    submission_queue = asyncio.Queue()

    await metrics_service.process_ui_store(
        page,
        WorkItem(
            store_name="Belle Vale Morrisons",
            dropdown_name="Belle Vale",
            merchant_id="",
            marketplace_id="",
        ),
        submission_queue,
        state,
    )

    queued = await submission_queue.get()

    assert queued["store"] == "Belle Vale Morrisons"
    assert queued["orders"] == "8"
    assert state.run_failures == []
    assert (
        state.cache.api_url_template
        == "https://example.com/api/summationMetrics?merchantIds%5B%5D={merchant_id}&endRange%5Bhour%5D=9"
    )
    assert state.cache.merchant_id_cache["Belle Vale Morrisons"] == "DISCOVERED"


@pytest.mark.asyncio
async def test_fast_path_worker_completes_without_creating_page():
    state = ScraperState()
    state.cache.api_url_template = "https://example.com/summationMetrics?merchantIds%5B%5D={merchant_id}"
    state.concurrency_limit = 1

    fast_path_queue = asyncio.Queue()
    ui_queue = asyncio.Queue()
    submission_queue = asyncio.Queue()
    await fast_path_queue.put(
        WorkItem(
            store_name="Belle Vale Morrisons",
            dropdown_name="Belle Vale",
            merchant_id="MID123",
            marketplace_id="",
        )
    )

    browser = FakeBrowser(page=None, request_client=FakeRequestClient([200, 200]))

    await scraper.fast_path_worker_task(
        worker_id=1,
        browser=browser,
        storage_template={"cookies": [{"name": "session"}]},
        fast_path_queue=fast_path_queue,
        ui_queue=ui_queue,
        submission_queue=submission_queue,
        state=state,
    )

    queued = await submission_queue.get()

    assert queued["store"] == "Belle Vale Morrisons"
    assert browser.created_contexts[0].new_page_calls == 0
    assert ui_queue.empty()


@pytest.mark.asyncio
async def test_fast_path_worker_requeues_failed_store_once_for_ui():
    state = ScraperState()
    state.cache.api_url_template = "https://example.com/summationMetrics?merchantIds%5B%5D={merchant_id}"
    state.concurrency_limit = 1

    fast_path_queue = asyncio.Queue()
    ui_queue = asyncio.Queue()
    submission_queue = asyncio.Queue()
    await fast_path_queue.put(
        WorkItem(
            store_name="Belle Vale Morrisons",
            dropdown_name="Belle Vale",
            merchant_id="MID123",
            marketplace_id="",
        )
    )

    browser = FakeBrowser(page=None, request_client=FakeRequestClient([500]))

    await scraper.fast_path_worker_task(
        worker_id=1,
        browser=browser,
        storage_template={"cookies": [{"name": "session"}]},
        fast_path_queue=fast_path_queue,
        ui_queue=ui_queue,
        submission_queue=submission_queue,
        state=state,
    )

    requeued = await ui_queue.get()

    assert requeued.store_name == "Belle Vale Morrisons"
    assert requeued.force_ui is True
    assert state.requeued_from_fast_path == 1
    assert submission_queue.empty()
    assert any(event["category"] == "api_fast_path" for event in state.failure_events)


@pytest.mark.asyncio
async def test_ui_worker_continues_after_store_error_and_tolerates_cleanup_failures(monkeypatch):
    processed: list[str] = []

    async def fake_process_ui_store(_page, work_item, _submission_queue, _state):
        if work_item.store_name == "First Store":
            raise RuntimeError("boom")
        processed.append(work_item.store_name)

    monkeypatch.setattr(scraper, "process_ui_store", fake_process_ui_store)

    state = ScraperState()
    state.concurrency_limit = 1

    ui_queue = asyncio.Queue()
    await ui_queue.put(
        WorkItem(
            store_name="First Store",
            dropdown_name="First Store",
            merchant_id="",
            marketplace_id="",
        )
    )
    await ui_queue.put(
        WorkItem(
            store_name="Second Store",
            dropdown_name="Second Store",
            merchant_id="",
            marketplace_id="",
        )
    )

    page = FakePage(fail_on_close=True)
    browser = FakeBrowser(page=page, fail_on_context_close=True)
    submission_queue = asyncio.Queue()
    fast_path_done = asyncio.Event()
    fast_path_done.set()

    await scraper.ui_worker_task(
        worker_id=1,
        browser=browser,
        storage_template={"cookies": [{"name": "session"}]},
        ui_queue=ui_queue,
        submission_queue=submission_queue,
        fast_path_done=fast_path_done,
        state=state,
    )

    assert processed == ["Second Store"]
    assert "First Store (Worker Exception)" in state.run_failures
    assert any(event["category"] == "cleanup" for event in state.failure_events)
