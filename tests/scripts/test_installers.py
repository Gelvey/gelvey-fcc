import subprocess
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _script_text(name: str) -> str:
    return (_repo_root() / "scripts" / name).read_text(encoding="utf-8")


def _braced_body(text: str, declaration: str) -> str:
    start = text.index(declaration)
    brace_start = text.index("{", start)
    depth = 0

    for index, char in enumerate(text[brace_start:], start=brace_start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start + 1 : index]

    raise AssertionError(f"Unclosed function body for {declaration}")


def test_install_sh_installs_claude_only_when_missing() -> None:
    text = _script_text("install.sh")
    body = _braced_body(text, "install_claude_if_missing()")
    main = text[text.index('parse_args "$@"') :]

    assert "Installs Claude Code and Codex if missing" in text
    assert "if command -v claude >/dev/null 2>&1; then" in body
    assert "Claude Code already found on PATH; skipping install." in body
    assert "require_command npm" in body
    assert "run npm install -g @anthropic-ai/claude-code" in body
    assert body.index("command -v claude") < body.index("run npm install")
    assert body.index("return 0") < body.index("run npm install")
    assert 'step "Installing Claude Code if missing"\ninstall_claude_if_missing' in main
    assert "npm install -g @anthropic-ai/claude-code" not in main


def test_install_sh_installs_codex_only_when_missing() -> None:
    text = _script_text("install.sh")
    body = _braced_body(text, "install_codex_if_missing()")
    main = text[text.index('parse_args "$@"') :]

    assert "if command -v codex >/dev/null 2>&1; then" in body
    assert "Codex already found on PATH; skipping install." in body
    assert "require_command npm" in body
    assert "run npm install -g @openai/codex" in body
    assert body.index("command -v codex") < body.index("run npm install")
    assert body.index("return 0") < body.index("run npm install")
    assert 'step "Installing Codex if missing"\ninstall_codex_if_missing' in main
    assert "npm install -g @openai/codex" not in main
    assert "fcc-claude" in text
    assert "fcc-codex" in text


def test_install_sh_installs_missing_uv_without_self_update() -> None:
    text = _script_text("install.sh")
    body = _braced_body(text, "install_or_update_uv()")

    assert "if command -v uv >/dev/null 2>&1; then" in body
    assert "update_existing_uv" in body
    assert "run uv self update" not in body

    update_index = body.index("update_existing_uv")
    validate_existing_index = body.index("validate_uv_version", update_index)
    installer_index = body.index("run_uv_installer")
    validate_installed_index = body.index("validate_uv_version", installer_index)
    verification_index = body.index('if [ "$dry_run" -eq 0 ] && ! command -v uv')

    assert update_index < validate_existing_index < installer_index
    assert installer_index < verification_index < validate_installed_index


def test_install_sh_updates_uv_with_detected_source() -> None:
    text = _script_text("install.sh")
    update_body = _braced_body(text, "update_existing_uv()")

    assert "uv self update --dry-run" in text
    assert update_body.count("run uv self update") == 1
    assert update_body.index("uv_self_update_supported") < update_body.index(
        "run uv self update"
    )

    assert "brew list --versions uv" in text
    assert "run brew upgrade uv" in update_body
    assert "pipx list" in text
    assert "run pipx upgrade uv" in update_body
    assert "VIRTUAL_ENV" in text
    assert "run python -m pip install --upgrade uv" in update_body
    assert "uv_version_satisfies_minimum" in update_body
    assert "install source was not detected" in update_body


def test_install_sh_validates_minimum_uv_version() -> None:
    text = _script_text("install.sh")
    validate_body = _braced_body(text, "validate_uv_version()")

    assert 'MIN_UV_VERSION="0.11.0"' in text
    assert "uv self version --short" in text
    assert "version_ge" in validate_body
    assert "uv $MIN_UV_VERSION or newer is required" in validate_body


