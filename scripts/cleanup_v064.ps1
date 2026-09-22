$ErrorActionPreference = 'Stop'
$reportPath = 'D:\FotoArchiveData\reports\v064-storage-cleanup.json'
$beforeFree = (Get-PSDrive D).Free
$targets = @(
    'D:\FotoArchiveData\backups',
    'D:\FotoArchiveData\reports\v05-pipeline-3c3j9p1c',
    'D:\FotoArchiveData\reports\v05-pipeline-xxgz0ulw',
    'D:\FotoArchiveData\reports\v04-copies-evpjsgsh',
    'D:\FotoArchiveData\reports\v04-copies-46be_wqf',
    'D:\FotoArchiveData\reports\v04-copies-t5gh3hr0',
    'D:\FotoArchiveData\reports\v052-pipeline-ovrqyp6a',
    'D:\FotoArchiveData\reports\scale_200k',
    'D:\FotoArchiveData\reports\scale_faces_v03',
    'D:\FotoArchiveData\reports\v061-media-runtime',
    'D:\FotoArchiveData\reports\v060-media-runtime',
    'D:\FotoArchiveData\reports\v060-inventory-hw2lbf22',
    'D:\FotoArchiveData\reports\v061-inventory-3l3ocahn',
    'D:\FotoArchiveData\reports\v054-inventory-zxn1ap8b',
    'D:\FotoArchiveData\reports\v053-inventory-wo97agh3',
    'D:\FotoArchiveData\reports\v053-inventory-hq9yux47',
    'D:\FotoArchiveData\reports\v061-inventory-in4ys277',
    'D:\FotoArchiveData\reports\geosearch_fixture',
    'D:\FotoArchiveData\reports\orientation_probe',
    'D:\Projects\FotoArchive\dist\FotoArchive-v050-20260921',
    'D:\Projects\FotoArchive\dist\FotoArchive-v051-20260921',
    'D:\Projects\FotoArchive\dist\FotoArchive-v052-20260921',
    'D:\Projects\FotoArchive\dist\FotoArchive-v053-20260921',
    'D:\Projects\FotoArchive\dist\FotoArchive-v060-20260921',
    'D:\Projects\FotoArchive\dist\FotoArchive-v061-20260921',
    'D:\Projects\FotoArchive\dist-staging-v054'
)
$deleted = [System.Collections.Generic.List[string]]::new()
$unlinked = [System.Collections.Generic.List[string]]::new()
foreach ($target in $targets) {
    if (-not (Test-Path -LiteralPath $target)) { continue }
    $resolved = (Resolve-Path -LiteralPath $target).ProviderPath.TrimEnd('\')
    if ($resolved -cne $target) { throw "Unexpected resolved path: $resolved" }
    $allowed = $resolved -eq 'D:\FotoArchiveData\backups' -or
        $resolved.StartsWith('D:\FotoArchiveData\reports\', [StringComparison]::OrdinalIgnoreCase) -or
        $resolved.StartsWith('D:\Projects\FotoArchive\dist\FotoArchive-v', [StringComparison]::OrdinalIgnoreCase) -or
        $resolved -eq 'D:\Projects\FotoArchive\dist-staging-v054'
    if (-not $allowed) { throw "Outside cleanup targets: $resolved" }
    if ((Get-Item -LiteralPath $resolved).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Cleanup root cannot be a link: $resolved"
    }
    # Enumerate without following junctions. Detach the link entries themselves
    # before a recursive delete so main models/runtime can never be traversed.
    $pending = [System.Collections.Generic.Stack[string]]::new()
    $pending.Push($resolved)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                if (-not $item.FullName.StartsWith($resolved + '\', [StringComparison]::OrdinalIgnoreCase)) {
                    throw "Link outside target: $($item.FullName)"
                }
                Remove-Item -LiteralPath $item.FullName -Force
                $unlinked.Add($item.FullName)
            } elseif ($item.PSIsContainer) {
                $pending.Push($item.FullName)
            }
        }
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
    $deleted.Add($resolved)
}
$afterFree = (Get-PSDrive D).Free
$result = [ordered]@{
    at = (Get-Date).ToString('o'); free_before = $beforeFree; free_after = $afterFree
    freed_bytes = $afterFree - $beforeFree; deleted = @($deleted); detached_links = @($unlinked)
    preserved = @('D:\FotoArchiveData\catalog.sqlite3', 'D:\FotoArchiveData\models',
        'D:\FotoArchiveData\runtime', 'D:\FotoArchiveData\geonames', 'F:\MyFoto',
        'D:\Projects\FotoArchive\dist\FotoArchive', 'D:\Projects\FotoArchive\dist-v063')
}
$result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath -Encoding utf8
[pscustomobject]@{freed_GiB = [math]::Round(($afterFree-$beforeFree)/1GB, 3); removed_directories = $deleted.Count; detached_links = $unlinked.Count}
