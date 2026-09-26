# Publish the current master tree to the public repository as one clean commit.
#
#   scripts\publish.ps1                         # dry run: checks + diff preview
#   scripts\publish.ps1 -Push                   # snapshot commit + push
#   scripts\publish.ps1 -Tag v0.1.1 -Push       # ... and tag the release
#   scripts\publish.ps1 -Message "..." -Push    # custom commit subject
#
# Model
# -----
#   master  = the real history. Every commit, private experiments included.
#   public  = a SNAPSHOT branch. Its history is exactly what the public repo
#             sees: the first publish is an orphan root commit, every later
#             publish is a normal child of the previous one whose tree equals
#             master's. So the public repo gets a clean, linear, incremental
#             history while nothing private ever leaves this machine.
#
#   Invariant: after every publish, `git diff public master` is empty.
#
# Rules this script enforces
# --------------------------
#   * working tree must be clean
#   * master must not contain keys, tokens, private keys or absolute user
#     paths (six patterns, all currently zero-hit on this repo)
#   * the snapshot branch must not be checked out (never edit it by hand --
#     the next publish would silently revert your edit)
#   * only ONE ref ever leaves the machine: <Branch>:<TargetBranch>. No
#     --mirror, no --all: this repo also carries internal refs
#     (refs/dsh-turn-rewind/**) that must never be published.
#   * -Force exists for exactly one job: repairing history that was published
#     before anyone could consume it (a re-root, or a tag whose snapshot could
#     not build). It pushes with --force-with-lease, so it still refuses when
#     the remote moved behind your back, and it must never appear in a routine
#     publish.
#
# Settled policies
# ----------------
#   * the public repo grows by ONE snapshot commit per publish; the private
#     history is never rewritten and never published
#   * the public repo's CI builds the release: pushing the tag starts
#     .github/workflows/release.yml, which builds and publishes the Release
#     over there. Do NOT also upload the locally built dist\ installer --
#     two artifacts from two sources is worse than either one.
#   * the release body is docs/release-notes/<tag>.md, written BEFORE tagging
#     (Chinese first, then an English section); CI prepends it to GitHub's
#     generated commit list. Missing file = generated notes only, silently.
#   * back the private remote up with BRANCHES, not tags: a tag there would
#     start a second Actions build and spend private-repo minutes
#
# One-time setup
# --------------
#   git remote add private <private-repo-url>
#   git remote add public  <public-repo-url>
#   git config remote.pushDefault private   # a bare `git push` reaches only private
#
# GitHub is unreachable on this machine without the proxy; if the push fails
# with a timeout, start FlClash (or whatever the proxy is) and retry.

[CmdletBinding()]
param(
    [switch]$Push,
    [string]$Message = "",
    [string]$Remote = "public",
    [string]$Branch = "public",
    [string]$TargetBranch = "main",
    [string]$Tag = "",
    [string]$PrivateRemote = "private",
    # One-time history repair only: re-rooting, or replacing a snapshot whose
    # tag could not build. Uses --force-with-lease, so it still refuses if the
    # remote moved since the last fetch. Never for a routine publish.
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Fail([string]$Text) {
    Write-Host ""
    Write-Host "FAIL: $Text" -ForegroundColor Red
    exit 1
}
function Step([string]$Text) { Write-Host "==> $Text" }

# Native git writes its push progress to STDERR. When the caller redirects
# that stream (2>&1, *> log.txt, a CI capture) PowerShell turns each line into
# an ErrorRecord, and $ErrorActionPreference = "Stop" makes the first one
# terminating: this script died right after a `git push` that had in fact
# succeeded, so the tag never went out while the run looked failed. Scope the
# preference back to Continue for those calls and judge by $LASTEXITCODE, the
# way every other call here already does.
function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$GitArgs)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & git @GitArgs } finally { $ErrorActionPreference = $previous }
}

$Root = (& git rev-parse --show-toplevel 2>$null)
if (-not $Root) { Fail "not inside a git repository" }
Set-Location $Root

# ---------------------------------------------------------------- 1. clean tree
Step "working tree"
$dirty = & git status --porcelain
if ($dirty) {
    Write-Host ($dirty -join "`n")
    Fail "uncommitted changes -- commit or stash them before publishing"
}
Write-Host "    clean"

# --------------------------------------------------------------- 2. version
Step "version"
$confText = (& git show "master:app/src-tauri/tauri.conf.json") -join "`n"
$version = ($confText | ConvertFrom-Json).version
if (-not $version) { Fail "could not read the version from master's tauri.conf.json" }
if (-not $Message) { $Message = "mai2srt v$version" }
Write-Host "    $version"

# ------------------------------------------------------------ 3. secret scan
Step "scanning master's tracked files for secrets and private paths"
$patterns = @(
    'sk-[A-Za-z0-9_-]{16,}',
    '(api[_-]?key|apikey|secret|passwd|password|token)\s*[:=]\s*["''][^"'']{12,}["'']',
    'BEGIN [A-Z ]*PRIVATE KEY',
    'C:\\Users\\[A-Za-z0-9]',
    'E:\\github',
    'hub\.oaifree'
)
$hits = @()
foreach ($pattern in $patterns) {
    $found = & git grep -nIE -e $pattern master
    if ($LASTEXITCODE -eq 0 -and $found) { $hits += $found }
}
if ($hits.Count -gt 0) {
    Write-Host ($hits -join "`n")
    Fail "the scan above matched; fix or remove those lines before publishing"
}
Write-Host "    $($patterns.Count) patterns, no hits"