def test_install_ps1_installs_claude_only_when_missing() -> None:
    text = _script_text("install.ps1")
    body = _braced_body(text, "function Install-ClaudeIfMissing")

    assert "Installs Claude Code and Codex if missing" in text
    assert "if (Get-Command claude -ErrorAction SilentlyContinue)" in body
    assert "Claude Code already found on PATH; skipping install." in body
    assert 'Assert-CommandAvailable "npm"' in body
    assert (
        'Invoke-InstallCommand -FilePath "npm" '
        '-Arguments @("install", "-g", "@anthropic-ai/claude-code")'
    ) in body
    assert body.index("Get-Command claude") < body.index("Invoke-InstallCommand")
    assert body.index("return") < body.index("Invoke-InstallCommand")
    assert (
        'Write-Step "Installing Claude Code if missing"\nInstall-ClaudeIfMissing'
        in text
    )


def test_install_ps1_installs_codex_only_when_missing() -> None:
    text = _script_text("install.ps1")
    body = _braced_body(text, "function Install-CodexIfMissing")

    assert "if (Get-Command codex -ErrorAction SilentlyContinue)" in body
    assert "Codex already found on PATH; skipping install." in body
    assert 'Assert-CommandAvailable "npm"' in body
    assert (
        'Invoke-InstallCommand -FilePath "npm" '
        '-Arguments @("install", "-g", "@openai/codex")'
    ) in body
    assert body.index("Get-Command codex") < body.index("Invoke-InstallCommand")
    assert body.index("return") < body.index("Invoke-InstallCommand")
    assert 'Write-Step "Installing Codex if missing"\nInstall-CodexIfMissing' in text
    assert "fcc-claude" in text
    assert "fcc-codex" in text


def test_install_ps1_installs_missing_uv_without_self_update() -> None:
    text = _script_text("install.ps1")
    body = _braced_body(text, "function Install-OrUpdateUv")
    self_update = 'Invoke-InstallCommand -FilePath "uv" -Arguments @("self", "update")'

    assert "if (Get-Command uv -ErrorAction SilentlyContinue)" in body
    assert "Update-ExistingUv" in body
    assert self_update not in body

    update_index = body.index("Update-ExistingUv")
    validate_existing_index = body.index("Assert-MinUvVersion", update_index)
    installer_index = body.index("Invoke-UvInstaller")
    verification_index = body.index("if ((-not $DryRun)")
    validate_installed_index = body.index("Assert-MinUvVersion", installer_index)

    assert update_index < validate_existing_index < installer_index
    assert installer_index < verification_index < validate_installed_index


def test_install_ps1_updates_uv_with_detected_source() -> None:
    text = _script_text("install.ps1")
    update_body = _braced_body(text, "function Update-ExistingUv")
    self_update = 'Invoke-InstallCommand -FilePath "uv" -Arguments @("self", "update")'

    assert '"self", "update", "--dry-run"' in text
    assert update_body.count(self_update) == 1
    assert update_body.index("Test-UvSelfUpdateSupported") < update_body.index(
        self_update
    )

    assert (
        'Invoke-InstallCommand -FilePath "scoop" -Arguments @("update", "uv")'
        in update_body
    )
    assert '"winget"' in update_body
    assert '"astral-sh.uv"' in update_body
    assert '"--accept-package-agreements"' in update_body
    assert (
        'Invoke-InstallCommand -FilePath "pipx" -Arguments @("upgrade", "uv")'
        in update_body
    )
    assert (
        'Invoke-InstallCommand -FilePath "python" -Arguments @("-m", "pip", "install", "--upgrade", "uv")'
        in update_body
    )
    assert "Test-UvVersionSatisfiesMinimum" in update_body
    assert "install source was not detected" in update_body


def test_install_ps1_validates_minimum_uv_version() -> None:
    text = _script_text("install.ps1")
    validate_body = _braced_body(text, "function Assert-MinUvVersion")

    assert '$MinUvVersion = "0.11.0"' in text
    assert '"self", "version", "--short"' in text
    assert "[version]" in text
    assert "uv $MinUvVersion or newer is required" in validate_body


