[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[a-z][a-z0-9-]{4,28}[a-z0-9]$")]
    [string]$ProjectId,

    [Parameter(Mandatory = $true)]
    [ValidateScript({ [Uri]::IsWellFormedUriString($_, [UriKind]::Absolute) })]
    [string]$SupabaseUrl,

    [Parameter(Mandatory = $true)]
    [ValidateScript({ [Uri]::IsWellFormedUriString($_, [UriKind]::Absolute) -and -not $_.Contains(",") })]
    [string]$AllowedOrigin,

    [Parameter(Mandatory = $true)]
    [string]$EmailFrom,

    [ValidatePattern("^[a-z]+-[a-z]+[0-9]$")]
    [string]$Region = "asia-south1",

    [ValidatePattern("^[a-z][a-z0-9-]{0,62}$")]
    [string]$Repository = "cv-job-matcher",

    [ValidatePattern("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
    [string]$ImageTag = "manual",

    [string]$ApiService = "cvjm-api",
    [string]$CrawlerJob = "cvjm-crawler",
    [string]$MatcherJob = "cvjm-matcher",
    [string]$CrawlerSchedule = "*/10 * * * *",
    [string]$QueueRecoverySchedule = "*/2 * * * *",
    [string]$SchedulerTimeZone = "Asia/Colombo",

    [ValidatePattern("^[1-9][0-9]*$")]
    [string]$SecretVersion = "1",

    [string]$DatabaseSecret = "cvjm-database-url",
    [string]$SupabaseServiceRoleSecret = "cvjm-supabase-service-role-key",
    [string]$GroqSecret = "cvjm-groq-api-key",
    [string]$ResendSecret = "cvjm-resend-api-key",

    [switch]$PublicApi,
    [switch]$SkipBuild,
    [switch]$SkipScheduler,
    [switch]$PlanOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($EmailFrom.Contains(",")) {
    throw "EmailFrom cannot contain a comma because Cloud Run env mappings use comma delimiters."
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$apiIdentityId = "cvjm-api-runtime"
$crawlerIdentityId = "cvjm-crawl-runtime"
$matcherIdentityId = "cvjm-match-runtime"
$schedulerIdentityId = "cvjm-scheduler"
$apiIdentity = "$apiIdentityId@$ProjectId.iam.gserviceaccount.com"
$crawlerIdentity = "$crawlerIdentityId@$ProjectId.iam.gserviceaccount.com"
$matcherIdentity = "$matcherIdentityId@$ProjectId.iam.gserviceaccount.com"
$schedulerIdentity = "$schedulerIdentityId@$ProjectId.iam.gserviceaccount.com"

$registry = "$Region-docker.pkg.dev/$ProjectId/$Repository"
$apiImage = "$registry/api`:$ImageTag"
$crawlerImage = "$registry/crawler`:$ImageTag"
$matcherImage = "$registry/matcher`:$ImageTag"

function Format-Command {
    param([string[]]$Arguments)
    return "gcloud " + (($Arguments | ForEach-Object {
        if ($_ -match "[\s<>]") {
            return "'" + $_.Replace("'", "''") + "'"
        }
        return $_
    }) -join " ")
}

function Invoke-Gcloud {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    Write-Host (Format-Command $Arguments)
    if ($PlanOnly) {
        return
    }

    & gcloud @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "gcloud command failed with exit code $LASTEXITCODE."
    }
}

function Test-GcloudResource {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    if ($PlanOnly) {
        return $false
    }
    & gcloud @Arguments 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}

function Ensure-ServiceAccount {
    param(
        [Parameter(Mandatory = $true)][string]$AccountId,
        [Parameter(Mandatory = $true)][string]$DisplayName
    )

    $describe = @(
        "iam", "service-accounts", "describe",
        "$AccountId@$ProjectId.iam.gserviceaccount.com",
        "--project=$ProjectId"
    )
    if (-not (Test-GcloudResource $describe)) {
        Invoke-Gcloud @(
            "iam", "service-accounts", "create", $AccountId,
            "--display-name=$DisplayName",
            "--project=$ProjectId",
            "--quiet"
        )
    }
}

function Assert-SecretVersion {
    param([Parameter(Mandatory = $true)][string]$SecretName)

    $describe = @(
        "secrets", "versions", "describe", $SecretVersion,
        "--secret=$SecretName",
        "--project=$ProjectId"
    )
    if ($PlanOnly) {
        Write-Host "Would verify Secret Manager value: $SecretName version $SecretVersion"
        return
    }
    if (-not (Test-GcloudResource $describe)) {
        throw @"
Required secret '$SecretName' version '$SecretVersion' was not found.
Create it in Google Cloud Console > Security > Secret Manager, then rerun.
Do not put the value in this script, Git, a shell argument, or chat.
"@
    }
}

function Grant-SecretAccess {
    param(
        [Parameter(Mandatory = $true)][string]$SecretName,
        [Parameter(Mandatory = $true)][string]$ServiceAccount
    )

    Invoke-Gcloud @(
        "secrets", "add-iam-policy-binding", $SecretName,
        "--member=serviceAccount:$ServiceAccount",
        "--role=roles/secretmanager.secretAccessor",
        "--project=$ProjectId",
        "--quiet"
    )
}

function Deploy-CloudRunJob {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Image,
        [Parameter(Mandatory = $true)][string]$ServiceAccount,
        [Parameter(Mandatory = $true)][string]$Environment,
        [Parameter(Mandatory = $true)][string]$Secrets,
        [Parameter(Mandatory = $true)][string]$Timeout,
        [Parameter(Mandatory = $true)][string]$Memory,
        [Parameter(Mandatory = $true)][string]$Cpu
    )

    $verb = if (Test-GcloudResource @(
        "run", "jobs", "describe", $Name,
        "--region=$Region",
        "--project=$ProjectId"
    )) { "update" } else { "create" }

    Invoke-Gcloud @(
        "run", "jobs", $verb, $Name,
        "--image=$Image",
        "--region=$Region",
        "--project=$ProjectId",
        "--service-account=$ServiceAccount",
        "--tasks=1",
        "--parallelism=1",
        "--max-retries=1",
        "--task-timeout=$Timeout",
        "--cpu=$Cpu",
        "--memory=$Memory",
        "--set-env-vars=$Environment",
        "--set-secrets=$Secrets",
        "--labels=component=$Name,managed-by=cvjm-script",
        "--quiet"
    )
}

function Ensure-SchedulerJob {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$TargetJob,
        [Parameter(Mandatory = $true)][string]$Schedule,
        [Parameter(Mandatory = $true)][string]$Description
    )

    $verb = if (Test-GcloudResource @(
        "scheduler", "jobs", "describe", $Name,
        "--location=$Region",
        "--project=$ProjectId"
    )) { "update" } else { "create" }
    $uri = "https://run.googleapis.com/v2/projects/$ProjectId/locations/$Region/jobs/${TargetJob}:run"

    Invoke-Gcloud @(
        "scheduler", "jobs", $verb, "http", $Name,
        "--location=$Region",
        "--project=$ProjectId",
        "--schedule=$Schedule",
        "--time-zone=$SchedulerTimeZone",
        "--uri=$uri",
        "--http-method=POST",
        "--oauth-service-account-email=$schedulerIdentity",
        "--oauth-token-scope=https://www.googleapis.com/auth/cloud-platform",
        "--attempt-deadline=30s",
        "--max-retry-attempts=3",
        "--min-backoff=30s",
        "--max-backoff=300s",
        "--description=$Description",
        "--quiet"
    )
}

if (-not $PlanOnly -and -not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    throw "Google Cloud CLI (gcloud) is not installed or is not on PATH."
}

Push-Location $repoRoot
try {
    Invoke-Gcloud @(
        "services", "enable",
        "artifactregistry.googleapis.com",
        "cloudbuild.googleapis.com",
        "run.googleapis.com",
        "secretmanager.googleapis.com",
        "cloudscheduler.googleapis.com",
        "--project=$ProjectId",
        "--quiet"
    )

    if (-not (Test-GcloudResource @(
        "artifacts", "repositories", "describe", $Repository,
        "--location=$Region",
        "--project=$ProjectId"
    ))) {
        Invoke-Gcloud @(
            "artifacts", "repositories", "create", $Repository,
            "--repository-format=docker",
            "--location=$Region",
            "--description=CV Job Matcher service images",
            "--project=$ProjectId",
            "--quiet"
        )
    }

    Ensure-ServiceAccount $apiIdentityId "CVJM API runtime"
    Ensure-ServiceAccount $crawlerIdentityId "CVJM crawler runtime"
    Ensure-ServiceAccount $matcherIdentityId "CVJM matcher runtime"
    Ensure-ServiceAccount $schedulerIdentityId "CVJM Scheduler invoker"

    foreach ($secret in @(
        $DatabaseSecret,
        $SupabaseServiceRoleSecret,
        $GroqSecret,
        $ResendSecret
    )) {
        Assert-SecretVersion $secret
    }

    foreach ($identity in @($apiIdentity, $crawlerIdentity, $matcherIdentity)) {
        Grant-SecretAccess $DatabaseSecret $identity
    }
    foreach ($identity in @($apiIdentity, $matcherIdentity)) {
        Grant-SecretAccess $SupabaseServiceRoleSecret $identity
    }
    Grant-SecretAccess $GroqSecret $matcherIdentity
    Grant-SecretAccess $ResendSecret $matcherIdentity

    $optionalCrawlerSecrets = @(
        @{ Environment = "ADZUNA_APP_ID"; Name = "cvjm-adzuna-app-id" },
        @{ Environment = "ADZUNA_APP_KEY"; Name = "cvjm-adzuna-app-key" },
        @{ Environment = "GOOGLE_CSE_API_KEY"; Name = "cvjm-google-cse-api-key" },
        @{ Environment = "GOOGLE_CSE_ID"; Name = "cvjm-google-cse-id" },
        @{ Environment = "SERPAPI_API_KEY"; Name = "cvjm-serpapi-api-key" }
    )
    $crawlerOptionalRefs = @()
    if (-not $PlanOnly) {
        foreach ($optional in $optionalCrawlerSecrets) {
            if (Test-GcloudResource @(
                "secrets", "versions", "describe", $SecretVersion,
                "--secret=$($optional.Name)",
                "--project=$ProjectId"
            )) {
                Grant-SecretAccess $optional.Name $crawlerIdentity
                $crawlerOptionalRefs += "$($optional.Environment)=$($optional.Name):$SecretVersion"
            }
            else {
                Write-Host "Optional source secret not configured: $($optional.Name)"
            }
        }
    }

    if (-not $SkipBuild) {
        Invoke-Gcloud @(
            "builds", "submit", ".",
            "--config=deploy/cloudbuild.yaml",
            "--substitutions=_REGION=$Region,_REPOSITORY=$Repository,_TAG=$ImageTag",
            "--project=$ProjectId",
            "--quiet"
        )
    }

    $apiEnvironment = @(
        "GOOGLE_CLOUD_PROJECT=$ProjectId",
        "CLOUD_RUN_REGION=$Region",
        "MATCHER_JOB_NAME=$MatcherJob",
        "SUPABASE_URL=$SupabaseUrl",
        "CV_BUCKET=cv-uploads",
        "RESULT_BUCKET=job-results",
        "CV_RETENTION_HOURS=24",
        "RESULT_RETENTION_HOURS=168",
        "TASK_LEASE_SECONDS=900",
        "SOURCE_LEASE_SECONDS=2700",
        "REQUIRE_AUTH=1",
        "ALLOWED_ORIGINS=$AllowedOrigin",
        "MAX_CV_UPLOAD_MB=10",
        "API_GUNICORN_WORKERS=1",
        "API_GUNICORN_THREADS=4",
        "API_REQUEST_TIMEOUT_SECONDS=120"
    ) -join ","
    $apiSecrets = @(
        "DATABASE_URL=${DatabaseSecret}:$SecretVersion",
        "SUPABASE_SERVICE_ROLE_KEY=${SupabaseServiceRoleSecret}:$SecretVersion"
    ) -join ","
    $apiAccessFlag = if ($PublicApi) {
        "--allow-unauthenticated"
    }
    else {
        "--no-allow-unauthenticated"
    }

    Invoke-Gcloud @(
        "run", "deploy", $ApiService,
        "--image=$apiImage",
        "--region=$Region",
        "--project=$ProjectId",
        "--platform=managed",
        "--execution-environment=gen2",
        "--service-account=$apiIdentity",
        "--port=8080",
        "--cpu=1",
        "--memory=1Gi",
        "--concurrency=20",
        "--min-instances=0",
        "--max-instances=5",
        "--timeout=120s",
        "--set-env-vars=$apiEnvironment",
        "--set-secrets=$apiSecrets",
        "--labels=component=api,managed-by=cvjm-script",
        "--ingress=all",
        $apiAccessFlag,
        "--quiet"
    )

    $crawlerEnvironment = @(
        "GOOGLE_CLOUD_PROJECT=$ProjectId",
        "CLOUD_RUN_REGION=$Region",
        "SUPABASE_URL=$SupabaseUrl",
        "INVENTORY_COUNTRY=Sri Lanka",
        "SOURCE_LEASE_SECONDS=2700",
        "SOURCE_AGENT_WORKERS=8",
        "SOURCE_RESULT_LIMIT=5000",
        "SOURCE_INVENTORY_CACHE_MINUTES=0",
        "DISCOVERY_RESULT_CACHE_MINUTES=0",
        "CRAWL4AI_ENABLED=1",
        "WEB_DISCOVERY_MAX_QUERIES_PER_SOURCE=4",
        "WEB_DISCOVERY_MAX_DETAIL_PAGES_PER_SOURCE=30",
        "WEB_DISCOVERY_DETAIL_WORKERS=6",
        "LOG_LEVEL=INFO"
    ) -join ","
    $crawlerSecretRefs = @("DATABASE_URL=${DatabaseSecret}:$SecretVersion")
    $crawlerSecretRefs += $crawlerOptionalRefs
    Deploy-CloudRunJob `
        -Name $CrawlerJob `
        -Image $crawlerImage `
        -ServiceAccount $crawlerIdentity `
        -Environment $crawlerEnvironment `
        -Secrets ($crawlerSecretRefs -join ",") `
        -Timeout "45m" `
        -Memory "2Gi" `
        -Cpu "2"

    $matcherEnvironment = @(
        "GOOGLE_CLOUD_PROJECT=$ProjectId",
        "CLOUD_RUN_REGION=$Region",
        "SUPABASE_URL=$SupabaseUrl",
        "CV_BUCKET=cv-uploads",
        "RESULT_BUCKET=job-results",
        "CV_RETENTION_HOURS=24",
        "RESULT_RETENTION_HOURS=168",
        "TASK_LEASE_SECONDS=900",
        "TASK_MAX_ATTEMPTS=3",
        "TASK_RETRY_DELAY_SECONDS=30",
        "EMAIL_PROVIDER=resend",
        "EMAIL_FROM=$EmailFrom",
        "LLM_PROVIDER=groq",
        "GROQ_MODEL=openai/gpt-oss-20b",
        "LLM_LIMIT=500",
        "LLM_BATCH_SIZE=5",
        "OLLAMA_FALLBACK_ENABLED=0",
        "LOG_LEVEL=INFO"
    ) -join ","
    $matcherSecrets = @(
        "DATABASE_URL=${DatabaseSecret}:$SecretVersion",
        "SUPABASE_SERVICE_ROLE_KEY=${SupabaseServiceRoleSecret}:$SecretVersion",
        "GROQ_API_KEY=${GroqSecret}:$SecretVersion",
        "RESEND_API_KEY=${ResendSecret}:$SecretVersion"
    ) -join ","
    Deploy-CloudRunJob `
        -Name $MatcherJob `
        -Image $matcherImage `
        -ServiceAccount $matcherIdentity `
        -Environment $matcherEnvironment `
        -Secrets $matcherSecrets `
        -Timeout "60m" `
        -Memory "2Gi" `
        -Cpu "2"

    Invoke-Gcloud @(
        "run", "jobs", "add-iam-policy-binding", $MatcherJob,
        "--member=serviceAccount:$apiIdentity",
        "--role=roles/run.invoker",
        "--region=$Region",
        "--project=$ProjectId",
        "--quiet"
    )
    foreach ($job in @($CrawlerJob, $MatcherJob)) {
        Invoke-Gcloud @(
            "run", "jobs", "add-iam-policy-binding", $job,
            "--member=serviceAccount:$schedulerIdentity",
            "--role=roles/run.invoker",
            "--region=$Region",
            "--project=$ProjectId",
            "--quiet"
        )
    }

    if (-not $SkipScheduler) {
        Ensure-SchedulerJob `
            -Name "cvjm-inventory-refresh" `
            -TargetJob $CrawlerJob `
            -Schedule $CrawlerSchedule `
            -Description "Refresh the shared Sri Lankan job inventory"
        Ensure-SchedulerJob `
            -Name "cvjm-queue-recovery" `
            -TargetJob $MatcherJob `
            -Schedule $QueueRecoverySchedule `
            -Description "Claim queued or expired matching tasks not dispatched by the API"
    }

    if ($PlanOnly) {
        Write-Host "Plan complete. No Google Cloud resources were changed."
    }
    else {
        $apiUrl = & gcloud run services describe $ApiService `
            "--region=$Region" `
            "--project=$ProjectId" `
            "--format=value(status.url)"
        if ($LASTEXITCODE -ne 0) {
            throw "Deployment succeeded, but reading the API URL failed."
        }
        Write-Host ""
        Write-Host "Cloud deployment complete."
        Write-Host "API URL: $apiUrl"
        if (-not $PublicApi) {
            Write-Host "The API is private. Rerun with -PublicApi only after bearer-token authentication is verified."
        }
        Write-Host "Set Netlify VITE_API_BASE_URL to the API URL."
    }
}
finally {
    Pop-Location
}
