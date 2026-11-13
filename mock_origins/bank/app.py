"""Mock bank origin (bank.test), serving the bank plant pages."""
from __future__ import annotations

from fastapi import FastAPI

from mock_origins import plant_pages

app = FastAPI(title="mock-bank")
plant_pages.register(app, kind="bank")
