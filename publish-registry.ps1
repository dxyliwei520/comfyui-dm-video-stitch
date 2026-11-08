# 发布到 Comfy 官方 Registry（这样所有人在 ComfyUI-Manager 里都能搜到）
# 需要：GitHub 账号（浏览器登录一次）+ 在 registry.comfy.org 拿到 PublisherId
# 用法：双击，或在仓库目录执行 powershell -ExecutionPolicy Bypass -File publish-registry.ps1
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$py = 'C:\Users\liwei\AppData\Local\Programs\Python\Python312\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }
$venv = 'F:\_tmp\comfycli'

function Say($m, $c = 'Cyan') { Write-Host $m -ForegroundColor $c }

Say '== 0) 检查 pyproject.toml 的 PublisherId =='
$pp = Get-Content '.\pyproject.toml' -Raw -Encoding UTF8
if ($pp -match 'PublisherId\s*=\s*"REPLACE_WITH_YOUR_PUBLISHER_ID"') {
    Say '!! 还没填 PublisherId。' 'Yellow'
    Say '   1) 打开 https://registry.comfy.org 用 GitHub 登录' 'Yellow'
    Say '   2) 复制页面上的 Publisher ID' 'Yellow'
    Say '   3) 把 pyproject.toml 里 PublisherId 那一行改掉，再重跑本脚本' 'Yellow'
    notepad '.\pyproject.toml'
    exit 1
}
Say '   OK: PublisherId 已填写'

Say '== 1) 建独立 venv（不污染 ComfyUI 的 python_embeded）=='
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    & $py -m venv $venv
}
$vpy = "$venv\Scripts\python.exe"
& $vpy -m pip install -q -i https://mirrors.cloud.tencent.com/pypi/simple --upgrade pip
Say '   安装 comfy-cli …'
& $vpy -m pip install -q -i https://mirrors.cloud.tencent.com/pypi/simple comfy-cli
if ($LASTEXITCODE -ne 0) { Say '!! comfy-cli 安装失败' 'Red'; exit 1 }
$comfy = "$venv\Scripts\comfy.exe"
Say "   comfy-cli: $comfy"

Say '== 2) 登录 registry（会给出一个 GitHub 授权码，浏览器点一下）=='
& $comfy --skip-prompt model registry login
if ($LASTEXITCODE -ne 0) { Say '!! 登录失败，重跑本脚本' 'Red'; exit 1 }

Say '== 3) 发布 =='
& $comfy node publish
if ($LASTEXITCODE -ne 0) { Say '!! 发布失败（常见原因：version 已存在 → 先改 pyproject.toml 的 version）' 'Red'; exit 1 }

Say ''
Say '== DONE ==' 'Green'
Say '  https://registry.comfy.org/nodes/dm-video-stitch'
Say '  之后任何人的 ComfyUI-Manager 里都能搜到 "DM Video Stitch"，或  comfy node install dm-video-stitch'
