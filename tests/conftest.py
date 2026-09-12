"""测试公共设施。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

# §12.3 实测供应商样例（7 行；密钥为伪造值，仅测结构）
SAMPLE_PROVIDERS = [
    ("claude-official", "claude", "Claude Official", "official", 1, "{}", "{}"),
    ("claude-desktop-official", "claude-desktop", "Claude Desktop Official", "official", 1, "{}", "{}"),
    (
        "kimi-id", "codex", "Kimi For Coding", "cn_official", 0,
        json.dumps({"auth": {"OPENAI_API_KEY": "sk-fake-kimi"}}),
        json.dumps({"usage_script": {
            "enabled": True, "language": "javascript", "code": None,
            "timeout": 10, "templateType": "token_plan",
            "autoQueryInterval": 5, "codingPlanProvider": "kimi",
        }}),
    ),
    (
        "opencode-id", "codex", "OpenCode Go", "third_party", 0,
        "{}",
        json.dumps({"usage_script": {
            "enabled": True, "language": "javascript",
            "code": "({request:{url:'http://127.0.0.1:18443/usage'},extractor:r=>r})",
            "timeout": 10, "templateType": "custom", "autoQueryInterval": 5,
        }}),
    ),
    ("codex-official", "codex", "OpenAI Official", "official", 1, "{}",
     json.dumps({"usage_script": {
         "enabled": True, "templateType": "official_subscription",
         "autoQueryInterval": 5,
     }})),
    (
        "bailian-id", "gemini", "阿里云百炼（个人）", "aggregator", 0,
        json.dumps({"env": {
            "GOOGLE_GEMINI_BASE_URL": "https://dashscope.example/compatible-mode/v1",
            "GEMINI_API_KEY": "sk-fake-bailian",
        }}),
        "{}",
    ),
    ("gemini-official", "gemini", "Google Official", "official", 0, "{}", "{}"),
]

PROVIDERS_DDL = """
CREATE TABLE providers (
    id TEXT PRIMARY KEY,
    app_type TEXT NOT NULL,
    name TEXT NOT NULL,
    settings_config TEXT,
    website_url TEXT,
    category TEXT,
    created_at INTEGER,
    sort_index INTEGER DEFAULT 0,
    notes TEXT,
    icon TEXT,
    icon_color TEXT,
    meta TEXT,
    is_current INTEGER DEFAULT 0,
    in_failover_queue INTEGER DEFAULT 0,
    cost_multiplier REAL DEFAULT 1.0,
    limit_daily_usd REAL,
    limit_monthly_usd REAL,
    provider_type TEXT
);
"""

ROLLUPS_DDL = """
CREATE TABLE usage_daily_rollups (
    date TEXT NOT NULL,
    app_type TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    model TEXT,
    request_model TEXT,
    pricing_model TEXT,
    request_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    total_cost_usd REAL DEFAULT 0,
    avg_latency_ms REAL,
    input_token_semantics TEXT
);
"""


@pytest.fixture
def sample_db(tmp_path) -> Path:
    """§10 Store 层测试库：§3 表结构 + 7 行 providers 样例。"""
    db_path = tmp_path / "cc-switch.db"
    con = sqlite3.connect(db_path)
    con.execute(PROVIDERS_DDL)
    con.execute(ROLLUPS_DDL)
    con.executemany(
        "INSERT INTO providers (id, app_type, name, category, is_current,"
        " settings_config, meta, sort_index)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [row[:5] + (row[5], row[6], idx) for idx, row in enumerate(SAMPLE_PROVIDERS)],
    )
    con.commit()
    con.close()
    return db_path
