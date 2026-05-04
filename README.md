# sre-demo-service

Serviço Cloud Run sintético que gera sinais SRE reais (logs, métricas, latência, erros) para testar o SRE AI Triage Agent.

## Estrutura

```
infra/sre-demo-service/
├── main.py           # Flask service com endpoints de falha
├── requirements.txt  # flask + gunicorn
├── Dockerfile        # Python 3.12-slim + gunicorn
├── deploy.ps1        # Deploy completo (Artifact Registry → Cloud Run → Scheduler → Alerts)
└── smoke-test.ps1    # Teste rápido de todos os endpoints
```

## Deploy rápido

```powershell
cd infra\sre-demo-service

# Deploy completo (~5 min)
.\deploy.ps1 -ProjectId "optimum-web-487816-v4" -Region "europe-west1"
```

O script faz automaticamente:
1. **Habilita APIs** (Run, Build, Scheduler, Monitoring, Logging, Artifact Registry)  
2. **Cria Artifact Registry** `europe-west1-docker.pkg.dev/<project>/sre-demo/`
3. **Builda e push** da imagem Docker
4. **Deploy no Cloud Run** (min-instances=0 → custo $0 quando idle)
5. **Cloud Scheduler** — 3 jobs:
   - `sre-demo-health-poller` — `/health` cada 5 min (tráfego normal)
   - `sre-demo-chaos` — `/chaos` cada 10 min (erros aleatórios)
   - `sre-demo-error-burst` — `/error` cada 30 min (500 garantido)
6. **Uptime Check** — Cloud Monitoring verifica `/health` cada 60s
7. **Alerting Policy** — dispara quando:
   - Taxa de 5xx > 5% em 5 minutos
   - p95 latência > 5000ms em 5 minutos

## Endpoints

| Endpoint | Comportamento | Serve para |
|---|---|---|
| `GET /health` | 200 OK sempre | Baseline + Uptime check |
| `GET /error` | **500 sempre** + stack trace | Log Explorer errors |
| `GET /slow` | 200 após 6-9s | Latency metrics + SLO breach |
| `GET /crash` | Exception não tratada → 500 | Crash logs |
| `GET /db-timeout` | 5% 500, 30% lento, 65% OK | Flaky DB simulation |
| `POST /webhook` | 20% 500, 80% OK | Event ingestion errors |
| `GET /chaos` | Mix aleatório dos anteriores | Scheduler job |

## O que aparece no GCP

### Log Explorer
```
resource.type="cloud_run_revision"
resource.labels.service_name="sre-demo-service"
severity>=ERROR
```

Verás:
- `ERROR: Unhandled exception in payment processing: psycopg2.OperationalError...`
- `ERROR: Database connection pool exhausted — all 50 connections in use`
- `ERROR: Worker entering unstable state — OOM imminent`
- `WARNING: Slow query detected — waiting 7.3s for replica to respond`
- Stack traces completos com `error.stack`

### Cloud Monitoring

Query de métricas úteis:
```
metric.type="run.googleapis.com/request_count"
resource.labels.service_name="sre-demo-service"
metric.labels.response_code_class="5xx"
```
```
metric.type="run.googleapis.com/request_latencies"
resource.labels.service_name="sre-demo-service"
```

### Teste manual rápido

```powershell
.\smoke-test.ps1 -ServiceUrl "https://sre-demo-service-xxx-ew.a.run.app"
```

## Custo estimado

Com os schedules configurados (~10 requests/hora):
- Cloud Run: **$0** (free tier: 2M requests/mês, min-instances=0)
- Artifact Registry: **$0** (free tier: 0.5 GB/mês)
- Cloud Scheduler: **$0** (free tier: 3 jobs)
- Cloud Monitoring: **$0** (free tier: métricas básicas)
- **Total: ~$0/mês**

## Limpeza

```powershell
$proj = "optimum-web-487816-v4"
$region = "europe-west1"
gcloud run services delete sre-demo-service --region $region --project $proj
gcloud scheduler jobs delete sre-demo-health-poller --location $region --project $proj
gcloud scheduler jobs delete sre-demo-chaos --location $region --project $proj
gcloud scheduler jobs delete sre-demo-error-burst --location $region --project $proj
gcloud artifacts repositories delete sre-demo --location $region --project $proj
```
