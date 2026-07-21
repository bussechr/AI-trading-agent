from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import textwrap
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
FEATURE_TAIL_LIMITS = {"M5": 3, "H4": 3, "D": 3}
RAW_TAIL_LIMITS = {"M5": 10, "H4": 30, "D": 120}
RUNTIME_OPS_FILES = (
    "_env.bat",
    "00_preflight.bat",
    "01_sync_python.bat",
    "02_sync_node.bat",
    "03_postgres_start.bat",
    "04_db_migrate.bat",
    "05_gpu_check.bat",
    "19_start_mt4.ps1",
    "20_start_bridge.bat",
    "21_start_runtime.bat",
    "22_start_dashboard.bat",
    "22_start_dashboard.ps1",
    "23_start_monitor.bat",
    "24_deploy_bridge_ea.bat",
    "24_deploy_bridge_ea.ps1",
    "24_start_feature_push_worker.bat",
    "90_stop_all.bat",
    "README.md",
    "ensure_local_bridge_key.ps1",
    "find_owned_instance_processes.ps1",
    "resolve_stack_endpoints.ps1",
    "stop_owned_stack_processes.ps1",
    "validate_runtime_risk_limits.ps1",
)


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(cmd, cwd=str(cwd or REPO), check=True, env=env)


def windows_to_wsl(path: str) -> Path:
    txt = str(path).strip().replace("\\", "/")
    if len(txt) >= 3 and txt[1:3] == ":/":
        drive = txt[0].lower()
        return Path("/mnt") / drive / txt[3:]
    return Path(txt)


def wsl_to_windows(path: Path) -> str:
    return subprocess.check_output(["wslpath", "-w", str(path)], text=True).strip()


