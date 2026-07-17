[CmdletBinding()]
param(
    [string]$GameRoot = 'C:\Program Files (x86)\Steam\steamapps\common\Omikron',
    [string]$OutputDirectory,
    [string]$PythonPath,
    [string]$BlenderPath,
    [string]$ValidatorPath,
    [switch]$SkipBlender,
    [switch]$SkipValidator
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ToolDirectory = $PSScriptRoot
$RepositoryRoot = Split-Path -Parent $ToolDirectory
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $RepositoryRoot 'exports\Anekbah'
}

$GameRoot = (Resolve-Path -LiteralPath $GameRoot).Path
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
if (-not $PythonPath) {
    $PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        $PythonPath = $PythonCommand.Source
    }
    else {
        $KnownPython = Join-Path `
            ([Environment]::GetFolderPath('LocalApplicationData')) `
            'Programs\Python\Python39\python.exe'
        if (Test-Path -LiteralPath $KnownPython -PathType Leaf) {
            $PythonPath = $KnownPython
        }
    }
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'Python was not found. Supply the interpreter with -PythonPath.'
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$Converter = Join-Path $ToolDirectory 'omikron_glb.py'
$EffectsTool = Join-Path $ToolDirectory 'anekbah_effects.py'
$InteriorsTool = Join-Path $ToolDirectory 'anekbah_interiors.py'
$ComposeTool = Join-Path $ToolDirectory 'anekbah_compose.py'
$PreviewTool = Join-Path $ToolDirectory 'blender_preview.py'
$ZoneLayout = Join-Path $ToolDirectory 'anekbah_zone_layout.json'
$DecorSource = Join-Path $GameRoot 'MESHES\DECORS\Anekbah.3DO'
$SkySource = Join-Path $GameRoot 'MESHES\MISC\Asky.3DO'
$DecorGlb = Join-Path $OutputDirectory 'Anekbah.glb'
$SkyGlb = Join-Path $OutputDirectory 'Asky.glb'
$EffectsDirectory = Join-Path $OutputDirectory 'effects'
$InteriorsDirectory = Join-Path $OutputDirectory 'interiors'
$InteriorReport = Join-Path $InteriorsDirectory 'anekbah_interiors_report.json'
$CompleteGlb = Join-Path $OutputDirectory 'Anekbah_complete.glb'
$CompositionReport = Join-Path $OutputDirectory 'Anekbah_complete.composition.json'
$PreviewDirectory = Join-Path $OutputDirectory 'previews_complete'
$BlendOutput = Join-Path $OutputDirectory 'Anekbah_complete.blend'

foreach ($RequiredFile in @(
    $Converter,
    $EffectsTool,
    $InteriorsTool,
    $ComposeTool,
    $PreviewTool,
    $ZoneLayout,
    $DecorSource,
    $SkySource
)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "Required file not found: $RequiredFile"
    }
}
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

Write-Host 'Running converter, installed-data, and direct-composition regression tests...'
Push-Location $ToolDirectory
try {
    & $PythonPath -m unittest -v test_omikron_glb.py test_anekbah_compose.py
    if ($LASTEXITCODE -ne 0) { throw "Regression tests failed with exit code $LASTEXITCODE" }
}
finally {
    Pop-Location
}

Write-Host 'Converting the source-faithful Anekbah exterior...'
& $PythonPath $Converter convert $DecorSource `
    --lighting baked `
    --camera-aspect-ratio 1.3333333333333333 `
    -o $DecorGlb
if ($LASTEXITCODE -ne 0) { throw "Anekbah conversion failed with exit code $LASTEXITCODE" }

Write-Host 'Converting Asky as a standalone research artifact (not used in the final scene)...'
& $PythonPath $Converter convert $SkySource `
    --lighting baked `
    --camera-aspect-ratio 1.3333333333333333 `
    -o $SkyGlb
if ($LASTEXITCODE -ne 0) { throw "Asky conversion failed with exit code $LASTEXITCODE" }

Write-Host 'Extracting the embedded Anekbah effect assets...'
& $PythonPath $EffectsTool extract $GameRoot $EffectsDirectory `
    --lighting baked `
    --camera-aspect-ratio 1.3333333333333333 `
    --overwrite
if ($LASTEXITCODE -ne 0) { throw "Anekbah effect extraction failed with exit code $LASTEXITCODE" }

Write-Host 'Converting the 81 canonical Anekbah interiors with decoded explicit lights...'
& $PythonPath $InteriorsTool convert $GameRoot $InteriorsDirectory --overwrite
if ($LASTEXITCODE -ne 0) { throw "Anekbah interior conversion failed with exit code $LASTEXITCODE" }

Write-Host 'Composing the exterior and manifest-selected interiors directly into Anekbah_complete.glb...'
& $PythonPath $ComposeTool $DecorGlb $InteriorsDirectory `
    -o $CompleteGlb `
    --report $CompositionReport `
    --overwrite
