param(
    [string]$FFmpegPath = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not $FFmpegPath) {
    $ffmpegCommand = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
    if ($ffmpegCommand) {
        $FFmpegPath = $ffmpegCommand.Source
    }
}
if (-not $FFmpegPath -or -not (Test-Path -LiteralPath $FFmpegPath -PathType Leaf)) {
    throw "找不到 ffmpeg.exe。请先安装 FFmpeg，或用 -FFmpegPath 指定完整路径。"
}

$distRoot = Join-Path $root "dist"
$workRoot = Join-Path $root "build\pyinstaller"
$buildDist = Join-Path $workRoot "dist"
$packageName = "LightRec-portable-x64-fast"
$packageDir = Join-Path $distRoot $packageName
$archive = Join-Path $distRoot "$packageName.zip"
$stageDir = Join-Path $distRoot (".LightRec-package-" + [guid]::NewGuid().ToString("N"))
$stagedArchive = Join-Path $distRoot (".LightRec-package-" + [guid]::NewGuid().ToString("N") + ".zip")
$specDir = Join-Path $workRoot "spec"

py -3.14 -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name LightRec `
    --distpath $buildDist `
    --workpath $workRoot `
    --specpath $specDir `
    --hidden-import numpy `
    --collect-all soundcard `
    --add-binary "$FFmpegPath;." `
    --add-data "$root\assets\onenote-tray.ico;assets" `
    lightrec.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 打包失败，退出码 $LASTEXITCODE。"
}

try {
    New-Item -ItemType Directory -Force -Path $stageDir | Out-Null
    $stagedPackage = Join-Path $stageDir $packageName
    Copy-Item -LiteralPath (Join-Path $buildDist "LightRec") -Destination $stagedPackage -Recurse
    Copy-Item -LiteralPath (Join-Path $root "README.md") -Destination (Join-Path $stageDir "使用说明.md")
    Copy-Item -LiteralPath (Join-Path $root "THIRD_PARTY_NOTICES.txt") -Destination $stageDir

    Compress-Archive -Path (Join-Path $stageDir "*") -DestinationPath $stagedArchive -CompressionLevel Optimal -Force
    Move-Item -LiteralPath $stagedArchive -Destination $archive -Force
    New-Item -ItemType Directory -Force -Path $packageDir | Out-Null
    Copy-Item -Path (Join-Path $stagedPackage "*") -Destination $packageDir -Recurse -Force
}
finally {
    Remove-Item -LiteralPath $stageDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $stagedArchive -Force -ErrorAction SilentlyContinue
}

Get-Item -LiteralPath $archive | Select-Object FullName,Length,LastWriteTime
