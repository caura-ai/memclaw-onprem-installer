"""Tests for the `support bundle` subcommand and its redaction logic."""

from __future__ import annotations

import hashlib
import json
import sys
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cauractl.cli import cli  # noqa: E402
from cauractl.support import (  # noqa: E402
    _cap_size,
    _redact,
    build_bundle,
    scan_for_leaks,
)


# ── redactor unit tests ────────────────────────────────────────────────────


def test_redact_strips_json_password_field():
    blob = b'{"email":"a@b","password":"hunter2","other":"keep"}'
    out = _redact(blob).decode()
    assert "hunter2" not in out
    assert '"password":"***"' in out
    assert '"other":"keep"' in out


def test_redact_strips_all_sensitive_json_fields():
    # Keep the list in sync with _JSON_FIELD_REDACT in support.py
    fields = [
        "password",
        "api_key",
        "admin_key",
        "jwt_secret",
        "jwt",
        "secret",
        "token",
        "authorization",
        "cookie",
        "settings_encryption_key",
        "openai_api_key",
        "anthropic_api_key",
        "gemini_api_key",
        "postgres_password",
        "github_client_secret",
        "email_api_key",
        "smtp_password",
        "license_key",
        "phone_home_url",
    ]
    for field in fields:
        blob = ('{"' + field + '":"sensitive-value-xyz"}').encode()
        out = _redact(blob).decode()
        assert "sensitive-value-xyz" not in out, f"field {field!r} not redacted"


def test_redact_strips_env_lines():
    blob = (
        b"FOO=keep\n"
        b"JWT_SECRET=super-secret-value-1234\n"
        b"POSTGRES_PASSWORD=pg-pw\n"
        b"OPENAI_API_KEY=sk-something\n"
        b"HOST=ok\n"
    )
    out = _redact(blob).decode()
    assert "super-secret-value-1234" not in out
    assert "pg-pw" not in out
    assert "sk-something" not in out
    assert "FOO=keep" in out
    assert "HOST=ok" in out
    assert "JWT_SECRET=***" in out


def test_redact_strips_bearer_tokens_in_logs():
    blob = (
        b'some free text "Authorization: Bearer eyJhbGc.payload.sig" fallthrough\n'
        b"stack trace: headers={'Authorization': 'Bearer abc123.def456.ghi789'}\n"
    )
    out = _redact(blob).decode()
    assert "eyJhbGc" not in out
    assert "abc123.def456.ghi789" not in out
    assert "***" in out


def test_redact_strips_openai_style_tokens_outside_json():
    blob = b"context: key=sk-proj-abcdefghijklmnopqrstuvwxyz1234567890 end"
    out = _redact(blob).decode()
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890" not in out
    assert "***" in out


def test_redact_strips_admin_api_keys():
    blob = b"provisioned key mc_admin_abcdef1234567890 for tenant"
    out = _redact(blob).decode()
    assert "mc_admin_abcdef1234567890" not in out


def test_redact_handles_invalid_utf8():
    # A log with a stray byte sequence that isn't UTF-8 must not raise.
    blob = b'\x80\x81password="super" rest'
    _redact(blob)  # no exception


def test_cap_size_small_file_unchanged():
    data = b"a" * 100
    assert _cap_size(data, "x.log") == data


def test_cap_size_large_file_truncated_with_marker():
    # Write 120 MiB of 'x', cap limit is 50 MiB → expect head+tail+marker.
    data = b"x" * (120 * 1024 * 1024)
    out = _cap_size(data, "huge.log")
    assert len(out) < len(data)
    assert b"truncated" in out
    # First + last bytes preserved
    assert out.startswith(b"x")
    assert out.endswith(b"x")


# ── end-to-end bundle build ────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "opt-caura"
    (home / "logs" / "platform-admin-api").mkdir(parents=True)
    (home / "logs" / "core-api").mkdir(parents=True)

    (home / "logs" / "platform-admin-api" / "platform-admin-api.log").write_text(
        '{"event":"login","email":"a@b","password":"leak","org":"acme"}\n'
        '{"event":"other","keep":"me"}\n'
    )
    (home / "logs" / "core-api" / "core-api.log").write_text(
        '{"event":"embed","api_key":"sk-dont-leak","tokens":42}\n'
    )
    (home / ".env").write_text(
        "JWT_SECRET=aaaaaaaaaaaaaaaaaaaaaaaa\n"
        "POSTGRES_PASSWORD=bbbbbbbb\n"
        "EMBEDDING_PROVIDER=local\n"
    )
    (home / "install.state.json").write_text(
        json.dumps({"version": "1.2.3", "installed_at": "2026-04-20T00:00:00Z"})
    )
    return home


