# -*- coding: utf-8 -*-
"""兼容入口：旧单文件脚本已迁移到 legacy/，当前推荐使用 main.py。"""

import runpy

from legacy.gpt_automation import *  # noqa: F401,F403


def _warn_deprecated() -> None:
    import warnings

    warnings.warn(
        "`gpt_automation.py` 已迁移到 `legacy/`，请优先使用 `python main.py`。",
        DeprecationWarning,
        stacklevel=2,
    )


def main() -> None:
    _warn_deprecated()
    runpy.run_module("legacy.gpt_automation", run_name="__main__")


if __name__ == "__main__":
    main()
