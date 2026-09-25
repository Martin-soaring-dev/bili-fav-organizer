$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$requirements = Join-Path $projectDir "requirements.txt"
$serverScript = Join-Path $projectDir "server.py"
$appData = Join-Path $env:LOCALAPPDATA "BiliFavOrganizer"
$venvRoot = Join-Path $appData "runtime"

function Resolve-Python {
    param([string]$commandName, [string[]]$prefixArgs)

    $command = Get-Command $commandName -CommandType Application -ErrorAction SilentlyContinue
    if (-not $command) { return $null }

    try {
        $callArgs = @($prefixArgs) + @("-c", "import sys; assert sys.version_info >= (3, 10); print(sys.executable)")
        $result = & $command.Source @callArgs 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        $candidate = ($result | Select-Object -Last 1).ToString().Trim()
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    } catch { }

    return $null
}

function Find-Python {
    $candidates = @(
        @{ Name = "pymanager"; Args = @("exec", "-V:3.13") },
        @{ Name = "py"; Args = @("-3.13") },
        @{ Name = "py"; Args = @("-3.12") },
        @{ Name = "py"; Args = @("-3.11") },
        @{ Name = "py"; Args = @("-3.10") },
        @{ Name = "py"; Args = @("-3") },
        @{ Name = "python"; Args = @() }
    )
    foreach ($candidate in $candidates) {
        $path = Resolve-Python $candidate.Name $candidate.Args
        if ($path) { return $path }
    }
    return $null
}

try {
    if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
        throw "缺少 requirements.txt，请先完整解压项目文件夹。"
    }

    New-Item -ItemType Directory -Path $venvRoot -Force | Out-Null
    $python = Find-Python

    if (-not $python) {
        Write-Host "未检测到 Python 3.10 或更新版本，正在准备 Python 运行环境..."
        $winget = Get-Command winget -CommandType Application -ErrorAction SilentlyContinue
        if ($winget) {
            Write-Host "正在通过 Windows 包管理器安装官方 Python 安装管理器..."
            & $winget.Source install 9NQ7512CXL7T -e --accept-package-agreements --disable-interactivity
            if ($LASTEXITCODE -eq 0) {
                $windowsApps = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps"
                if (Test-Path -LiteralPath $windowsApps) { $env:Path = "$windowsApps;$env:Path" }
                $manager = Get-Command pymanager -CommandType Application -ErrorAction SilentlyContinue
                $managerPath = if ($manager) { $manager.Source } else { Join-Path $windowsApps "pymanager.exe" }
                if (Test-Path -LiteralPath $managerPath -PathType Leaf) {
                    Write-Host "正在安装 Python 3.13..."
                    & $managerPath install 3.13
                    if ($LASTEXITCODE -eq 0) { $python = Find-Python }
                }
            }
        }

        if (-not $python) {
            Write-Host "正在从 python.org 下载并安装 Python 3.13.15..."
            $architecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
            $installerName = switch ($architecture.ToUpperInvariant()) {
                "AMD64" { "python-3.13.15-amd64.exe"; break }
                "ARM64" { "python-3.13.15-arm64.exe"; break }
                default { throw "当前系统架构 $architecture 暂不支持自动安装。请从 https://www.python.org/downloads/windows/ 安装 Python 3.13，然后重试。" }
            }
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $installerUrl = "https://www.python.org/ftp/python/3.13.15/$installerName"
            $installerPath = Join-Path $env:TEMP $installerName
            Invoke-WebRequest -Uri $installerUrl -OutFile $installerPath -UseBasicParsing
            $signature = Get-AuthenticodeSignature -LiteralPath $installerPath
            if ($signature.Status -ne "Valid") { throw "Python 安装程序签名验证失败，已停止安装。" }

            $pythonHome = Join-Path $venvRoot "python313"
            Write-Host "正在安装 Python 和 pip（当前用户，不需要管理员权限）..."
            & $installerPath /quiet "InstallAllUsers=0" "TargetDir=$pythonHome" "Include_pip=1" "Include_launcher=0" "Include_test=0" "PrependPath=0" "AssociateFiles=0" "Shortcuts=0"
            if ($LASTEXITCODE -ne 0) { throw "Python 安装失败，安装程序退出代码：$LASTEXITCODE" }
            $python = Join-Path $pythonHome "python.exe"
            if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Python 安装结束，但未找到 python.exe。" }
        }
    }

    $versionTag = & $python -c "import sys; print('py%d%d' % sys.version_info[:2])"
    if ($LASTEXITCODE -ne 0) { throw "无法读取 Python 版本。" }
    $venv = Join-Path $venvRoot $versionTag.Trim()
    $venvPython = Join-Path $venv "Scripts\python.exe"

    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Write-Host "正在准备应用专用环境..."
        & $python -m ensurepip --upgrade
        if ($LASTEXITCODE -ne 0) { throw "无法通过 Python ensurepip 安装 pip。" }
        & $python -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw "无法创建应用专用环境：$venv" }
    }

    $hash = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash
    $marker = Join-Path $venv "requirements.sha256"
    $oldHash = if (Test-Path -LiteralPath $marker) { (Get-Content -LiteralPath $marker -Raw).Trim() } else { "" }
    $dependenciesReady = $oldHash -eq $hash
    if ($dependenciesReady) {
        & $venvPython -c "import fastapi, uvicorn, requests, pydantic, qrcode, browsercookie, Cryptodome" 2>$null
        $dependenciesReady = $LASTEXITCODE -eq 0
    }
    if (-not $dependenciesReady) {
        Write-Host "正在确保 pip 已安装..."
        & $venvPython -m ensurepip --upgrade
        if ($LASTEXITCODE -ne 0) { throw "无法在应用专用环境中安装 pip。" }

        Write-Host "正在安装应用依赖，可能需要几分钟..."
        & $venvPython -m pip install --disable-pip-version-check --upgrade pip
        if ($LASTEXITCODE -ne 0) { throw "pip 准备失败。请检查网络连接或代理设置。" }
        & $venvPython -m pip install --disable-pip-version-check -r $requirements
        if ($LASTEXITCODE -ne 0) { throw "依赖安装失败。请检查网络连接或代理设置，然后重新运行启动.bat。" }
        Set-Content -LiteralPath $marker -Value $hash -Encoding ascii
    } else {
        Write-Host "Python 和应用依赖已就绪。"
    }

    Write-Host "正在打开应用：http://127.0.0.1:8080"
    Start-Process "http://127.0.0.1:8080"
    & $venvPython $serverScript --port 8080
    exit $LASTEXITCODE
} catch {
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