# ── install.sh execution tests (--dry-run) ────────────────────────────────


def _run_install_sh(*args: str) -> subprocess.CompletedProcess[str]:
    """Run install.sh with given arguments and return the result."""
    sh = _repo_root() / "scripts" / "install.sh"
    return subprocess.run(
        ["sh", str(sh), *args],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        check=False,
    )


def test_install_sh_dry_run_produces_expected_steps() -> None:
    """install.sh --dry-run prints the expected installation steps."""
    result = _run_install_sh("--dry-run")
    assert result.returncode == 0, result.stderr

    stdout = result.stdout
    # Step headers (printed by step() function)
    assert "Installing Claude Code if missing" in stdout
    assert "Installing Codex if missing" in stdout
    assert "Installing uv if missing, updating if present" in stdout
    assert "Installing Python" in stdout
    assert "Installing or updating Free Claude Code" in stdout

    # Post-install instructions
    assert "Free Claude Code is installed" in stdout
    assert "fcc-server" in stdout
    assert "fcc-claude" in stdout
    assert "fcc-codex" in stdout

    # Commands should be printed with '+' prefix
    assert "+ " in stdout


def test_install_sh_help_shows_usage() -> None:
    """install.sh --help prints the usage text."""
    result = _run_install_sh("--help")
    assert result.returncode == 0, result.stderr
    assert "Usage: install.sh" in result.stdout
    assert "--voice-nim" in result.stdout
    assert "--voice-local" in result.stdout
    assert "--dry-run" in result.stdout


def test_install_sh_help_short_flag() -> None:
    """install.sh -h also prints the usage text."""
    result = _run_install_sh("-h")
    assert result.returncode == 0, result.stderr
    assert "Usage: install.sh" in result.stdout


def test_install_sh_rejects_unknown_option() -> None:
    """install.sh fails on unknown options."""
    result = _run_install_sh("--nonexistent")
    assert result.returncode != 0
    assert "unknown option" in result.stderr


def test_install_sh_torch_backend_requires_voice_local() -> None:
    """--torch-backend without --voice-local or --voice-all fails."""
    result = _run_install_sh("--dry-run", "--torch-backend", "cu130")
    assert result.returncode != 0
    assert "requires --voice-local or --voice-all" in result.stderr


def test_install_sh_dry_run_prints_no_real_commands() -> None:
    """--dry-run mode must not invoke real install commands."""
    result = _run_install_sh("--dry-run")
    assert result.returncode == 0, result.stderr
    stdout = result.stdout
    # The script should only print commands (prefixed with '+'), not execute them.
    # If uv is already installed, the script prints '+ uv self update' or
    # '+ brew upgrade uv' etc. If uv is missing, it prints '+ curl ...'.
    # Either way, we should see printed commands prefixed with '+'.
    assert "+ " in stdout, (
        f"Expected printed commands (prefixed with '+') in dry-run output, "
        f"but got:\n{stdout}"
    )


def test_install_sh_darwin_homebrew_code_path_static() -> None:
    """install.sh contains the macOS Homebrew npm --prefix fallback."""
    text = _script_text("install.sh")
    # macOS detection guard
    assert "uname -s" in text
    assert '"Darwin"' in text
    # Homebrew prefix for npm
    assert "brew --prefix" in text
    assert "--prefix" in text
    # Both Claude and Codex functions should have the macOS path
    claude_body = _braced_body(text, "install_claude_if_missing()")
    codex_body = _braced_body(text, "install_codex_if_missing()")
    assert "uname -s" in claude_body
    assert "uname -s" in codex_body
    assert "brew --prefix" in claude_body
    assert "brew --prefix" in codex_body


def test_install_sh_defaults_dry_run_zero() -> None:
    """dry_run defaults to 0 (not dry-run mode)."""
    text = _script_text("install.sh")
    assert "dry_run=0" in text