def test_build_bundle_writes_redacted_tarball(fake_home: Path, tmp_path: Path):
    out_dir = tmp_path / "bundles"
    bundle = build_bundle(
        fake_home,
        out_dir,
        include_logs=True,
        include_compose_state=False,  # no docker in test env
    )
    assert bundle.exists()
    assert bundle.name.startswith("memclaw-support-")
    assert bundle.suffix == ".gz"

    with tarfile.open(bundle, "r:gz") as tar:
        names = tar.getnames()
        assert any(n.endswith("platform-admin-api.log") for n in names)
        assert any(n.endswith("core-api.log") for n in names)
        assert "manifest.json" in names
        assert "config/env.redacted" in names

        # Confirm redaction happened in-tarball
        admin_log = _extract(tar, "logs/platform-admin-api/platform-admin-api.log")
        assert b"leak" not in admin_log
        assert b'"password":"***"' in admin_log

        core_log = _extract(tar, "logs/core-api/core-api.log")
        assert b"sk-dont-leak" not in core_log

        env = _extract(tar, "config/env.redacted")
        assert b"aaaaaaaa" not in env
        assert b"JWT_SECRET=***" in env
        assert b"EMBEDDING_PROVIDER=local" in env

        manifest = json.loads(_extract(tar, "manifest.json"))
        assert manifest["schema_version"] == 1
        assert manifest["deployment"]["version"] == "1.2.3"
        assert manifest["byte_total"] > 0
        assert manifest["file_total"] >= 2


def test_build_bundle_no_logs_skips_log_entries(fake_home: Path, tmp_path: Path):
    bundle = build_bundle(
        fake_home,
        tmp_path / "bundles",
        include_logs=False,
        include_compose_state=False,
    )
    with tarfile.open(bundle, "r:gz") as tar:
        names = tar.getnames()
        assert not any(n.startswith("logs/") for n in names)
        assert "manifest.json" in names


def test_build_bundle_refuses_home_as_out_dir(fake_home: Path):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["support", "bundle", "--home", str(fake_home), "--out-dir", str(fake_home)],
    )
    assert result.exit_code != 0
    assert "cannot be MEMCLAW_HOME" in result.output


def test_build_bundle_missing_home(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "support",
            "bundle",
            "--home",
            str(tmp_path / "nope"),
            "--out-dir",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code != 0
    assert "MEMCLAW_HOME not found" in result.output


def test_support_bundle_cli_happy_path(fake_home: Path, tmp_path: Path):
    out_dir = tmp_path / "out"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "support",
            "bundle",
            "--home",
            str(fake_home),
            "--out-dir",
            str(out_dir),
            "--no-compose",
            "--notes",
            "triaging recall 503",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Bundle ready" in result.output
    # Check manifest picks up notes
    bundle = next(out_dir.glob("*.tar.gz"))
    with tarfile.open(bundle, "r:gz") as tar:
        manifest = json.loads(_extract(tar, "manifest.json"))
    assert manifest["notes"] == "triaging recall 503"


def test_cli_help_lists_support():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "support" in result.output


# ── review subcommand ─────────────────────────────────────────────────────


def test_review_passes_on_clean_bundle(fake_home: Path, tmp_path: Path):
    bundle = build_bundle(
        fake_home,
        tmp_path / "out",
        include_logs=True,
        include_compose_state=False,
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["support", "review", str(bundle)])
    assert result.exit_code == 0, result.output
    assert "Leak-scan: clean" in result.output
    assert "manifest.json" in result.output.replace("\u2026", "")


def test_review_fails_when_bundle_leaks_secret(tmp_path: Path):
    # Hand-craft a bundle with a leaked key to prove the scanner catches
    # shapes the redactor missed. The key is high-entropy + has an
    # openai-style prefix so `openai_key` hits.
    bundle = tmp_path / "leaky.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        data = b"oops this one got through: sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAA rest\n"
        info = tarfile.TarInfo(name="logs/bad/leaky.log")
        info.size = len(data)
        tar.addfile(info, fileobj=_bytesio(data))
        manifest = json.dumps(
            {
                "schema_version": 1,
                "collected_at": "2026-04-20T00:00:00Z",
                "deployment": {},
                "file_total": 1,
                "byte_total": len(data),
                "sha256_of_redacted_payload": "x",
                "notes": "",
            }
        ).encode()
        mi = tarfile.TarInfo(name="manifest.json")
        mi.size = len(manifest)
        tar.addfile(mi, fileobj=_bytesio(manifest))

    hits = scan_for_leaks(bundle)
    assert any(h[1] == "openai_key" for h in hits), hits

    runner = CliRunner()
    result = runner.invoke(cli, ["support", "review", str(bundle)])
    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output


def test_review_extracts_when_requested(fake_home: Path, tmp_path: Path):
    bundle = build_bundle(
        fake_home,
        tmp_path / "out",
        include_logs=True,
        include_compose_state=False,
    )
    extract_to = tmp_path / "extracted"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["support", "review", str(bundle), "--extract-to", str(extract_to)],
    )
    assert result.exit_code == 0, result.output
    assert (extract_to / "manifest.json").is_file()
    assert (extract_to / "logs").is_dir()