if ($LASTEXITCODE -ne 0) { throw "Anekbah GLB composition failed with exit code $LASTEXITCODE" }

$CoreGlbs = @(
    $DecorGlb,
    (Join-Path $EffectsDirectory 'Anekbah_smoke.glb'),
    (Join-Path $EffectsDirectory 'Anekbah_glow.glb'),
    (Join-Path $EffectsDirectory 'Anekbah_explosion.glb'),
    $CompleteGlb
)
$ResearchGlbs = @($SkyGlb)
$InteriorManifestData = Get-Content -Raw -LiteralPath $InteriorReport | ConvertFrom-Json
$InteriorGlbs = @(
    $InteriorManifestData.interiors |
        ForEach-Object {
            $DeclaredPath = [string]$_.output.file
            if (-not (Test-Path -LiteralPath $DeclaredPath -PathType Leaf)) {
                throw "Manifest-listed interior GLB not found: $DeclaredPath"
            }
            (Resolve-Path -LiteralPath $DeclaredPath).Path
        }
)
if ($InteriorGlbs.Count -ne 81) {
    throw "Expected 81 manifest-selected canonical interior GLBs, found $($InteriorGlbs.Count)"
}
$VerifiedGlbs = @($CoreGlbs + $ResearchGlbs + $InteriorGlbs)

Write-Host "Running the built-in verifier on $($VerifiedGlbs.Count) GLBs..."
foreach ($Glb in $VerifiedGlbs) {
    & $PythonPath $Converter verify $Glb
    if ($LASTEXITCODE -ne 0) { throw "Built-in GLB verification failed: $Glb" }
}

$KhronosValidatorRun = $false
if (-not $SkipValidator) {
    if ($ValidatorPath) {
        $ValidatorPath = (Resolve-Path -LiteralPath $ValidatorPath).Path
        Write-Host "Running independent Khronos validation on $($VerifiedGlbs.Count) GLBs..."
        foreach ($Glb in $VerifiedGlbs) {
            & $ValidatorPath $Glb
            if ($LASTEXITCODE -ne 0) { throw "Khronos validation failed: $Glb" }
        }
        $KhronosValidatorRun = $true
    }
    else {
        Write-Warning 'ValidatorPath was not supplied; skipping independent Khronos validation.'
    }
}

