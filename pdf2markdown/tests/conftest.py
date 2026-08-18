import sys
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import pytest  # noqa: E402

# PyMuPDF 的 `fitz` 兼容层在**首次** import 时向 stdout 打一行 deprecation
# warning。生产代码在 validate_pdf_identity / page_crop / preflight 里按需 import
# 它，于是「哪个用例先碰到 fitz」决定了那一行落在谁的 capsys 捕获里——而多个用例
# 断言 stdout 恰好只有一行 JSON。这里在收集阶段先 import 一次，把那一行赶到任何
# 捕获之外，使整套测试与只跑其中一个文件的结果一致。
try:  # pragma: no cover - 依赖缺失时由预检用例自己报告
    import fitz  # noqa: F401
except Exception:  # noqa: BLE001
    pass


@pytest.fixture(autouse=True)
def _instant_retry_backoff(monkeypatch):
    """把 ADR 0006 重试的指数退避睡眠在测试里压成 0。

    来源下载、结果 ZIP 下载和任务轮询在瞬时故障时会退避 1s、2s 再重试
    （scripts/pdf_source.py、scripts/result_archive.py、scripts/doc2x.py）。
    走 `workflow.main` 的端到端用例注入的是 transport 而不是 sleep，真睡会让每个
    故障用例多花 3 秒。这里只消除等待，不改变尝试次数与分类逻辑；退避秒数本身由
    直接调用这三个函数、显式传 `sleep=` 的单元用例断言
    （tests/unit/test_network_retry.py）。
    """
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
