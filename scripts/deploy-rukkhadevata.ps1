<#
.SYNOPSIS
  Deploy this fork to the NAS Rukkhadevata (192.168.50.84), stack root
  /Volume1/apps/shelly-energy-analyzer. See docs/deploy-rukkhadevata.md.

.DESCRIPTION
  1. git archive of -Ref -> src/ on Rukkhadevata.
  2. Build the image shelly-energy-analyzer:<version>-<sha7> there.
  3. Copy data/config.json and an online SQLite copy of data/data/energy.db
     to data/backup-pre-<tag>-<utc time>/.
  4. Set the image in compose.yaml and run `docker compose up -d`.
  5. Wait for http://127.0.0.1:8765/api/version. If it fails, go back to the
     previous image tag.

  -Rollback only goes back to the previous tag (stored in .previous-image).
  Needs the `ssh rukkhadevata` alias. Uses no GitHub Actions.

.EXAMPLE
  & "C:\Repositories\shelly-energy-analyzer\scripts\deploy-rukkhadevata.ps1"
.EXAMPLE
  & "C:\Repositories\shelly-energy-analyzer\scripts\deploy-rukkhadevata.ps1" -Rollback
#>
[CmdletBinding()]
param(
    [string]$Ref = 'origin/throndir2-plug-history',
    [string]$SshHost = 'rukkhadevata',
    [string]$StackRoot = '/Volume1/apps/shelly-energy-analyzer',
    [switch]$Rollback
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repo = Split-Path -Parent $PSScriptRoot

function Invoke-Remote([string]$script) {
    # LF-only text on stdin; docker is not on the non-interactive SSH PATH.
    $prefix = "set -eu`nexport PATH=/Volume1/@apps/DockerEngine/dockerd/bin:`$PATH`ncd $StackRoot`n"
    ($prefix + $script).Replace("`r", '') | ssh $SshHost "tr -d '\r' | sh"
    if ($LASTEXITCODE -ne 0) { throw "Remote step failed (exit $LASTEXITCODE)." }
}

$helpers = @'
health() {
  i=0
  while [ $i -lt 60 ]; do
    if curl -fsS -m 3 http://127.0.0.1:8765/api/version; then echo; return 0; fi
    i=$((i+1)); sleep 2
  done
  return 1
}
set_image() { sed -i "s#^\([[:space:]]*image:[[:space:]]*\).*#\1$1#" compose.yaml; }

'@

if ($Rollback) {
    Invoke-Remote ($helpers + @'
prev=$(cat .previous-image)
echo "Rolling back to $prev"
set_image "$prev"
docker compose up -d
health || { echo "Rollback image is not healthy either"; exit 1; }
'@)
    return
}

Push-Location $repo
try {
    git fetch --quiet origin
    if ($LASTEXITCODE -ne 0) { throw 'git fetch failed.' }
    $sha = (git rev-parse --short=7 "$Ref").Trim()
    $version = ((git show "${Ref}:pyproject.toml") | Select-String -Pattern '^version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
    $tag = "$version-$sha"
    $image = "shelly-energy-analyzer:$tag"
    Write-Host "Deploying $Ref as $image to ${SshHost}:$StackRoot"

    $tar = Join-Path $env:TEMP "shelly-energy-analyzer-$tag.tar"
    git archive --format=tar -o $tar "$Ref"
    if ($LASTEXITCODE -ne 0) { throw 'git archive failed.' }
    Invoke-Remote 'test -f compose.yaml || { echo "compose.yaml is missing in the stack root"; exit 1; }'
    scp -q $tar "${SshHost}:$StackRoot/src.tar"
    if ($LASTEXITCODE -ne 0) { throw 'scp failed.' }
    Remove-Item $tar
} finally { Pop-Location }

Invoke-Remote ($helpers + "image='$image'`ntag='$tag'`n" + @'
rm -rf src.new && mkdir src.new && tar -xf src.tar -C src.new && rm src.tar
rm -rf src.old; if [ -d src ]; then mv src src.old; fi; mv src.new src; rm -rf src.old
docker build -q --build-arg TARGETARCH=amd64 -t "$image" src

prev=$(sed -n 's#^[[:space:]]*image:[[:space:]]*##p' compose.yaml | head -1)
if [ "$prev" != "$image" ]; then echo "$prev" > .previous-image; fi

bk="data/backup-pre-$tag-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$bk"
if [ -f data/config.json ]; then cp -p data/config.json "$bk/"; fi
if [ -f data/data/energy.db ]; then
  python3 -c "import sqlite3,sys;s=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True,timeout=300);d=sqlite3.connect(sys.argv[2]);s.backup(d);d.close();s.close()" data/data/energy.db "$bk/energy.db"
fi
echo "Backup: $bk"

set_image "$image"
docker compose up -d
if health; then
  echo "Deployed $image"
else
  echo "Health check failed; rolling back to $prev"
  set_image "$prev"
  docker compose up -d
  health || echo "Rollback image is not healthy either"
  exit 1
fi
'@)
