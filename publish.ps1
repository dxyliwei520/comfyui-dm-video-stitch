# 一键发布 DM Video Stitch 到 GitHub（纯 ASCII 提示，避免 cmd 编码问题）
# 用法：双击，或在 F:\dm-video-stitch-repo 里执行 powershell -ExecutionPolicy Bypass -File publish.ps1
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$remote = 'git@github.com:dxyliwei520/comfyui-dm-video-stitch.git'
Write-Host "== 0) check remote ==" -ForegroundColor Cyan
git remote -v

Write-Host "== 1) probe repo exists? ==" -ForegroundColor Cyan
$probe = & git ls-remote --heads $remote 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "!! Repository NOT found on GitHub:" -ForegroundColor Yellow
    Write-Host "   $remote"
    Write-Host ""
    Write-Host "   Please create it first (Public, EMPTY - no README/gitignore/license):" -ForegroundColor Yellow
    Write-Host "   https://github.com/new   name: comfyui-dm-video-stitch" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "   Then run this script again." -ForegroundColor Yellow
    exit 1
}

Write-Host "== 2) push ==" -ForegroundColor Cyan
git push -u origin main
if ($LASTEXITCODE -ne 0) { Write-Host "!! push failed" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "== DONE ==" -ForegroundColor Green
Write-Host "Repo   : https://github.com/dxyliwei520/comfyui-dm-video-stitch"
Write-Host "ZipURL : https://github.com/dxyliwei520/comfyui-dm-video-stitch/archive/refs/heads/main.zip"
Write-Host ""
Write-Host "-- install (option A: git clone) --" -ForegroundColor Cyan
Write-Host "  cd D:\ComfyUI_experimental\ComfyUI_windows_portable\ComfyUI\custom_nodes"
Write-Host "  git clone https://github.com/dxyliwei520/comfyui-dm-video-stitch dm_video_stitch"
Write-Host ""
Write-Host "-- install (option B: Manager -> Install via Git URL) --" -ForegroundColor Cyan
Write-Host "  paste: https://github.com/dxyliwei520/comfyui-dm-video-stitch"
Write-Host "  (needs allow_git_url_install = True in Manager config.ini)"
Write-Host ""
Write-Host "-- publish to Comfy Registry (option C, needs your token) --" -ForegroundColor Cyan
Write-Host "  pip install comfy-cli ; comfy --skip-prompt model registry login ; comfy node publish"
