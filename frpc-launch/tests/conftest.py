import sys
import os
import subprocess
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))


@pytest.fixture(scope="session")
def native_fake(tmp_path_factory):
    if os.name != "nt":
        return None
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        pytest.skip("Windows fixture requires the installed .NET Framework C# compiler")
    binary = tmp_path_factory.mktemp("synthetic-client") / "fake frpc.exe"
    source = Path(__file__).parent / "fixtures/fake_frpc.cs"
    subprocess.run([str(compiler), "/nologo", "/out:" + str(binary), str(source)],
                   check=True, capture_output=True, timeout=30)
    return binary
