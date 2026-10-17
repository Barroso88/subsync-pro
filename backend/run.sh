#!/bin/sh
exec uvicorn app.main:app_factory --factory --host 0.0.0.0 --port "${PORT:-8484}"
