"""Запуск всех тестов без pytest (embedded-питоном portable):

  D:\\ComfyUI_windows_portable\\python_embeded\\python.exe tests\\run_all.py
"""

import os
import sys
import traceback

_TESTS = os.path.dirname(os.path.abspath(__file__))
# embedded-питон портабла (python313._pth) не добавляет каталог скрипта сам
sys.path.insert(0, _TESTS)
sys.path.insert(0, os.path.dirname(_TESTS))

import test_graph_rewrite  # noqa: E402
import test_parity  # noqa: E402

MODULES = [test_graph_rewrite, test_parity]


def main():
    passed, failed = 0, 0
    for mod in MODULES:
        for name in sorted(dir(mod)):
            if not name.startswith("test_"):
                continue
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            try:
                fn()
                passed += 1
                print(f"  ok  {mod.__name__}.{name}")
            except Exception:
                failed += 1
                print(f"FAIL  {mod.__name__}.{name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
