"""
shared_utils.py
================
Common config, theme, LM Studio/backend helpers shared by all 4 apps.
Import this in each app:
    from shared_utils import *
"""

import tkinter as tk
from tkinter import scrolledtext
import threading
import requests
import os

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
PORTFOLIO_FILE   = "portfolio.xlsx"
MAX_TOKENS       = 1024
TEMPERATURE      = 0.3

LM_STUDIO_URL    = "http://localhost:1234/v1/chat/completions"
LM_STUDIO_MODELS = "http://localhost:1234/v1/models"

GROQ_URL    = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS = {
    "Llama 3.3 70B (Fast)":   "llama-3.3-70b-versatile",
    "Llama 3.1 8B (Fastest)": "llama-3.1-8b-instant",
    "Gemma 3 27B":            "gemma2-9b-it",
    "Mixtral 8x7B":           "mixtral-8x7b-32768",
}

TOGETHER_URL    = "https://api.together.xyz/v1/chat/completions"
TOGETHER_MODELS = {
    "Llama 3.1 405B": "meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo",
    "Llama 3.3 70B":  "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "Deepseek V3":    "deepseek-ai/DeepSeek-V3",
}

GROQ_API_KEY     = os.environ.get("GROQ_API_KEY", "")
TOGETHER_API_KEY = os.environ.get("TOGETHER_API_KEY", "")

_ACTIVE_BACKEND      = "local"
_ACTIVE_ONLINE_MODEL = list(GROQ_MODELS.values())[0]

# ─────────────────────────────────────────────
# THEME
# ─────────────────────────────────────────────
BG     = "#0A0E14"
BG2    = "#12171F"
BG3    = "#1A2030"
FG     = "#CDD6F4"
FG_DIM = "#6C7086"
ACCENT = "#F4C430"
GREEN  = "#A6E3A1"
RED    = "#F38BA8"
YELLOW = "#F9E2AF"
BLUE   = "#89B4FA"
TEAL   = "#94E2D5"
BORDER = "#313244"

FONT    = ("Consolas", 10)
FONT_SM = ("Consolas", 9)
FONT_LG = ("Consolas", 12, "bold")
FONT_HD = ("Consolas", 14, "bold")

CLIENT_CONTEXT = (
    "Client: Johnathan Rush, age 30, 90% disabled veteran, Port Orange FL. "
    "VA disability = permanent income floor → HIGH risk tolerance, aggressive accumulation. "
    "Roth 401k: O, MSFT, VICI. Taxable: PFE, GIS, UPS + cash to deploy. "
    "Goal: maximize total real return over 30yr horizon."
)

# ─────────────────────────────────────────────
# BACKEND HELPERS
# ─────────────────────────────────────────────

def get_active_model() -> str:
    if _ACTIVE_BACKEND == "local":
        try:
            resp = requests.get(LM_STUDIO_MODELS, timeout=5)
            if resp.status_code == 200:
                models = resp.json().get("data", [])
                if models:
                    return models[0].get("id", "local-model")
        except Exception:
            pass
        return "local-model"
    return _ACTIVE_ONLINE_MODEL


def get_backend_url() -> str:
    if _ACTIVE_BACKEND == "groq":    return GROQ_URL
    if _ACTIVE_BACKEND == "together": return TOGETHER_URL
    return LM_STUDIO_URL


def get_backend_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if _ACTIVE_BACKEND == "groq" and GROQ_API_KEY:
        headers["Authorization"] = f"Bearer {GROQ_API_KEY}"
    elif _ACTIVE_BACKEND == "together" and TOGETHER_API_KEY:
        headers["Authorization"] = f"Bearer {TOGETHER_API_KEY}"
    return headers


