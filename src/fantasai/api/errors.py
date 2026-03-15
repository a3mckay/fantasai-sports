"""Centralized error handling for the FastAPI application."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base application error with HTTP status code."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class NotFoundError(AppError):
    """Resource not found."""

    def __init__(self, message: str = "Resource not found") -> None:
        super().__init__(message, status_code=404)


class ValidationError(AppError):
    """Invalid input data."""

    def __init__(self, message: str = "Invalid input") -> None:
        super().__init__(message, status_code=422)


def register_error_handlers(app: FastAPI) -> None:
    """Register exception handlers on the FastAPI app."""

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "AppError: %s (status=%d, path=%s)",
            exc.message,
            exc.status_code,
            request.url.path,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.message},
        )

    @app.exception_handler(NotImplementedError)
    async def not_implemented_handler(
        request: Request, exc: NotImplementedError
    ) -> JSONResponse:
        logger.info("NotImplementedError at %s", request.url.path)
        return JSONResponse(
            status_code=501,
            content={"error": "This endpoint is not yet implemented"},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.error(
            "Unhandled exception at %s: %s",
            request.url.path,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error"},
        )