def test_review_rejects_non_bundle(tmp_path: Path):
    path = tmp_path / "fake.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        data = b"not a bundle"
        info = tarfile.TarInfo(name="random.txt")
        info.size = len(data)
        tar.addfile(info, fileobj=_bytesio(data))
    runner = CliRunner()
    result = runner.invoke(cli, ["support", "review", str(path)])
    assert result.exit_code != 0
    assert "manifest.json" in result.output


def test_scan_for_leaks_flags_jwt_shape():
    tmp = Path("/tmp") / "cauractl-leak-test.tar.gz"
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            bad = b"token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhYmMifQ.sig123abcdef\n"
            info = tarfile.TarInfo(name="x.log")
            info.size = len(bad)
            tar.addfile(info, fileobj=_bytesio(bad))
        hits = scan_for_leaks(tmp)
        assert any(h[1] == "jwt_token" for h in hits)
    finally:
        tmp.unlink(missing_ok=True)


# ── helpers ────────────────────────────────────────────────────────────────


def _extract(tar: tarfile.TarFile, name: str) -> bytes:
    member = tar.getmember(name)
    fh = tar.extractfile(member)
    assert fh is not None, f"member {name} not extractable"
    return fh.read()


def _bytesio(data: bytes):
    import io

    return io.BytesIO(data)


# ── upload subcommand ─────────────────────────────────────────────────────


def _fake_license_file(home: Path, license_id: str, issued_at_iso: str) -> None:
    """Write a syntactically-valid (but unsigned) JWT file for tests."""
    import base64

    home_dir = home / "license"
    home_dir.mkdir(parents=True, exist_ok=True)
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"license_id": license_id, "issued_at": issued_at_iso}).encode()
    ).rstrip(b"=")
    signature = b"sig"
    (home_dir / "license.key").write_bytes(header + b"." + payload + b"." + signature)


def test_upload_signs_and_posts(fake_home: Path, tmp_path: Path, monkeypatch):
    import hmac as _hmac

    _fake_license_file(fake_home, "lic-abc", "2026-04-20T00:00:00+00:00")

    bundle = build_bundle(
        fake_home,
        tmp_path / "out",
        include_logs=True,
        include_compose_state=False,
    )

    captured = {}

    class _Resp:
        status_code = 201

        def json(self):
            return {"bundle_id": "b-1", "sha256": "x" * 64}

        @property
        def text(self):
            return "{}"

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, files=None, data=None, headers=None, **kwargs):
            captured["url"] = url
            captured["license_id"] = data["license_id"]
            captured["manifest"] = data["manifest"]
            captured["bundle_bytes"] = files["bundle"][1]
            captured["signature"] = headers["X-License-Signature"]
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "support",
            "upload",
            str(bundle),
            "--home",
            str(fake_home),
            "--endpoint",
            "https://support.caura.ai/api/onprem/support",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Uploaded" in result.output
    assert captured["license_id"] == "lic-abc"

    # Re-derive the signature server-side to prove determinism.
    key = hashlib.sha256(b"lic-abc:2026-04-20T00:00:00+00:00").digest()
    signed_body = (
        b"lic-abc."
        + captured["manifest"].encode()
        + b"."
        + hashlib.sha256(captured["bundle_bytes"]).hexdigest().encode()
    )
    expected = _hmac.new(key, signed_body, hashlib.sha256).hexdigest()
    assert captured["signature"] == expected


