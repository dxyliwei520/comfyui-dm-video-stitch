# 用 Personal Access Token 发布到 Comfy Registry（token 从文件读，绝不打印）
# 用法：先把 token 存进本目录的 .registry-token（一行，别加引号），然后运行本脚本
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$tf = Join-Path $PSScriptRoot '.registry-token'
if (-not (Test-Path $tf)) {
    Write-Host '!! 缺 .registry-token' -ForegroundColor Yellow
    Write-Host '   打开 https://registry.comfy.org -> 头像/设置 -> 生成 Personal Access Token' -ForegroundColor Yellow
    Write-Host ('   把它写进：' + $tf + '（一行，不带引号）') -ForegroundColor Yellow
    notepad $tf
    exit 1
}
$tok = (Get-Content $tf -Raw).Trim()
if ($tok.Length -lt 20) { Write-Host '!! .registry-token 内容太短，可能没粘全' -ForegroundColor Red; exit 1 }
Write-Host ("token 已读取（长度 " + $tok.Length + "，不回显）") -ForegroundColor DarkGray

$comfy = 'F:\_tmp\comfycli\Scripts\comfy.exe'
if (-not (Test-Path $comfy)) { Write-Host '!! 找不到 comfy-cli，请先跑 publish-registry.ps1 里的安装步骤' -ForegroundColor Red; exit 1 }

Write-Host '== 1) 校验节点包 ==' -ForegroundColor Cyan
& $comfy node validate
if ($LASTEXITCODE -ne 0) { Write-Host '!! validate 失败，先按提示修 pyproject.toml' -ForegroundColor Red; exit 1 }

Write-Host '== 2) 发布 ==' -ForegroundColor Cyan
$ver = (Select-String -Path '.\pyproject.toml' -Pattern '^version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
& $comfy node publish --token $tok --changelog "v$ver 首次发布：视频无缝拼接（流式防 OOM），支持 5 段 VIDEO 直连与不限段数路径列表；drop/crossfade 两种衔接；自动归一化分辨率/fps/10bit"
if ($LASTEXITCODE -ne 0) { Write-Host '!! 发布失败（常见：version 已存在 / PublisherId 与 token 不匹配）' -ForegroundColor Red; exit 1 }

Write-Host ''
Write-Host '== DONE ==' -ForegroundColor Green
Write-Host '  https://registry.comfy.org/nodes/dm-video-stitch'