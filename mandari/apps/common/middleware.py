"""
Common middleware for Mandari.

Includes error handling for database connection issues.
"""

import logging

from django.db import OperationalError
from django.shortcuts import render

logger = logging.getLogger(__name__)


class DatabaseErrorMiddleware:
    """
    Middleware that catches database connection errors and shows
    a user-friendly maintenance page instead of a 500 error.

    This is useful during:
    - Database maintenance/restarts
    - Deployments
    - Database failover scenarios
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            response = self.get_response(request)
            return response
        except OperationalError as e:
            error_message = str(e).lower()

            # Check if this is a connection error (not a query error)
            connection_errors = [
                "connection refused",
                "could not connect",
                "connection failed",
                "server closed the connection",
                "connection timed out",
                "no connection to the server",
                "terminating connection",
                "connection reset",
            ]

            is_connection_error = any(err in error_message for err in connection_errors)

            if is_connection_error:
                logger.error(f"Database connection error: {e}")
                return self._maintenance_response(request)

            # Re-raise if it's a different kind of OperationalError
            raise

    def _maintenance_response(self, request):
        """Render the maintenance page with 503 status."""
        return render(
            request,
            "errors/maintenance.html",
            status=503,
            headers={
                "Retry-After": "300",  # Suggest retry after 5 minutes
            },
        )