def check_backend_status() -> tuple:
    if _ACTIVE_BACKEND == "local":
        try:
            resp = requests.get(LM_STUDIO_MODELS, timeout=5)
            if resp.status_code == 200:
                models = resp.json().get("data", [])
                name = models[0].get("id", "none") if models else "none loaded"
                return True, f"LM Studio ✓  {name}"
            return False, "LM Studio not responding"
        except Exception:
            return False, "LM Studio offline"
    elif _ACTIVE_BACKEND == "groq":
        if not GROQ_API_KEY:
            return False, "Set GROQ_API_KEY env var"
        try:
            resp = requests.get("https://api.groq.com/openai/v1/models",
                                headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=8)
            return (resp.status_code == 200,
                    f"Groq ✓  {_ACTIVE_ONLINE_MODEL}" if resp.status_code == 200 else "Groq auth failed")
        except Exception as e:
            return False, f"Groq unreachable: {e}"
    elif _ACTIVE_BACKEND == "together":
        if not TOGETHER_API_KEY:
            return False, "Set TOGETHER_API_KEY env var"
        return True, f"Together AI — {_ACTIVE_ONLINE_MODEL}"
    return False, "Unknown backend"


def ask_lm_studio(question: str, composite_context: str, portfolio_context: str) -> str:
    """Send question to active backend. Returns answer string."""
    import re

    system = (
        f"You are a financial analyst reviewing pre-calculated investment scores.\n"
        f"{CLIENT_CONTEXT}\n\n"
        f"Scores:\n{composite_context}\n\n"
        f"Portfolio:\n{portfolio_context}\n\n"
        f"RULES: Plain English only. No markdown. Reference actual numbers. Be thorough but concise."
    )

    payload = {
        "model":    get_active_model(),
        "messages": [
            {"role": "system",  "content": system},
            {"role": "user",    "content": question},
        ],
        "max_tokens":  MAX_TOKENS,
        "temperature": TEMPERATURE,
    }

    try:
        resp = requests.post(get_backend_url(), json=payload,
                             headers=get_backend_headers(), timeout=60)
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            content = re.sub(r'\*+', '', content)
            content = re.sub(r'#{1,6}\s', '', content)
            return content.strip()
        return f"Backend error {resp.status_code}: {resp.text[:200]}"
    except requests.exceptions.ConnectionError:
        return "Cannot connect to LM Studio. Start it on port 1234, or switch to Groq/Together."
    except Exception as e:
        return f"Error: {e}"


def load_portfolio_context(filepath: str) -> str:
    """Load portfolio Excel for LM Studio context."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(filepath, data_only=True)
        ws = wb.active
        rows = []
        for row in ws.iter_rows(values_only=True):
            if any(c is not None for c in row):
                rows.append("  " + " | ".join(str(c) if c is not None else "" for c in row))
        return "\n".join(rows[:40])
    except Exception:
        return "Portfolio file not loaded."


# ─────────────────────────────────────────────
# ASSET CLASS ROUTING  (used by stock analysis)
# ─────────────────────────────────────────────
ASSET_ANALYZERS = {
    "ETF": {
        "run":  ["bogle", "druckenmiller"],
        "skip": ["buffett", "weiss_quality", "dalio"],
        "note": "ETF: Bogle + Druckenmiller. Buffett/Weiss not applicable.",
    },
    "REIT": {
        "run":  ["buffett", "weiss", "bogle", "dalio", "druckenmiller"],
        "skip": [],
        "note": "REIT: Full analysis. Buffett DCF unreliable — use FFO.",
    },
    "STOCK": {
        "run":  ["buffett", "weiss", "bogle", "dalio", "druckenmiller"],
        "skip": [],
        "note": "",
    },
    "MUTUAL_FUND": {
        "run":  ["bogle", "druckenmiller"],
        "skip": ["buffett", "weiss", "dalio"],
        "note": "Mutual Fund: Bogle primary.",
    },
    "UNKNOWN": {
        "run":  ["buffett", "weiss", "bogle", "dalio", "druckenmiller"],
        "skip": [],
        "note": "Unknown asset — running all analyzers.",
    },
}


def detect_asset_class(info: dict) -> str:
    qt = (info.get("quoteType") or "").upper()
    sector = (info.get("sector") or "").lower()
    name   = (info.get("longName") or "").lower()
    if qt == "ETF":
        return "ETF"
    if qt == "MUTUALFUND":
        return "MUTUAL_FUND"
    if qt == "EQUITY":
        if "real estate" in sector or "reit" in name:
            return "REIT"
        return "STOCK"
    return "UNKNOWN"
