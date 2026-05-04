param(
    [string]$ProjectId    = "optimum-web-487816-v4",
    [string]$Region       = "europe-west1",
    [string]$ServiceName  = "sre-demo-service",
    [string]$RegistryName = "sre-demo"
)

$ImageTag = "$Region-docker.pkg.dev/$ProjectId/$RegistryName/${ServiceName}:latest"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

Write-Host "--- SRE Demo Service Deploy ---" -ForegroundColor Cyan
Write-Host "Project : $ProjectId"
Write-Host "Region  : $Region"
Write-Host "Image   : $ImageTag"

# 0. Set project
Write-Host "[0/7] Setting active project..." -ForegroundColor Yellow
gcloud config set project $ProjectId

# 1. Enable APIs
Write-Host "[1/7] Enabling APIs..." -ForegroundColor Yellow
gcloud services enable run.googleapis.com cloudbuild.googleapis.com cloudscheduler.googleapis.com monitoring.googleapis.com logging.googleapis.com artifactregistry.googleapis.com cloudtrace.googleapis.com --project $ProjectId
Write-Host "  APIs enabled (incl. Cloud Trace)." -ForegroundColor Green

# 2. Artifact Registry
Write-Host "[2/7] Artifact Registry '$RegistryName'..." -ForegroundColor Yellow
$null = gcloud artifacts repositories describe $RegistryName --location $Region --project $ProjectId 2>&1
if ($LASTEXITCODE -ne 0) {
    gcloud artifacts repositories create $RegistryName --repository-format docker --location $Region --project $ProjectId --description "SRE demo images"
    Write-Host "  Repository created." -ForegroundColor Green
} else {
    Write-Host "  Repository already exists." -ForegroundColor DarkGray
}

# 3. Build with Cloud Build (no local Docker needed)
Write-Host "[3/7] Building image via Cloud Build..." -ForegroundColor Yellow
gcloud builds submit $scriptDir --tag $ImageTag --project $ProjectId
if ($LASTEXITCODE -ne 0) { Write-Host "Cloud Build failed!" -ForegroundColor Red; exit 1 }
Write-Host "  Image built: $ImageTag" -ForegroundColor Green

# 4. Cloud Run deploy
Write-Host "[4/7] Deploying to Cloud Run..." -ForegroundColor Yellow
gcloud run deploy $ServiceName --image $ImageTag --platform managed --region $Region --project $ProjectId --allow-unauthenticated --min-instances 0 --max-instances 3 --memory 256Mi --cpu 1 --timeout 120 --set-env-vars "GOOGLE_CLOUD_PROJECT=$ProjectId" --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "Cloud Run deploy failed!" -ForegroundColor Red; exit 1 }

# Grant Cloud Trace write permission to the Cloud Run service account
$ProjectNumber = gcloud projects describe $ProjectId --format "value(projectNumber)"
$DefaultSA = "${ProjectNumber}-compute@developer.gserviceaccount.com"
Write-Host "  Granting cloudtrace.agent to $DefaultSA..." -ForegroundColor DarkGray
gcloud projects add-iam-policy-binding $ProjectId --member "serviceAccount:$DefaultSA" --role "roles/cloudtrace.agent" --condition None --quiet 2>&1 | Out-Null
Write-Host "  IAM: cloudtrace.agent granted." -ForegroundColor Green

$ServiceUrl = gcloud run services describe $ServiceName --region $Region --project $ProjectId --format "value(status.url)"
Write-Host "  Service URL: $ServiceUrl" -ForegroundColor Green

# 5. Cloud Scheduler
Write-Host "[5/7] Cloud Scheduler jobs..." -ForegroundColor Yellow
$null = gcloud scheduler jobs delete sre-demo-health-poller --location $Region --project $ProjectId --quiet 2>&1
$null = gcloud scheduler jobs delete sre-demo-chaos         --location $Region --project $ProjectId --quiet 2>&1
$null = gcloud scheduler jobs delete sre-demo-error-burst   --location $Region --project $ProjectId --quiet 2>&1

