param([int]$Seconds = 30, [string]$Label = 'gpu-observation')
$ErrorActionPreference = 'Stop'
$targetProcesses = @(Get-CimInstance Win32_Process -Filter "Name='FotoArchive.exe' OR Name='llama-server.exe'")
$targetIds = @($targetProcesses.ProcessId)
$rows = @(Get-Counter '\GPU Engine(*)\Utilization Percentage' -SampleInterval 1 -MaxSamples $Seconds | ForEach-Object {
    $sample = $_
    $selected = @($sample.CounterSamples | Where-Object {
        $_.InstanceName -match '^pid_(\d+)_' -and [int]$Matches[1] -in $targetIds -and $_.CookedValue -gt 0.05
    } | ForEach-Object { [pscustomobject]@{engine=$_.InstanceName;usage=$_.CookedValue} })
    [pscustomobject]@{at=$sample.Timestamp.ToUniversalTime().ToString('o');engines=$selected}
})
$outputPath = Join-Path 'D:\FotoArchiveData\reports' ($Label + '.json')
[pscustomobject]@{processes=@($targetProcesses | Select-Object Name,ProcessId,ParentProcessId);samples=$rows} |
    ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $outputPath -Encoding utf8
[pscustomobject]@{samples=$rows.Count;output=$outputPath} | ConvertTo-Json
