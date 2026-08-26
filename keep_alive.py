"""
Render-compatible health server for the Discord bot.

Render Web Services require the application to listen on the
TCP port supplied through the PORT environment variable.
"""

from __future__ import annotations

import os
import logging
from threading import Thread

from flask import Flask, jsonify


logger = logging.getLogger("doraemon-health")

app = Flask(__name__)


@app.get("/")
def home():
    return jsonify(
        {
            "status": "ok",
            "service": "Doraemon Discord Bot",
        }
    ), 200


@app.get("/health")
def health():
    return jsonify(
        {
            "status": "healthy",
        }
    ), 200


def run() -> None:
    """
    Start the HTTP health server.

    Render supplies PORT dynamically. Never hard-code 8080.
    """

    port_raw = os.getenv("PORT", "10000")

    try:
        port = int(port_raw)
    except ValueError:
        logger.warning(
            "Invalid PORT=%r; falling back to 10000.",
            port_raw,
        )
        port = 10000

    port = max(1, min(port, 65535))

    logger.info(
        "Starting health server on 0.0.0.0:%d",
        port,
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True,
        use_reloader=False,
    )


def keep_alive() -> Thread:
    """Start the health server in a daemon thread."""

    thread = Thread(
        target=run,
        name="render-health-server",
        daemon=True,
    )

    thread.start()

    return thread
