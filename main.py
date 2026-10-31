import os
import json
import sqlite3
import hashlib
import uuid
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

APP_VERSION = "1.0.0"
# Render free services should use /tmp unless a paid persistent disk is attached.
DB_PATH = os.getenv("DATABASE_PATH", "/tmp/agent.db")
TOKEN = os.getenv("A2A_BEARER_TOKEN", "CHANGE_ME_BEFORE_DEPLOY")
BASE_URL = os.getenv(
    "A2A_BASE_URL",
    "https://invoice-a2a-agent-dewd.onrender.com"
).rstrip("/")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
API_KEY = os.getenv("OPENAI_API_KEY", "")
API_BASE = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LOCK = threading.RLock()

ACTIONS = {
    "settle_invoice",
    "request_approval",
    "hold_invoice",
    "reject_duplicate",
    "open_exception",
}
BATCH_MIME = "application/vnd.ga5.invoice-claim-batch+json"
PROPOSAL_MIME = "application/vnd.ga5.invoice-action-proposals+json"
RESULT_MIME = "application/vnd.ga5.invoice-action-results+json"
RECEIPT_MIME = "application/vnd.ga5.invoice-action-receipts+json"

app = FastAPI(title="Invoice Action Agent", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def db():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks(
            id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            context_id TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            state TEXT NOT NULL,
            task_json TEXT NOT NULL,
            created REAL NOT NULL,
            updated REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages(
            owner TEXT NOT NULL,
            message_id TEXT NOT NULL,
            digest TEXT NOT NULL,
            task_id TEXT NOT NULL,
            PRIMARY KEY(owner, message_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decisions(
            package_hash TEXT PRIMARY KEY,
            proposal_json TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
