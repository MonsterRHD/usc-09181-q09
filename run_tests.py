#!/usr/bin/env python3
"""Tiny zero-dependency test runner: discovers test_* modules in tests/
and runs every callable named test_*."""
import importlib.util
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(HERE, "tests")

failures = []
passed = 0
for fname in sorted(os.listdir(TESTS)):
    if not fname.startswith("test_") or not fname.endswith(".py"):
        continue
    mod_name = fname[:-3]
    spec = importlib.util.spec_from_file_location(mod_name, os.path.join(TESTS, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in sorted(dir(mod)):
        if not name.startswith("test_"):
            continue
        fn = getattr(mod, name)
        if not callable(fn):
            continue
        try:
            fn()
            passed += 1
            print(f"PASS {mod_name}.{name}")
        except Exception:
            failures.append((f"{mod_name}.{name}", traceback.format_exc()))
            print(f"FAIL {mod_name}.{name}")

print(f"\n{passed} passed, {len(failures)} failed")
for name, tb in failures:
    print("=" * 70)
    print("FAIL", name)
    print(tb)
sys.exit(1 if failures else 0)