if (-not $SkipBlender) {
    if (-not $BlenderPath) {
        $BlenderCommand = Get-Command blender.exe -ErrorAction SilentlyContinue
        if ($BlenderCommand) {
            $BlenderPath = $BlenderCommand.Source
        }
        else {
            $KnownBlenderPaths = [System.Collections.Generic.List[string]]::new()
            $ProgramFilesX86 = [Environment]::GetFolderPath('ProgramFilesX86')
            if ($ProgramFilesX86) {
                $KnownBlenderPaths.Add(
                    (Join-Path $ProgramFilesX86 'Steam\steamapps\common\Blender\blender.exe')
                )
            }
            $ProgramFiles = [Environment]::GetFolderPath('ProgramFiles')
            $BlenderFoundation = Join-Path $ProgramFiles 'Blender Foundation'
            if (Test-Path -LiteralPath $BlenderFoundation -PathType Container) {
                Get-ChildItem -LiteralPath $BlenderFoundation -Directory |
                    Sort-Object -Property Name -Descending |
                    ForEach-Object {
                        $KnownBlenderPaths.Add((Join-Path $_.FullName 'blender.exe'))
                    }
            }
            $BlenderPath = $KnownBlenderPaths |
                Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
                Select-Object -First 1
        }
    }
    if (-not $BlenderPath -or -not (Test-Path -LiteralPath $BlenderPath -PathType Leaf)) {
        throw 'Blender was not found. Supply blender.exe with -BlenderPath or use -SkipBlender.'
    }
    $BlenderPath = (Resolve-Path -LiteralPath $BlenderPath).Path
    # The positional preview interface places SKY before EFFECTS_DIR and
    # INTERIORS_DIR. For Anekbah the script intentionally ignores the fourth
    # argument, so pass a sentinel rather than the bounded Asky plane.
    $OmittedSkyPlaceholder = Join-Path $OutputDirectory '_OMITTED_ANEKBAH_SKY_'
    Write-Host 'Building the complete calibrated Blender scene and rendering source cameras...'
    & $BlenderPath --background --factory-startup `
        --python $PreviewTool -- `
        $DecorGlb `
        $PreviewDirectory `
        $BlendOutput `
        $OmittedSkyPlaceholder `
        $EffectsDirectory `
        $InteriorsDirectory
    if ($LASTEXITCODE -ne 0) { throw "Blender composition failed with exit code $LASTEXITCODE" }
}

$ArtifactSpecs = [System.Collections.Generic.List[object]]::new()
foreach ($Spec in @(
    [pscustomobject]@{ role = 'exteriorGlb'; path = $DecorGlb },
    [pscustomobject]@{ role = 'completeGlb'; path = $CompleteGlb },
    [pscustomobject]@{ role = 'compositionReport'; path = $CompositionReport },
    [pscustomobject]@{ role = 'teleportZoneLayout'; path = $ZoneLayout },
    [pscustomobject]@{ role = 'interiorManifest'; path = $InteriorReport },
    [pscustomobject]@{ role = 'rawSmokeGlb'; path = (Join-Path $EffectsDirectory 'Anekbah_smoke.glb') },
    [pscustomobject]@{ role = 'rawGlowGlb'; path = (Join-Path $EffectsDirectory 'Anekbah_glow.glb') },
    [pscustomobject]@{ role = 'rawExplosionGlb'; path = (Join-Path $EffectsDirectory 'Anekbah_explosion.glb') },
    [pscustomobject]@{ role = 'effectsReport'; path = (Join-Path $EffectsDirectory 'anekbah_effects_report.json') },
    [pscustomobject]@{ role = 'standaloneSkyResearchGlb'; path = $SkyGlb }
)) {
    $ArtifactSpecs.Add($Spec)
}
foreach ($Glb in $InteriorGlbs) {
    $ArtifactSpecs.Add([pscustomobject]@{ role = 'interiorGlb'; path = $Glb })
}
if (-not $SkipBlender) {
    $ArtifactSpecs.Add([pscustomobject]@{ role = 'completeBlend'; path = $BlendOutput })
    $ArtifactSpecs.Add([pscustomobject]@{
        role = 'blenderValidationReport'
        path = (Join-Path $PreviewDirectory 'blender_validation.json')
    })
    foreach ($Preview in @(Get-ChildItem -LiteralPath $PreviewDirectory -Filter 'preview_*.png' -File | Sort-Object Name)) {
        $ArtifactSpecs.Add([pscustomobject]@{ role = 'previewPng'; path = $Preview.FullName })
    }
}

$ArtifactRecords = foreach ($Spec in $ArtifactSpecs) {
    $File = Get-Item -LiteralPath $Spec.path
    [ordered]@{
        role = $Spec.role
        file = $File.FullName
        bytes = $File.Length
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $File.FullName).Hash.ToLowerInvariant()
    }
}
$BuildReport = Join-Path $OutputDirectory 'anekbah_build_report.json'
$Summary = [ordered]@{
    schema = 'omikron-anekbah-build-v2'
    generatedAtUtc = [DateTime]::UtcNow.ToString('o')
    gameRoot = $GameRoot
    exteriorGlb = $DecorGlb
    completeGlb = $CompleteGlb
    interiorsDirectory = $InteriorsDirectory
    interiorManifest = $InteriorReport
    teleportZoneLayout = $ZoneLayout
    effectsDirectory = $EffectsDirectory
    standaloneSkyResearchGlb = $SkyGlb
    finalSkyIncluded = $false
    previewDirectory = if ($SkipBlender) { $null } else { $PreviewDirectory }
    blend = if ($SkipBlender) { $null } else { $BlendOutput }
    artifacts = $ArtifactRecords
    canonicalInteriorGlbs = $InteriorGlbs.Count
    decodedInteriorExplicitLights = [int]$InteriorManifestData.totals.explicitLights
    unresolvedInteriorMeshLights = [int]$InteriorManifestData.totals.undecodedMeshLights
    builtInVerifiedGlbs = $VerifiedGlbs.Count
    khronosValidatedGlbs = if ($KhronosValidatorRun) { $VerifiedGlbs.Count } else { 0 }
    khronosValidatorRun = $KhronosValidatorRun
}
$SummaryJson = $Summary | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText(
    $BuildReport,
    $SummaryJson + [Environment]::NewLine,
    [System.Text.UTF8Encoding]::new($false)
)
$SummaryJson
Write-Host "Build report: $BuildReport"
