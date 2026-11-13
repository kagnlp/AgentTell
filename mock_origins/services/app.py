"""Mock services origin (services.test), serving the services plant pages."""
from __future__ import annotations

from fastapi import FastAPI

from mock_origins import plant_pages

app = FastAPI(title="mock-services")
plant_pages.register(app, kind="services")
