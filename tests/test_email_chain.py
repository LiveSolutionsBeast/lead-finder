#!/usr/bin/env python3
"""Unit tests for resolve_and_validate_email chain in lf_email_patterns.py."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

# Ensure imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lf_email_patterns as ep
import lf_db as db


class TestEmailChain(unittest.TestCase):
    def setUp(self):
        # Create an isolated in-memory DB schema for contacts + companies + cache.
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE companies (
                id INTEGER PRIMARY KEY,
                name TEXT,
                website TEXT,
                email_pattern TEXT,
                email_pattern_confidence REAL,
                email_pattern_source TEXT,
                business_type TEXT,
                city TEXT,
                state TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE contacts (
                id INTEGER PRIMARY KEY,
                company_id INTEGER,
                first_name TEXT,
                last_name TEXT,
                email TEXT,
                is_derived_email INTEGER DEFAULT 0,
                is_manually_edited INTEGER DEFAULT 0,
                smtp_validation_status TEXT,
                smtp_validated_at TEXT,
                smtp_validation_code INTEGER,
                email_ready_for_export INTEGER DEFAULT 0,
                email_rejected_reason TEXT,
                validation_confidence REAL DEFAULT 0.0,
                validation_method TEXT,
                validation_checked_at TEXT,
                validation_mx_host TEXT,
                validation_response TEXT,
                validation_latency_ms INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE email_validation_cache (
                email TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                status TEXT NOT NULL,
                analysis TEXT NOT NULL,
                smtp_probed INTEGER DEFAULT 0,
                smtp_code INTEGER,
                mx_host TEXT,
                catch_all INTEGER,
                validated_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

        def _test_get_db():
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn

        # Patch lf_db.get_db to return this DB.
        self._orig_get_db = db.get_db
        db.get_db = _test_get_db
        ep.get_db = db.get_db

    def tearDown(self):
        db.get_db = self._orig_get_db
        ep.get_db = self._orig_get_db
        os.unlink(self.db_path)

    def _seed(self, company: dict, contact: dict):
        conn = db.get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO companies (id, name, website, email_pattern, email_pattern_confidence, email_pattern_source, business_type, city, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                company["id"],
                company.get("name", ""),
                company.get("website", ""),
                company.get("email_pattern", None),
                company.get("email_pattern_confidence", 0.0),
                company.get("email_pattern_source", ""),
                company.get("business_type", ""),
                company.get("city", ""),
                company.get("state", ""),
            ),
        )
        cur.execute(
            "INSERT INTO contacts (id, company_id, first_name, last_name, email, is_manually_edited, smtp_validation_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                contact["id"],
                contact["company_id"],
                contact.get("first_name", ""),
                contact.get("last_name", ""),
                contact.get("email", None),
                contact.get("is_manually_edited", 0),
                contact.get("smtp_validation_status", None),
            ),
        )
        conn.commit()
        conn.close()

    def test_existing_pattern_derive_and_smtp(self):
        """Chain uses existing pattern, derives email, runs SMTP, persists."""
        self._seed(
            {"id": 1, "website": "https://acme.com", "email_pattern": "{first}.{last}@acme.com"},
            {"id": 10, "company_id": 1, "first_name": "John", "last_name": "Smith"},
        )
        mock_result = {
            "email": "john.smith@acme.com",
            "domain": "acme.com",
            "status": "Okay to Send",
            "analysis": "Accepted",
            "smtp_probed": True,
            "smtp_code": 250,
            "mx_host": "mx.acme.com",
            "catch_all": False,
            "validated_at": datetime.now(timezone.utc).isoformat(),
            "validation_confidence": 0.9,
            "validation_method": "smtp_live",
            "validation_checked_at": datetime.now(timezone.utc).isoformat(),
            "validation_mx_host": "mx.acme.com",
            "validation_response": "250 OK",
            "validation_latency_ms": 1234,
        }

        with patch.object(ep, "_smtp_validate", return_value=mock_result):
            res = ep.resolve_and_validate_email(10, source="test")

        self.assertEqual(res.candidate_email, "john.smith@acme.com")
        self.assertEqual(res.smtp_validation_status, "Okay to Send")
        self.assertEqual(res.validation_method, "smtp_2probe")
        self.assertTrue(res.email_ready_for_export)

        # Verify DB write
        conn = db.get_db()
        row = conn.execute("SELECT smtp_validation_status, email_ready_for_export FROM contacts WHERE id=?", (10,)).fetchone()
        conn.close()
        self.assertEqual(row[0], "Okay to Send")
        self.assertEqual(row[1], 1)

    def test_popup_email_used_directly(self):
        """If popup_email is supplied, skip pattern/derive and validate it."""
        self._seed(
            {"id": 1, "website": "https://acme.com"},
            {"id": 10, "company_id": 1, "first_name": "John", "last_name": "Smith"},
        )
        mock_result = {
            "email": "other@acme.com",
            "domain": "acme.com",
            "status": "Do Not Send",
            "analysis": "Rejected",
            "smtp_probed": True,
            "smtp_code": 550,
            "mx_host": "mx.acme.com",
            "catch_all": False,
            "validated_at": datetime.now(timezone.utc).isoformat(),
            "validation_confidence": 0.8,
            "validation_method": "smtp_live",
            "validation_checked_at": datetime.now(timezone.utc).isoformat(),
            "validation_mx_host": "mx.acme.com",
            "validation_response": "550 bad",
            "validation_latency_ms": 500,
        }

        with patch.object(ep, "_smtp_validate", return_value=mock_result):
            res = ep.resolve_and_validate_email(10, source="test", popup_email="other@acme.com")

        self.assertEqual(res.candidate_email, "other@acme.com")
        self.assertEqual(res.validation_method, "smtp_2probe")
        self.assertFalse(res.email_ready_for_export)

    def test_cache_hit_skips_smtp(self):
        """Fresh cache hit re-applies result and skips SMTP."""
        self._seed(
            {"id": 1, "website": "https://acme.com", "email_pattern": "{first}.{last}@acme.com"},
            {"id": 10, "company_id": 1, "first_name": "John", "last_name": "Smith"},
        )
        validated_at = datetime.now(timezone.utc).isoformat()
        conn = ep.get_db()
        conn.execute(
            "INSERT INTO email_validation_cache (email, domain, status, analysis, smtp_probed, smtp_code, mx_host, catch_all, validated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("john.smith@acme.com", "acme.com", "Okay to Send", "Accepted", 1, 250, "mx.acme.com", 0, validated_at),
        )
        conn.commit()
        conn.close()

        with patch.object(ep, "_smtp_validate") as mock_smtp:
            res = ep.resolve_and_validate_email(10, source="test")
            mock_smtp.assert_not_called()

        self.assertEqual(res.validation_method, "cache_hit")
        self.assertEqual(res.smtp_validation_status, "Okay to Send")

    def test_is_manually_edited_skip(self):
        """Contacts with is_manually_edited=1 skip the chain unless forced."""
        self._seed(
            {"id": 1, "website": "https://acme.com", "email_pattern": "{first}.{last}@acme.com"},
            {"id": 10, "company_id": 1, "first_name": "John", "last_name": "Smith", "is_manually_edited": 1},
        )

        res = ep.resolve_and_validate_email(10, source="test")
        self.assertEqual(res.validation_method, "skipped_manual_edit")

        # Forced revalidate should not skip
        with patch.object(ep, "_smtp_validate") as mock_smtp:
            mock_result = {
                "email": "john.smith@acme.com",
                "domain": "acme.com",
                "status": "Maybe",
                "analysis": "Soft Fail",
                "smtp_probed": True,
                "smtp_code": 450,
                "mx_host": "mx.acme.com",
                "catch_all": False,
                "validated_at": datetime.now(timezone.utc).isoformat(),
                "validation_confidence": 0.3,
                "validation_method": "smtp_live",
                "validation_checked_at": datetime.now(timezone.utc).isoformat(),
                "validation_mx_host": "mx.acme.com",
                "validation_response": "450 deferred",
                "validation_latency_ms": 100,
            }
            mock_smtp.return_value = mock_result
            res2 = ep.resolve_and_validate_email(10, source="test", force_revalidate=True)
            self.assertEqual(res2.validation_method, "smtp_2probe")

    def test_existing_validation_status_skip(self):
        """Already-validated contacts skip unless forced."""
        self._seed(
            {"id": 1, "website": "https://acme.com", "email_pattern": "{first}.{last}@acme.com"},
            {"id": 10, "company_id": 1, "first_name": "John", "last_name": "Smith", "email": "john.smith@acme.com", "smtp_validation_status": "Okay to Send"},
        )

        with patch.object(ep, "_smtp_validate") as mock_smtp:
            res = ep.resolve_and_validate_email(10, source="test")
            mock_smtp.assert_not_called()
        self.assertEqual(res.validation_method, "skipped")


if __name__ == "__main__":
    unittest.main()
