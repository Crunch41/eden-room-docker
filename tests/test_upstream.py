#!/usr/bin/env python3
"""Tests for scripts/upstream.py (no network)."""

import datetime as dt
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("upstream", Path(__file__).parent.parent / "scripts" / "upstream.py")
upstream = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upstream)

NOW = dt.datetime(2026, 10, 17, 17, 0, tzinfo=dt.timezone.utc)
CURRENT = {"upstream": "a" * 40, "bases": "b", "source": "c"}
PUBLISHED = {
    "io.crunch41.upstream.commit": "a" * 40,
    "io.crunch41.inputs.bases": "b",
    "io.crunch41.inputs.source": "c",
    "org.opencontainers.image.created": "2026-10-16T17:00:00Z",
}


class Decision(unittest.TestCase):
    def test_unchanged_scheduled_run_skips(self):
        self.assertEqual(upstream.decision(CURRENT, PUBLISHED, "schedule", 0, NOW), [])

    def test_manual_and_push_runs_always_build(self):
        self.assertTrue(upstream.decision(CURRENT, PUBLISHED, "workflow_dispatch", 0, NOW))
        self.assertTrue(upstream.decision(CURRENT, PUBLISHED, "push", 0, NOW))

    def test_nothing_published_builds(self):
        self.assertEqual(upstream.decision(CURRENT, {}, "schedule", 0, NOW), ["nothing published yet"])

    def test_each_changed_input_is_a_reason(self):
        for key in CURRENT:
            changed = dict(CURRENT, **{key: "new"})
            self.assertEqual(upstream.decision(changed, PUBLISHED, "schedule", 0, NOW), [f"{key} changed"])

    def test_refresh_after_the_configured_age(self):
        old = dict(PUBLISHED, **{"org.opencontainers.image.created": "2026-10-10T17:30:00Z"})
        self.assertEqual(upstream.decision(CURRENT, old, "schedule", 7, NOW), ["image older than 7 days"])
        self.assertEqual(upstream.decision(CURRENT, PUBLISHED, "schedule", 7, NOW), [])
        self.assertEqual(upstream.decision(CURRENT, old, "schedule", 0, NOW), [])


class Dockerfiles(unittest.TestCase):
    def test_base_images_skip_stages_and_scratch(self):
        text = "FROM ubuntu:24.04 AS build\nFROM build AS test\nFROM --platform=linux/amd64 alpine:3\nFROM scratch\n"
        self.assertEqual(upstream.base_images(text), ["alpine:3", "ubuntu:24.04"])

    def test_build_arguments_in_from_are_refused(self):
        with self.assertRaises(SystemExit):
            upstream.base_images("FROM node:${NODE}\n")


class Resolve(unittest.TestCase):
    def test_a_commit_ref_needs_no_network(self):
        cfg = {"repository": "o/r", "url": "https://example.invalid/o/r.git", "track": "release"}
        result = upstream.resolve(cfg, "f" * 40)
        self.assertEqual((result["commit"], result["version"]), ("f" * 40, "f" * 10))


if __name__ == "__main__":
    unittest.main()
