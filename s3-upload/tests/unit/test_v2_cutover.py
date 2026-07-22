from pathlib import Path

import config
import upload


def test_v1_invocation_is_rejected_with_secret_free_migration_guidance(
    tmp_path, capsys
):
    calls = []
    rc = upload.main(
        ["--file", "legacy.bin", "--profile", "old"],
        environ={
            "S3_UPLOAD_ACCESS_KEY_ID": "LEGACYKEY1234",
            "S3_UPLOAD_SECRET_ACCESS_KEY": "legacy-secret-value",
            "S3_UPLOAD_BUCKET": "legacy-bucket",
        },
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: calls.append(args),
    )

    output = capsys.readouterr()
    assert rc == 2 and output.out == "" and calls == []
    assert "v1 flat configuration is no longer accepted" in output.err
    assert "references/configuration.md" in output.err
    assert "LEGACYKEY1234" not in output.err
    assert "legacy-secret-value" not in output.err
    assert "legacy-bucket" not in output.err


def test_public_runtime_contains_no_v1_resolver_or_profile_writer():
    skill_root = Path(upload.__file__).resolve().parent.parent

    assert not hasattr(config, "resolve_connection")
    assert not (skill_root / "scripts" / "set_profile.sh").exists()
    help_text = upload.v2_parser().format_help()
    assert all(
        name in help_text
        for name in ("upload", "url", "delete", "resume", "reconcile", "abort")
    )
