"""Materialize a portable local result after the complete review loop."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from urllib.parse import unquote, urlsplit

from . import assets, fsatomic, imageformat, jcs, viewer


def local_asset(base, root, target):
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or parts.query or parts.fragment or "\\" in target:
        raise assets.AssetError("image must use an unambiguous local relative path")
    decoded = unquote(parts.path)
    path = Path(os.path.abspath(base / decoded))
    if Path(decoded).is_absolute() or not path.is_relative_to(root):
        raise assets.AssetError("image escapes the extracted archive")
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise assets.AssetError("image path contains a symlink")
    fsatomic.check_safe_regular_file(str(path), "image")
    return path


def write_result(pipe, job):
    direct = pipe.state["phases"]["direct"]
    reviewed = pipe.state["phases"]["review"]
    source = pipe.state["phases"]["source"]
    base = Path(direct["primary_path"]).parent
    root = Path(direct["staging"])
    pandoc = pipe.services["pandoc_path"]
    direct_bytes = Path(direct["primary_path"]).read_bytes()
    final_bytes = Path(reviewed["verified_path"]).read_bytes()
    targets = list(dict.fromkeys(assets.discover_image_targets(direct_bytes, pandoc) +
                                assets.discover_image_targets(final_bytes, pandoc)))
    if len(targets) > assets.ASSET_MAX_COUNT:
        raise assets.AssetError("image count exceeds cap")
    dest = Path(pipe.work_dir) / "result"
    if dest.exists():
        # A crash after rename but before the job checkpoint can leave a complete
        # result. Verify its contents before returning it to the user.
        manifest = jcs.loads_strict((dest / "manifest.json").read_bytes())
        expected_input = jcs.sha256_canonical({"source": source["sha256"], "direct": direct["sha256"],
                                              "final": reviewed["verified_sha256"]})
        if manifest.get("input_sha256") != expected_input:
            raise ValueError("result input identity changed")
        for name, digest in manifest["files"].items():
            path = local_asset(dest, dest, name)
            if fsatomic.read_file_sha256(str(path)) != digest:
                raise ValueError("result integrity mismatch")
        return manifest
    with tempfile.TemporaryDirectory(prefix=".result-", dir=pipe.work_dir) as tmp:
        staging = Path(tmp)
        (staging / "images").mkdir(mode=0o700)
        replacements = {}
        total = 0
        for target in targets:
            path = local_asset(base, root, target)
            size = path.stat().st_size
            total += size
            if total > assets.ASSET_MAX_TOTAL_BYTES:
                raise assets.AssetError("image bytes exceed cap")
            data = path.read_bytes()
            extension = imageformat.MEDIA_EXTENSIONS.get(imageformat.sniff_image_media_type(data))
            if extension is None:
                raise assets.AssetError("unsupported image bytes")
            name = "images/%s.%s" % (hashlib.sha256(data).hexdigest(), extension)
            replacements[target] = name
            (staging / name).write_bytes(data)
        direct_output = assets.replace_image_targets(direct_bytes, replacements, pandoc)
        final_output = assets.replace_image_targets(final_bytes, replacements, pandoc)
        for output in (direct_output, final_output):
            if set(assets.discover_image_targets(output, pandoc)) - set(replacements.values()):
                raise assets.AssetError("image reference could not be rewritten safely")
        shutil.copyfile(source["frozen_path"], staging / "source.pdf")
        (staging / "direct.md").write_bytes(direct_output)
        (staging / "final.md").write_bytes(final_output)
        warnings = []
        if reviewed.get("unverified_replacements"):
            warnings.append({"kind": "final_revision_needs_review",
                             "block_ids": reviewed["unverified_replacements"]})
        for key, block_ids in reviewed["flags"].items():
            if block_ids:
                warnings.append({"kind": key, "block_ids": block_ids})
        if direct.get("omitted_images"):
            warnings.append({"kind": "possible_omitted_images", "items": direct["omitted_images"]})
        for unit_id, findings in job.get("page_findings", {}).items():
            if findings:
                warnings.append({"kind": "page_findings", "unit_id": unit_id, "findings": findings})
        report = {"schema_version": "pdf2markdown.review-report/v1", "warnings": warnings,
                  "quality": "needs_attention" if warnings else "reviewed",
                  "page_count": source["page_count"], "review": reviewed["accumulated"],
                  "responses": reviewed.get("responses", []),
                  "rounds_by_page": reviewed["budget"]["rounds_by_page"]}
        (staging / "review.json").write_bytes(jcs.canonicalize(report))
        (staging / "index.html").write_bytes(viewer.render(
            job["source_name"], direct_output, final_output, pandoc, replacements.values()))
        files = {}
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                os.chmod(path, 0o600)
                with path.open("r+b") as fh:
                    os.fsync(fh.fileno())
                files[path.relative_to(staging).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = {"schema_version": "pdf2markdown.result/v1", "quality": report["quality"],
                    "input_sha256": jcs.sha256_canonical({"source": source["sha256"],
                        "direct": direct["sha256"], "final": reviewed["verified_sha256"]}),
                    "files": files, "warnings": warnings}
        (staging / "manifest.json").write_bytes(jcs.canonicalize(manifest))
        os.chmod(staging / "manifest.json", 0o600)
        with (staging / "manifest.json").open("r+b") as fh:
            os.fsync(fh.fileno())
        fsatomic.fsync_dir(str(staging / "images"))
        fsatomic.fsync_dir(str(staging))
        os.rename(staging, dest)
        fsatomic.fsync_dir(pipe.work_dir)
    return manifest