# ------------------------------------------------------- 4. what would ship
Step "current public tip"
$publicTip = (& git rev-parse --verify --quiet "refs/heads/$Branch")
$hasBranch = ($LASTEXITCODE -eq 0 -and $publicTip)
if ($hasBranch) { Write-Host "    $Branch = $($publicTip.Substring(0,7))" }
else { Write-Host "    no '$Branch' branch yet -- this will be the first (root) publish" }

$head = (& git rev-parse --abbrev-ref HEAD).Trim()
if ($head -eq $Branch) {
    Fail "'$Branch' is checked out. It is a snapshot branch -- check out master and let this script write it"
}

$masterTree = (& git rev-parse "master^{tree}").Trim()
$files = @(& git ls-tree -r --name-only master)

Step "change set (public -> master)"
$nothingToPublish = $false
if ($hasBranch) {
    if ((& git rev-parse "$Branch^{tree}") -eq $masterTree) {
        $nothingToPublish = $true
        Write-Host "    nothing to publish: $Branch already has master's tree"
    } else {
        & git --no-pager diff --stat $Branch master | Out-Host
    }
} else {
    Write-Host "    $($files.Count) files will become the single root commit"
}

# list remotes instead of get-url: a missing remote makes `get-url` write to
# stderr, and with ErrorActionPreference=Stop a redirected stderr record turns
# into a terminating error that would swallow the message below
$remotes = @(& git remote)
if ($remotes -notcontains $Remote) {
    Fail "remote '$Remote' is not configured. One-time setup:`n  git remote add $Remote <public-repo-url>"
}
$remoteUrl = (& git remote get-url $Remote)
Step "target"
Write-Host "    $Remote -> $remoteUrl"
Write-Host "    push ref: $Branch`:$TargetBranch"
Write-Host "    message : $Message"

if (-not $Push) {
    Write-Host ""
    Write-Host "DRY RUN -- nothing was written. Re-run with -Push to do it." -ForegroundColor Yellow
    Write-Host "It would: create a $(if ($hasBranch) { 'child' } else { 'root' }) commit of master's tree on '$Branch',"
    Write-Host "         then push only '$Branch`:$TargetBranch'$(if ($Tag) { " plus tag $Tag" })."
    exit 0
}

# ------------------------------------------------------------- 5. snapshot
if (-not $nothingToPublish) {
    Step "creating the snapshot commit"
    $ctArgs = @($masterTree)
    if ($hasBranch) { $ctArgs += @("-p", $publicTip) }
    $ctArgs += @("-m", $Message)
    $new = (& git commit-tree @ctArgs | Select-Object -First 1)
    if (-not $new) { $new = "" }
    $new = $new.Trim()
    if ($new -notmatch '^[0-9a-f]{40}$') { Fail "git commit-tree returned nothing usable: [$new]" }
    # History length is the guard against ever moving the snapshot branch onto
    # something that is not "previous snapshot + one commit". Compare INTEGERS:
    # `git rev-list --count` comes back as a string, and PowerShell's `+` with a
    # string on the left concatenates ("1" + 1 = "11"), which made every publish
    # after the root one fail this check.
    $actualLength = [int](& git rev-list --count $new)
    $expectedLength = if ($hasBranch) { [int](& git rev-list --count $publicTip) + 1 } else { 1 }
    if ($actualLength -ne $expectedLength) {
        Fail "refusing to move '$Branch': the new commit's history is $actualLength commits, expected $expectedLength"
    }
    & git branch -f $Branch $new
    if ($LASTEXITCODE -ne 0) { Fail "git branch -f $Branch failed" }
    $publicTip = $new
    Write-Host "    $Branch = $($new.Substring(0,7)) $Message"
    & git diff --quiet $Branch master
    if ($LASTEXITCODE -ne 0) {
        Fail "the snapshot tree does not match master -- aborting before any push"
    }
    Write-Host "    tree matches master"
}

# ---------------------------------------------------------------- 6. push
Step "pushing (only this one ref)"
if ($Force) {
    Write-Host "    --force-with-lease: replacing the remote branch (history repair)" -ForegroundColor Yellow
    Invoke-Git push --force-with-lease $Remote "${Branch}:${TargetBranch}"
} else {
    Invoke-Git push $Remote "${Branch}:${TargetBranch}"
}
if ($LASTEXITCODE -ne 0) { Fail "push failed -- if this is a timeout, github is blocked without the proxy" }

if ($Tag) {
    Step "tagging $Tag"
    & git rev-parse --verify --quiet "refs/tags/$Tag" | Out-Null
    if ($LASTEXITCODE -eq 0) { Fail "tag $Tag already exists locally; pick another name" }
    & git tag -a $Tag -m "mai2srt v$version" $Branch
    if ($LASTEXITCODE -ne 0) { Fail "git tag failed" }
    Invoke-Git push $Remote "refs/tags/$Tag"
    if ($LASTEXITCODE -ne 0) { Fail "tag push failed" }
    Write-Host "    $Tag pushed -- CI on '$Remote' now builds and publishes the Release"
    Write-Host "    (do not upload the local dist\ installer as well)"
}

# ------------------------------------------------------------- 7. verify
Step "verifying the remote"
$remoteSha = ((Invoke-Git ls-remote $Remote "refs/heads/$TargetBranch") -split '\s+')[0]
if ($remoteSha -ne $publicTip) {
    Fail "remote $TargetBranch is $remoteSha but local $Branch is $publicTip"
}
Write-Host "    remote $TargetBranch = $($remoteSha.Substring(0,7)) -- matches"

Write-Host ""
Write-Host "Done. Public history:" -ForegroundColor Green
& git --no-pager log --oneline -5 $Branch | Out-Host
Write-Host ""
Write-Host "The private history is untouched. Back it up with branches (not tags):"
Write-Host "    git push $PrivateRemote master $Branch"
