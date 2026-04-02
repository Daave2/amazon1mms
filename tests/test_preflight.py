import csv

from core.preflight import run_preflight


def test_preflight_reports_missing_required_env_var(tmp_path, monkeypatch):
    _set_valid_runtime_env(monkeypatch)
    monkeypatch.delenv("LOGIN_PASSWORD", raising=False)
    _write_valid_store_file(tmp_path / "urls.csv")

    result = run_preflight(
        env_file=str(tmp_path / ".env"),
        csv_path=str(tmp_path / "urls.csv"),
        output_dir=str(tmp_path / "output"),
    )

    assert result["status"] == "error"
    assert "Missing required environment variables: LOGIN_PASSWORD" in result["errors"]


def test_preflight_reports_invalid_concurrency_combination(tmp_path, monkeypatch):
    _set_valid_runtime_env(monkeypatch)
    monkeypatch.setenv("INITIAL_CONCURRENCY", "50")
    monkeypatch.setenv("AUTO_MIN_CONCURRENCY", "1")
    monkeypatch.setenv("AUTO_MAX_CONCURRENCY", "10")
    _write_valid_store_file(tmp_path / "urls.csv")

    result = run_preflight(
        env_file=str(tmp_path / ".env"),
        csv_path=str(tmp_path / "urls.csv"),
        output_dir=str(tmp_path / "output"),
    )

    assert result["status"] == "error"
    assert "INITIAL_CONCURRENCY must fall within the AUTO_MIN_CONCURRENCY/AUTO_MAX_CONCURRENCY range" in result["errors"]


def test_preflight_reports_missing_urls_csv(tmp_path, monkeypatch):
    _set_valid_runtime_env(monkeypatch)

    result = run_preflight(
        env_file=str(tmp_path / ".env"),
        csv_path=str(tmp_path / "missing.csv"),
        output_dir=str(tmp_path / "output"),
    )

    assert result["status"] == "error"
    assert f"Store file not found: {tmp_path / 'missing.csv'}" in result["errors"]


def test_preflight_reports_no_usable_store_rows(tmp_path, monkeypatch):
    _set_valid_runtime_env(monkeypatch)
    csv_path = tmp_path / "urls.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as file_handle:
        writer = csv.writer(file_handle)
        writer.writerow(["merchant_id", "new_id", "store_name", "marketplace_id"])
        writer.writerow(["", "", "", ""])

    result = run_preflight(
        env_file=str(tmp_path / ".env"),
        csv_path=str(csv_path),
        output_dir=str(tmp_path / "output"),
    )

    assert result["status"] == "error"
    assert f"No usable store rows found in {csv_path}" in result["errors"]


def test_preflight_accepts_missing_optional_webhook(tmp_path, monkeypatch):
    _set_valid_runtime_env(monkeypatch)
    monkeypatch.delenv("CHAT_WEBHOOK_URL", raising=False)
    _write_valid_store_file(tmp_path / "urls.csv")

    result = run_preflight(
        env_file=str(tmp_path / ".env"),
        csv_path=str(tmp_path / "urls.csv"),
        output_dir=str(tmp_path / "output"),
    )

    assert result["status"] == "ok"
    assert result["details"]["urls"]["chat_webhook_url"] == ""
    assert result["errors"] == []


def _set_valid_runtime_env(monkeypatch):
    monkeypatch.setenv("LOGIN_URL", "https://example.com/signin")
    monkeypatch.setenv("LOGIN_EMAIL", "tester@example.com")
    monkeypatch.setenv("LOGIN_PASSWORD", "password")
    monkeypatch.setenv("OTP_SECRET_KEY", "JBSWY3DPEHPK3PXP")
    monkeypatch.setenv("TARGET_URL", "https://example.com/dashboard")
    monkeypatch.setenv("FORM_POST_URL", "https://example.com/form")
    monkeypatch.setenv("AUTO_ENABLED", "true")
    monkeypatch.setenv("INITIAL_CONCURRENCY", "5")
    monkeypatch.setenv("AUTO_MIN_CONCURRENCY", "1")
    monkeypatch.setenv("AUTO_MAX_CONCURRENCY", "10")
    monkeypatch.setenv("NUM_FORM_SUBMITTERS", "2")
    monkeypatch.setenv("FAST_PATH_MAX_CONCURRENCY", "4")


def _write_valid_store_file(csv_path):
    with csv_path.open("w", newline="", encoding="utf-8") as file_handle:
        writer = csv.writer(file_handle)
        writer.writerow(["merchant_id", "new_id", "store_name", "marketplace_id"])
        writer.writerow(["A1", "", "Belle Vale Morrisons", "UK"])
