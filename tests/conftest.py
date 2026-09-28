"""Test fixtures and mock helpers for google_calendar_push."""

import asyncio
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

class MockStore:
    """In-memory mock for Home Assistant Store helper."""
    def __init__(self, hass, version, key):
        self.hass = hass
        self.version = version
        self.key = key
        self.data = None

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data


class MockBatchHttpRequest:
    """Mock Google API batch HTTP request."""
    def __init__(self, execute_callback: Optional[Callable] = None):
        self._requests = []
        self._execute_callback = execute_callback

    def add(self, request, request_id=None, callback=None):
        self._requests.append((request, request_id, callback))

    def execute(self):
        for req, req_id, cb in self._requests:
            if self._execute_callback:
                resp, exc = self._execute_callback(req, req_id)
                if cb:
                    cb(req_id, resp, exc)
            else:
                if cb:
                    cb(req_id, {"id": f"g_{req_id}", "status": "confirmed"}, None)


class MockGoogleService:
    """Mock Google Calendar API service."""
    def __init__(self, batch_behavior: Optional[Callable] = None):
        self.batch_behavior = batch_behavior
        self._events_mock = MagicMock()
        self._events_mock.list.return_value = MagicMock(name="list_req")
        self._events_mock.insert.return_value = MagicMock(name="insert_req")
        self._events_mock.update.return_value = MagicMock(name="update_req")
        self._events_mock.delete.return_value = MagicMock(name="delete_req")

    def events(self):
        return self._events_mock

    def new_batch_http_request(self):
        return MockBatchHttpRequest(self.batch_behavior)


import inspect

class MockHass:
    """Mock HomeAssistant instance."""
    def __init__(self):
        self.data = {}
        self.tasks = []

    def verify_event_loop_thread(self, *args, **kwargs):
        pass

    async def async_add_executor_job(self, target, *args, **kwargs):
        if inspect.iscoroutinefunction(target):
            return await target(*args, **kwargs)
        return target(*args, **kwargs)

    def async_create_task(self, target):
        task = asyncio.create_task(target)
        self.tasks.append(task)
        return task



class MockOAuthSession:
    """Mock OAuth2Session."""
    def __init__(self):
        self.valid_token = True
        self.token = {
            "access_token": "mock_access_token",
            "refresh_token": "mock_refresh_token",
            "client_id": "mock_client_id",
            "client_secret": "mock_client_secret",
            "token_uri": "https://oauth2.googleapis.com/token",
        }

    async def async_ensure_token_valid(self):
        self.valid_token = True


class MockRequest:
    """Mock aiohttp web.Request."""
    def __init__(self, json_data: Any, headers: Optional[Dict[str, str]] = None):
        self._json_data = json_data
        self.headers = headers or {}

    async def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data
