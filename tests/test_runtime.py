import asyncio

import pytest

import scraper
from core.state import ScraperState
from services import metrics_service


class FakeRequestResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload or {}

    async def json(self):
        return self._payload


class FakeRequestClient:
    async def get(self, _url, timeout):
        assert timeout == 45_000
        return FakeRequestResponse(500)


class FakeContext:
    def __init__(self, page, fail_on_close=False):
        self.request = FakeRequestClient()
        self._page = page
        self.fail_on_close = fail_on_close

    def set_default_navigation_timeout(self, _timeout):
        pass

    def set_default_timeout(self, _timeout):
        pass

    async def new_page(self):
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
    def __init__(self, page, fail_on_context_close=False):
        self._page = page
        self.fail_on_context_close = fail_on_context_close

    async def new_context(self, storage_state):
        assert storage_state == {"cookies": [{"name": "session"}]}
        context = FakeContext(self._page, fail_on_close=self.fail_on_context_close)
        self._page.context = context
        return context


@pytest.mark.asyncio
async def test_process_single_store_falls_back_to_ui_after_fast_path_failure(monkeypatch):
    monkeypatch.setattr(metrics_service, "select_store_from_dropdown", lambda *args, **kwargs: asyncio.sleep(0))
    monkeypatch.setattr(metrics_service, "expect", lambda _locator: FakeExpectation())

    page = FakePage()
    page.context = FakeContext(page)

    state = ScraperState()
    state.cache.api_url_template = "https://example.com/summationMetrics?merchantIds%5B%5D={merchant_id}"
    submission_queue = asyncio.Queue()

    await metrics_service.process_single_store(
        page,
        {"store_name": "Belle Vale Morrisons", "merchant_id": "MID123", "marketplace_id": ""},
        submission_queue,
        state,
    )

    queued = await submission_queue.get()

    assert queued["store"] == "Belle Vale Morrisons"
    assert queued["orders"] == "8"
    assert state.run_failures == []
    assert any(event["category"] == "api_fast_path" for event in state.failure_events)


@pytest.mark.asyncio
async def test_worker_task_continues_after_store_error_and_tolerates_cleanup_failures(monkeypatch):
    processed: list[str] = []

    async def fake_process_single_store(_page, store_item, _submission_queue, _state):
        if store_item["store_name"] == "First Store":
            raise RuntimeError("boom")
        processed.append(store_item["store_name"])

    monkeypatch.setattr(scraper, "process_single_store", fake_process_single_store)

    state = ScraperState()
    state.concurrency_limit = 1

    job_queue = asyncio.Queue()
    await job_queue.put({"store_name": "First Store"})
    await job_queue.put({"store_name": "Second Store"})

    page = FakePage(fail_on_close=True)
    browser = FakeBrowser(page, fail_on_context_close=True)
    submission_queue = asyncio.Queue()

    await scraper.worker_task(
        worker_id=1,
        browser=browser,
        storage_template={"cookies": [{"name": "session"}]},
        job_queue=job_queue,
        submission_queue=submission_queue,
        state=state,
    )

    assert processed == ["Second Store"]
    assert "First Store (Worker Exception)" in state.run_failures
    assert any(event["category"] == "cleanup" for event in state.failure_events)