def active_runtime_venv() -> Path:
    stack_root = (REPO / "fx-quant-stack").resolve()
    marker = stack_root / ".venv_win.active"
    if not marker.is_file():
        raise RuntimeError(f"isolated runtime marker is missing: {marker}")
    name = marker.read_text(encoding="utf-8").strip()
    candidate = (stack_root / name).resolve()
    if not name or candidate.parent != stack_root:
        raise RuntimeError(f"invalid isolated runtime marker value: {name!r}")
    if not (candidate / ".fxstack_runtime_isolated").is_file():
        raise RuntimeError(f"active runtime is not isolation-verified: {candidate}")
    site_packages = candidate / "Lib" / "site-packages"
    editable_hooks: list[Path] = list(site_packages.glob("__editable__*.pth"))
    for pth_path in site_packages.glob("*.pth"):
        content = pth_path.read_text(encoding="utf-8", errors="replace").replace("\\", "/").lower()
        if "fx-quant-stack/src" in content:
            editable_hooks.append(pth_path)
    for direct_url_path in site_packages.glob("*.dist-info/direct_url.json"):
        try:
            direct_url = json.loads(direct_url_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"invalid installed-package provenance: {direct_url_path}") from exc
        if bool((direct_url.get("dir_info") or {}).get("editable")):
            editable_hooks.append(direct_url_path)
    if editable_hooks:
        names = ", ".join(sorted({path.name for path in editable_hooks}))
        raise RuntimeError(
            f"active runtime still contains an editable source hook: {candidate} ({names})"
        )
    python_exe = candidate / "Scripts" / "python.exe"
    isolation_probe = subprocess.run(
        [
            str(python_exe),
            "-I",
            "-c",
            "from fxstack.runtime.startup_preflight import "
            "runtime_physical_isolation_errors as check; "
            "errors=check(); print('\\n'.join(errors)); raise SystemExit(2 if errors else 0)",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if isolation_probe.returncode != 0:
        detail = (isolation_probe.stdout or isolation_probe.stderr).strip()
        raise RuntimeError(
            f"active runtime failed the current physical-isolation probe: {detail}"
        )
    return candidate


def read_base_python_home(venv_root: Path) -> Path:
    cfg = venv_root / "pyvenv.cfg"
    for line in cfg.read_text(encoding="utf-8").splitlines():
        if line.lower().startswith("home = "):
            return windows_to_wsl(line.split("=", 1)[1].strip())
    raise RuntimeError(f"unable to resolve base python home from {cfg}")


def safe_rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def copy_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        raise FileNotFoundError(src)
    shutil.copytree(src, dst, dirs_exist_ok=True)


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def write_installed_env(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "@echo off\r\n"
        "set \"FXSTACK_PACKAGE_MODE=1\"\r\n"
        "set \"FXSTACK_ALLOW_SQLITE=1\"\r\n"
        "set \"FXSTACK_DATABASE_URL=sqlite:///data/state/fxstack_runtime.db\"\r\n"
        "set \"FXSTACK_REQUIRE_CUDA=0\"\r\n"
        "set \"TRADER_PYTHON_EXE=%ROOT%\\runtime\\python\\python.exe\"\r\n"
        "set \"NODE_EXE=%ROOT%\\runtime\\node\\node.exe\"\r\n",
        encoding="utf-8",
    )


def write_helper_batch(path: Path, command: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "cd /d \"%~dp0\"\r\n"
        f"{command}\r\n",
        encoding="utf-8",
    )


def build_dashboard(build_root: Path) -> Path:
    print(f"[build] dashboard workspace: {build_root}", flush=True)
    safe_rmtree(build_root)
    build_root.mkdir(parents=True, exist_ok=True)
    for rel in ["app", "components", "lib", "public", "scripts"]:
        copy_tree(REPO / rel, build_root / rel)
    for rel in [
        "package.json",
        "pnpm-lock.yaml",
        "next.config.mjs",
        "next-env.d.ts",
        "postcss.config.mjs",
        "tsconfig.json",
        "components.json",
        ".env",
    ]:
        if (REPO / rel).exists():
            copy_file(REPO / rel, build_root / rel)
    next_cfg = build_root / "next.config.mjs"
    txt = next_cfg.read_text(encoding="utf-8")
    if 'output: "standalone",' not in txt:
        txt = txt.replace("const nextConfig = {\n", 'const nextConfig = {\n  output: "standalone",\n', 1)
        next_cfg.write_text(txt, encoding="utf-8")
    env = dict(os.environ)
    env.setdefault("NEXT_TELEMETRY_DISABLED", "1")
    print("[build] installing dashboard dependencies in isolated workspace...", flush=True)
    run(["pnpm", "install", "--frozen-lockfile"], cwd=build_root, env=env)
    print("[build] building dashboard production bundle...", flush=True)
    run(["pnpm", "build"], cwd=build_root, env=env)
    print("[build] dashboard build complete", flush=True)
    return build_root


def _manifest_local_path(raw: object) -> Path | None:
    if isinstance(raw, dict):
        evidence_refs = dict(raw.get("evidence_refs") or {})
        value = raw.get("path") or raw.get("artifact_path") or evidence_refs.get(
            "artifact_path"
        )
    else:
        value = raw
    txt = str(value or "").strip()
    if not txt or "://" in txt:
        return None
    rel = Path(txt.replace("\\", "/"))
    if rel.is_absolute():
        raise RuntimeError(f"active manifest path must be repository-relative: {txt}")
    resolved = (REPO / rel).resolve()
    try:
        resolved.relative_to(REPO.resolve())
    except ValueError as exc:
        raise RuntimeError(f"active manifest path escapes the repository: {txt}") from exc
    if not resolved.exists():
        raise FileNotFoundError(resolved)
    return rel


def read_active_manifest() -> tuple[dict, list[str], list[Path], list[Path]]:
    manifest_path = REPO / "fx-quant-stack" / "artifacts" / "active_models.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    active_sets = dict(manifest.get("active_model_sets") or {})
    pairs = sorted(active_sets.keys())
    artifact_paths: set[Path] = set()
    registry_paths: set[Path] = set()
    for entry in active_sets.values():
        artifacts = dict((entry or {}).get("artifacts") or {})
        for raw_path in artifacts.values():
            local_path = _manifest_local_path(raw_path)
            if local_path is not None:
                artifact_paths.add(local_path)
        registry_path = _manifest_local_path((entry or {}).get("registry_path"))
        if registry_path is not None:
            registry_paths.add(registry_path)
    return manifest, pairs, sorted(artifact_paths), sorted(registry_paths)


def partition_tail_dirs(root: Path, *, pair: str, timeframe: str, limit: int) -> list[Path]:
    base = root / "provider=dukascopy" / f"pair={pair}" / f"timeframe={timeframe}"
    if not base.exists():
        return []
    date_dirs = sorted(path for path in base.iterdir() if path.is_dir() and path.name.startswith("date="))
    keep = max(1, int(limit))
    return date_dirs[-keep:]


def add_path_to_tar(tar: tarfile.TarFile, src: Path, arcname: Path) -> None:
    tar.add(src, arcname=str(arcname).replace("\\", "/"))


def stage_generated_files(root: Path) -> Path:
    app = root / "app"
    safe_rmtree(root)
    (app / "ops" / "windows").mkdir(parents=True, exist_ok=True)
    write_installed_env(app / "ops" / "windows" / "installed_env.bat")
    write_helper_batch(app / "start_trading_agent.bat", "set LAUNCH_NO_PAUSE=1&& call launch_all.bat live 10000")
    write_helper_batch(app / "stop_trading_agent.bat", "set LAUNCH_NO_PAUSE=1&& call launch_all.bat stop")
    write_helper_batch(app / "status_trading_agent.bat", "set LAUNCH_NO_PAUSE=1&& call launch_all.bat status")
    write_helper_batch(
        app / "monitor_trading_agent.bat",
        "call ops\\windows\\23_start_monitor.bat --run",
    )
    return app


def build_payload(out_dir: Path, *, dashboard_root: Path) -> Path:
    payload = out_dir / "payload.tar"
    if payload.exists():
        payload.unlink()

    generated_root = out_dir / "_generated"
    generated_app = stage_generated_files(generated_root)

    manifest, pairs, active_artifact_paths, registry_paths = read_active_manifest()

    dashboard_materialized = Path(tempfile.mkdtemp(prefix="tradingagent_dashboard_runtime_", dir="/tmp"))
    dashboard_materialized_standalone = dashboard_materialized / "standalone"
    shutil.copytree(dashboard_root / ".next" / "standalone", dashboard_materialized_standalone, symlinks=False)

    print(f"[build] creating payload archive -> {payload}", flush=True)
    try:
        with tarfile.open(payload, "w") as tar:
            add_path_to_tar(tar, generated_app, Path("app"))

            for rel in [
                "launch_all.bat",
                "next.config.mjs",
                "MQL4",
                "public",
                "fx-quant-stack/configs",
                "fx-quant-stack/alembic",
                "installer/windows",
            ]:
                src = REPO / rel
                if src.exists():
                    add_path_to_tar(tar, src, Path("app") / rel)

            for name in RUNTIME_OPS_FILES:
                rel = Path("ops") / "windows" / name
                src = REPO / rel
                if not src.is_file():
                    raise FileNotFoundError(src)
                add_path_to_tar(tar, src, Path("app") / rel)

            for rel in [
                "fx-quant-stack/alembic.ini",
                "fx-quant-stack/pyproject.toml",
                "fx-quant-stack/README.md",
                "fx-quant-stack/artifacts/active_models.json",
            ]:
                src = REPO / rel
                if src.exists():
                    add_path_to_tar(tar, src, Path("app") / rel)

            for rel in active_artifact_paths + registry_paths:
                src = REPO / rel
                if src.exists():
                    add_path_to_tar(tar, src, Path("app") / rel)

            feature_root = REPO / "fx-quant-stack" / "data" / "features"
            raw_root = REPO / "fx-quant-stack" / "data" / "raw"

            print("[build] adding runtime feature snapshot...", flush=True)
            for pair in pairs:
                for timeframe, limit in FEATURE_TAIL_LIMITS.items():
                    for date_dir in partition_tail_dirs(feature_root, pair=pair, timeframe=timeframe, limit=limit):
                        add_path_to_tar(tar, date_dir, Path("app") / date_dir.relative_to(REPO))

            print("[build] adding runtime raw snapshot...", flush=True)
            for pair in pairs:
                for timeframe, limit in RAW_TAIL_LIMITS.items():
                    for date_dir in partition_tail_dirs(raw_root, pair=pair, timeframe=timeframe, limit=limit):
                        add_path_to_tar(tar, date_dir, Path("app") / date_dir.relative_to(REPO))

            print("[build] adding packaged dashboard runtime...", flush=True)
            add_path_to_tar(tar, dashboard_materialized_standalone, Path("app") / ".next" / "standalone")
            add_path_to_tar(tar, dashboard_root / ".next" / "static", Path("app") / ".next" / "static")
            add_path_to_tar(tar, dashboard_root / ".next" / "BUILD_ID", Path("app") / ".next" / "BUILD_ID")

            print("[build] adding bundled python runtime...", flush=True)
            runtime_venv = active_runtime_venv()
            base_home = read_base_python_home(runtime_venv)
            add_path_to_tar(tar, base_home, Path("app") / "runtime" / "python")
            site_packages = runtime_venv / "Lib" / "site-packages"
            exclude_prefixes = (
                "torch",
                "functorch",
                "transformers",
                "pytorch_tcn",
                "pytorch_tcn-",
                "tokenizers",
                "safetensors",
                "huggingface_hub",
                "hf_xet",
                "nvidia",
                "triton",
                "sympy",
                "mpmath",
                "networkx",
                "jinja2",
                "markupsafe",
                "regex",
                "tqdm",
                "mlflow",
            )
            for item in site_packages.iterdir():
                name = item.name.lower()
                if name == "__pycache__":
                    continue
                if any(name == prefix or name.startswith(prefix) for prefix in exclude_prefixes):
                    continue
                add_path_to_tar(tar, item, Path("app") / "runtime" / "python" / "Lib" / "site-packages" / item.name)

            print("[build] adding bundled node runtime...", flush=True)
            node_dir = windows_to_wsl(r"C:\Program Files\nodejs")
            add_path_to_tar(tar, node_dir, Path("app") / "runtime" / "node")
    finally:
        safe_rmtree(dashboard_materialized)

    print("[build] payload archive complete", flush=True)
    safe_rmtree(generated_root)
    return payload


def write_readme(out_dir: Path) -> None:
    (out_dir / "README.txt").write_text(
        textwrap.dedent(
            """
            Trading Agent Windows Installer
            ===============================

            Primary installer:
            - TradingAgentSetup.exe

            Fallback folder install:
            - TradingAgentSetup.cmd
            - payload.tar
            - installer\\windows\\install.ps1

            Installation:
            1. Double-click TradingAgentSetup.exe.
            2. If Windows blocks the EXE wrapper, keep the folder contents together and run TradingAgentSetup.cmd.

            The installer places the application under:
            %LOCALAPPDATA%\\Programs\\TradingAgent

            It also creates desktop shortcuts for:
            - Trading Agent
            - Trading Agent Monitor
            - Trading Agent Stop
            - Trading Agent Status
            - Trading Agent Uninstall

            MT4 is not bundled. To enable live broker execution, install MT4 separately
            and attach BridgeEA after the Trading Agent is installed.
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def write_bootstrap_cmd(path: Path) -> None:
    path.write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "set \"SOURCE_ROOT=%~dp0\"\r\n"
        "powershell -NoProfile -ExecutionPolicy Bypass -File \"%~dp0installer\\windows\\install.ps1\" -SourceRoot \"%SOURCE_ROOT%\"\r\n"
        "exit /b %errorlevel%\r\n",
        encoding="utf-8",
    )


def build_iexpress(out_dir: Path) -> None:
    exe = out_dir / "TradingAgentSetup.exe"
    payload = out_dir / "payload.tar"
    sed = out_dir / "TradingAgentSetup.sed"
    if sed.exists():
        sed.unlink()

    stub_root = out_dir / "_stubbuild"
    safe_rmtree(stub_root)
    stub_root.mkdir(parents=True, exist_ok=True)
    publish_dir = stub_root / "publish"
    csproj = stub_root / "TradingAgentSetup.csproj"
    program = stub_root / "Program.cs"
    marker = b"TRADING_AGENT_PAYLOAD_V1"
    install_script = (REPO / "installer" / "windows" / "install.ps1").read_text(encoding="utf-8")

    csproj.write_text(
        textwrap.dedent(
            """
            <Project Sdk="Microsoft.NET.Sdk">
              <PropertyGroup>
                <OutputType>Exe</OutputType>
                <TargetFramework>net8.0-windows</TargetFramework>
                <ImplicitUsings>enable</ImplicitUsings>
                <Nullable>enable</Nullable>
                <PublishSingleFile>true</PublishSingleFile>
                <SelfContained>true</SelfContained>
                <RuntimeIdentifier>win-x64</RuntimeIdentifier>
                <PublishTrimmed>false</PublishTrimmed>
                <EnableCompressionInSingleFile>true</EnableCompressionInSingleFile>
              </PropertyGroup>
            </Project>
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    program.write_text(
        textwrap.dedent(
            f"""
            using System;
            using System.Diagnostics;
            using System.IO;
            using System.Text;

            internal static class Program
            {{
                private static readonly byte[] Marker = Encoding.ASCII.GetBytes({json.dumps(marker.decode("ascii"))});
                private static readonly string InstallScript = {json.dumps(install_script)};

                private static int Main(string[] args)
                {{
                    string exePath = Environment.ProcessPath
                        ?? Process.GetCurrentProcess().MainModule?.FileName
                        ?? throw new InvalidOperationException("unable to resolve installer executable path");
                    string tempRoot = Path.Combine(Path.GetTempPath(), "TradingAgentInstall_" + Guid.NewGuid().ToString("N"));
                    Directory.CreateDirectory(tempRoot);
                    try
                    {{
                        string payloadPath = Path.Combine(tempRoot, "payload.tar");
                        string installPath = Path.Combine(tempRoot, "install.ps1");
                        ExtractPayload(exePath, payloadPath);
                        File.WriteAllText(installPath, InstallScript, new UTF8Encoding(false));

                        var psi = new ProcessStartInfo("powershell.exe");
                        psi.ArgumentList.Add("-NoProfile");
                        psi.ArgumentList.Add("-ExecutionPolicy");
                        psi.ArgumentList.Add("Bypass");
                        psi.ArgumentList.Add("-File");
                        psi.ArgumentList.Add(installPath);
                        psi.ArgumentList.Add("-SourceRoot");
                        psi.ArgumentList.Add(tempRoot);
                        foreach (string arg in args)
                        {{
                            psi.ArgumentList.Add(arg);
                        }}
                        psi.UseShellExecute = false;

                        using var proc = Process.Start(psi) ?? throw new InvalidOperationException("failed to start installer powershell");
                        proc.WaitForExit();
                        return proc.ExitCode;
                    }}
                    catch (Exception ex)
                    {{
                        Console.Error.WriteLine(ex.ToString());
                        return 1;
                    }}
                    finally
                    {{
                        try
                        {{
                            if (Directory.Exists(tempRoot))
                            {{
                                Directory.Delete(tempRoot, true);
                            }}
                        }}
                        catch
                        {{
                        }}
                    }}
                }}

                private static void ExtractPayload(string exePath, string payloadPath)
                {{
                    using var input = new FileStream(exePath, FileMode.Open, FileAccess.Read, FileShare.Read);
                    long trailerSize = Marker.Length + sizeof(long);
                    if (input.Length <= trailerSize)
                    {{
                        throw new InvalidOperationException("installer payload trailer missing");
                    }}

                    input.Seek(-trailerSize, SeekOrigin.End);
                    byte[] trailer = new byte[trailerSize];
                    ReadExactly(input, trailer, 0, trailer.Length);
                    for (int i = 0; i < Marker.Length; i++)
                    {{
                        if (trailer[i] != Marker[i])
                        {{
                            throw new InvalidOperationException("installer payload marker mismatch");
                        }}
                    }}

                    long payloadLength = BitConverter.ToInt64(trailer, Marker.Length);
                    if (payloadLength <= 0 || payloadLength > (input.Length - trailerSize))
                    {{
                        throw new InvalidOperationException("installer payload length is invalid");
                    }}

                    long payloadOffset = input.Length - trailerSize - payloadLength;
                    input.Seek(payloadOffset, SeekOrigin.Begin);
                    using var output = new FileStream(payloadPath, FileMode.Create, FileAccess.Write, FileShare.None);
                    CopyExactly(input, output, payloadLength);
                }}

                private static void ReadExactly(Stream input, byte[] buffer, int offset, int count)
                {{
                    while (count > 0)
                    {{
                        int read = input.Read(buffer, offset, count);
                        if (read <= 0)
                        {{
                            throw new EndOfStreamException();
                        }}
                        offset += read;
                        count -= read;
                    }}
                }}

                private static void CopyExactly(Stream input, Stream output, long count)
                {{
                    byte[] buffer = new byte[1024 * 1024];
                    long remaining = count;
                    while (remaining > 0)
                    {{
                        int want = (int)Math.Min(buffer.Length, remaining);
                        int read = input.Read(buffer, 0, want);
                        if (read <= 0)
                        {{
                            throw new EndOfStreamException();
                        }}
                        output.Write(buffer, 0, read);
                        remaining -= read;
                    }}
                }}
            }}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    print(f"[build] creating bootstrap exe -> {exe}", flush=True)
    project_win = wsl_to_windows(csproj)
    publish_win = wsl_to_windows(publish_dir)
    dotnet_cmd = (
        f"@echo off\r\n"
        f"\"C:\\Program Files\\dotnet\\dotnet.exe\" publish \"{project_win}\" "
        "-c Release -r win-x64 "
        "-p:PublishSingleFile=true -p:SelfContained=true -p:EnableCompressionInSingleFile=true "
        f"-o \"{publish_win}\"\r\n"
    )
    publish_cmd = stub_root / "publish_stub.cmd"
    publish_cmd.write_text(dotnet_cmd, encoding="utf-8")
    publish_cmd_win = wsl_to_windows(publish_cmd)
    try:
        run(["cmd.exe", "/c", publish_cmd_win])
        stub_exe = publish_dir / "TradingAgentSetup.exe"
        if not stub_exe.exists():
            raise FileNotFoundError(stub_exe)
        with stub_exe.open("rb") as src, payload.open("rb") as pay, exe.open("wb") as out:
            shutil.copyfileobj(src, out)
            shutil.copyfileobj(pay, out)
            out.write(marker)
            out.write(payload.stat().st_size.to_bytes(8, byteorder="little", signed=True))
    finally:
        safe_rmtree(stub_root)
    print("[build] bootstrap exe complete", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a one-click Windows installer bundle for the Trading Agent.")
    ap.add_argument("--out-dir", default=str(REPO / "dist" / "windows" / "TradingAgentInstaller"))
    args = ap.parse_args()

    out_dir = Path(args.out_dir).resolve()
    dashboard_root = Path(tempfile.mkdtemp(prefix="tradingagent_dashboard_build_", dir="/tmp"))
    safe_rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        build_dashboard(dashboard_root)
        build_payload(out_dir, dashboard_root=dashboard_root)
        write_bootstrap_cmd(out_dir / "TradingAgentSetup.cmd")
        copy_tree(REPO / "installer" / "windows", out_dir / "installer" / "windows")
        copy_file(REPO / "installer" / "windows" / "install.ps1", out_dir / "install.ps1")
        copy_file(REPO / "installer" / "windows" / "install_from_payload.cmd", out_dir / "install_from_payload.cmd")
        write_readme(out_dir)
        build_iexpress(out_dir)
        print(f"built installer bundle at {out_dir}", flush=True)
    finally:
        safe_rmtree(dashboard_root)


if __name__ == "__main__":
    main()