gcloud scheduler jobs create http sre-demo-health-poller --location $Region --project $ProjectId --schedule "*/5 * * * *"  --uri "${ServiceUrl}/health" --http-method GET  --attempt-deadline 30s  --description "Health baseline"
gcloud scheduler jobs create http sre-demo-chaos         --location $Region --project $ProjectId --schedule "*/10 * * * *" --uri "${ServiceUrl}/chaos"  --http-method GET  --attempt-deadline 120s --description "Chaos injector"
gcloud scheduler jobs create http sre-demo-error-burst   --location $Region --project $ProjectId --schedule "*/30 * * * *" --uri "${ServiceUrl}/error"  --http-method GET  --attempt-deadline 30s  --description "Guaranteed 500"
Write-Host "  3 Scheduler jobs created." -ForegroundColor Green

# 6. Uptime check
Write-Host "[6/7] Uptime Check..." -ForegroundColor Yellow
$svcHost = $ServiceUrl -replace "https://",""
$uptimeJson = "{`"displayName`":`"sre-demo uptime`",`"httpCheck`":{`"path`":`"/health`",`"port`":443,`"useSsl`":true},`"monitoredResource`":{`"type`":`"uptime_url`",`"labels`":{`"project_id`":`"$ProjectId`",`"host`":`"$svcHost`"}},`"period`":`"60s`",`"timeout`":`"10s`"}"
$uptimeFile = "$env:TEMP\uptime.json"
[System.IO.File]::WriteAllText($uptimeFile, $uptimeJson)
$null = gcloud monitoring uptime create --display-name "sre-demo uptime" --config-from-file $uptimeFile --project $ProjectId 2>&1
if ($LASTEXITCODE -eq 0) { Write-Host "  Uptime check created." -ForegroundColor Green } else { Write-Host "  Uptime: create manually in Console." -ForegroundColor DarkYellow }

# 7. Alert policy
Write-Host "[7/7] Alerting Policy..." -ForegroundColor Yellow
$alertJson = "{`"displayName`":`"sre-demo 5xx>5%`",`"combiner`":`"OR`",`"conditions`":[{`"displayName`":`"5xx rate`",`"conditionThreshold`":{`"filter`":`"resource.type=\`"cloud_run_revision\`" AND resource.labels.service_name=\`"$ServiceName\`" AND metric.type=\`"run.googleapis.com/request_count\`" AND metric.labels.response_code_class=\`"5xx\`"`",`"aggregations`":[{`"alignmentPeriod`":`"300s`",`"perSeriesAligner`":`"ALIGN_RATE`",`"crossSeriesReducer`":`"REDUCE_SUM`",`"groupByFields`":[`"resource.labels.service_name`"]}],`"comparison`":`"COMPARISON_GT`",`"thresholdValue`":0.05,`"duration`":`"0s`",`"trigger`":{`"count`":1}}}],`"alertStrategy`":{`"autoClose`":`"1800s`"}}"
$alertFile = "$env:TEMP\alert.json"
[System.IO.File]::WriteAllText($alertFile, $alertJson)
$null = gcloud alpha monitoring policies create --policy-from-file $alertFile --project $ProjectId 2>&1
if ($LASTEXITCODE -eq 0) { Write-Host "  Alert policy created." -ForegroundColor Green } else { Write-Host "  Alert: create manually in Console -> Monitoring -> Alerting." -ForegroundColor DarkYellow }

Write-Host ""
Write-Host "--- Deploy complete! ---" -ForegroundColor Green
Write-Host "Service URL : $ServiceUrl"
Write-Host "Log Explorer: https://console.cloud.google.com/logs?project=$ProjectId"
Write-Host "Cloud Trace : https://console.cloud.google.com/traces?project=$ProjectId"
Write-Host "Monitoring  : https://console.cloud.google.com/monitoring?project=$ProjectId"
Write-Host "Test : Invoke-WebRequest ${ServiceUrl}/error"