def test_upload_refuses_when_leak_scan_fails(tmp_path: Path, monkeypatch):
    # Bundle with a seeded leak shape
    bundle = tmp_path / "leaky.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        bad = b"sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAA extra"
        info = tarfile.TarInfo(name="logs/x.log")
        info.size = len(bad)
        tar.addfile(info, fileobj=_bytesio(bad))
        manifest = json.dumps({"schema_version": 1, "deployment": {}}).encode()
        mi = tarfile.TarInfo(name="manifest.json")
        mi.size = len(manifest)
        tar.addfile(mi, fileobj=_bytesio(manifest))

    home = tmp_path / "home"
    _fake_license_file(home, "lic-abc", "2026-04-20T00:00:00+00:00")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "support",
            "upload",
            str(bundle),
            "--home",
            str(home),
            "--endpoint",
            "https://x/api/onprem/support",
        ],
    )
    assert result.exit_code != 0
    assert "Leak-scan flagged" in result.output


def test_upload_requires_license_file(tmp_path: Path):
    # Home without license/license.key should fail
    home = tmp_path / "home-no-lic"
    home.mkdir()
    bundle = tmp_path / "empty.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        manifest = json.dumps({"schema_version": 1}).encode()
        mi = tarfile.TarInfo(name="manifest.json")
        mi.size = len(manifest)
        tar.addfile(mi, fileobj=_bytesio(manifest))

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "support",
            "upload",
            str(bundle),
            "--home",
            str(home),
            "--skip-leak-scan",
        ],
    )
    assert result.exit_code != 0
    assert "license file not found" in result.output


# ── the CLI's own version, across the distribution rename ──────────────────


def test_self_version_prefers_the_new_distribution(monkeypatch):
    """``cauractl`` first, so a reinstalled host reports its real version.

    THE ONE OF THESE THREE THAT DISCRIMINATES. Both distributions resolve, and
    only a lookup that asks for the new name first returns the new version; the
    two below pass on the pre-rename single-name lookup as well and are guards
    against a future rewrite rather than evidence the pair is read.
    """
    import cauractl.support as support

    def fake_version(dist):
        return {"cauractl": "9.9.9", "caura-memclawctl": "0.0.1"}[dist]  # legacy-name-ok: the previous distribution name, still installed until a host reinstalls

    monkeypatch.setattr("importlib.metadata.version", fake_version)
    assert support._self_version() == "9.9.9"


def test_self_version_falls_back_to_the_old_distribution(monkeypatch):
    """The case the fallback exists for: a host that has not reinstalled.

    The rename does not reach an already-installed CLI, and this value goes
    into the support bundle manifest beside ``collector``. Reading only the new
    name would report "unknown" to the support backend for every such host --
    a regression in a field someone reads, caused by a rename that was
    otherwise invisible to them.
    """
    from importlib.metadata import PackageNotFoundError

    import cauractl.support as support

    def fake_version(dist):
        if dist == "cauractl":
            raise PackageNotFoundError(dist)
        return "0.0.1"

    monkeypatch.setattr("importlib.metadata.version", fake_version)
    assert support._self_version() == "0.0.1"


def test_self_version_is_unknown_when_neither_is_installed(monkeypatch):
    """Running from a source checkout. Must not raise into bundle collection."""
    from importlib.metadata import PackageNotFoundError

    import cauractl.support as support

    def fake_version(dist):
        raise PackageNotFoundError(dist)

    monkeypatch.setattr("importlib.metadata.version", fake_version)
    assert support._self_version() == "unknown"
