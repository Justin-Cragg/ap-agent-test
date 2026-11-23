# Google Cloud
#
#   HTTPS load balancer --(IAP, Cloud Armor)--> Cloud Run service (API + approval/review UI)
#                      \-(Cloud Armor only)--> same service, /webhooks/* (secret-checked)
#   Cloud Run ingress = internal + load balancer only, so IAP can't be bypassed.
#   Cloud Scheduler -> Cloud Run jobs: poll, payment-run, purge
#   Payment executor job: separate service account and separate ERP credentials.
#   GCS (invoice files, lifecycle-expired), Secret Manager, Vertex AI, log-based alerts.
#
# Secrets are created out-of-band (values never land in Terraform state) and
# mapped to env vars with var.secret_env / var.executor_secret_env.

terraform {
  required_providers {
    google = { source = "hashicorp/google", version = ">= 6.0" }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "image" { type = string }
variable "domain" {
  type        = string
  description = "Public hostname for the approval UI, e.g. ap.example.com"
}
variable "iap_members" {
  type        = list(string)
  description = "Who may reach the UI at all, e.g. [\"group:finance@example.com\"]. Roles are still enforced by the app."
}
variable "plain_env" {
  type    = map(string)
  default = {}
}
variable "secret_env" {
  type        = map(string)
  description = "ENV_VAR => Secret Manager secret id for the agent (ERP user WITHOUT payment rights)"
  default     = {}
}
variable "executor_secret_env" {
  type        = map(string)
  description = "ENV_VAR => secret id for the payment executor (ERP user WITH payment rights, DATABASE_URL, ...)"
  default     = {}
}
variable "alert_email" { type = string }
variable "retention_days" {
  type    = number
  default = 2555 # 7 years
}
variable "poll_schedule" {
  type    = string
  default = "*/5 * * * *"
}
variable "payment_run_schedule" {
  type    = string
  default = "0 9 * * 2,4"
}

data "google_project" "this" {}

locals {
  base_env = merge({
    ENVIRONMENT       = "production"
    LLM_PROVIDER      = "vertex"
    VERTEX_PROJECT_ID = var.project_id
    VERTEX_REGION     = "global"
    BLOB_BACKEND      = "gcs"
    BLOB_BUCKET       = google_storage_bucket.invoices.name
    SECRET_STORE      = "gcp"
    GCP_PROJECT_ID    = var.project_id
    AUTH_MODE         = "iap"
    IAP_AUDIENCE      = "/projects/${data.google_project.this.number}/global/backendServices/${google_compute_backend_service.ui.generated_id}"
    PUBLIC_BASE_URL   = "https://${var.domain}"
    PAYMENT_EXECUTION = "separate"
  }, var.plain_env)
  jobs = {
    poll        = { cmd = ["ap-agent", "poll"], schedule = var.poll_schedule, executor = false }
    payment-run = { cmd = ["ap-agent", "payment-run"], schedule = var.payment_run_schedule, executor = false }
    purge       = { cmd = ["ap-agent", "purge"], schedule = "17 3 * * 0", executor = false }
    pay-execute = { cmd = ["ap-agent", "execute-payments"], schedule = "*/10 * * * *", executor = true }
  }
}

resource "google_project_service" "apis" {
  for_each           = toset(["run.googleapis.com", "cloudscheduler.googleapis.com", "secretmanager.googleapis.com", "aiplatform.googleapis.com", "iap.googleapis.com", "compute.googleapis.com", "monitoring.googleapis.com", "logging.googleapis.com"])
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------- identities
resource "google_service_account" "agent" {
  account_id   = "ap-agent"
  display_name = "AP agent runtime (no payment rights)"
}
resource "google_service_account" "executor" {
  account_id   = "ap-payment-executor"
  display_name = "AP payment executor"
}
resource "google_project_iam_member" "vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.agent.email}"
}
resource "google_secret_manager_secret_iam_member" "agent" {
  for_each  = var.secret_env
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent.email}"
}
resource "google_secret_manager_secret_iam_member" "executor" {
  for_each  = var.executor_secret_env
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.executor.email}"
}
# Rotating OAuth refresh tokens are written to Secret Manager (SECRET_STORE=gcp).
resource "google_project_iam_member" "token_store" {
  project = var.project_id
  role    = "roles/secretmanager.admin"
  member  = "serviceAccount:${google_service_account.agent.email}"
  condition {
    title      = "ap-agent token secrets only"
    expression = "resource.name.startsWith(\"projects/${data.google_project.this.number}/secrets/ap-agent-\")"
  }
}

# ---------------------------------------------------------------- storage
resource "google_storage_bucket" "invoices" {
  name                        = "${var.project_id}-ap-invoices"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning { enabled = true }
  lifecycle_rule {
    condition { age = var.retention_days }
    action { type = "Delete" }
  }
  lifecycle_rule {
    condition { days_since_noncurrent_time = 30 }
    action { type = "Delete" }
  }
}
resource "google_storage_bucket_iam_member" "rw" {
  bucket = google_storage_bucket.invoices.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.agent.email}"
}

# ---------------------------------------------------------------- service
resource "google_cloud_run_v2_service" "api" {
  name     = "ap-agent"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  template {
    service_account = google_service_account.agent.email
    timeout         = "900s"
    containers {
      image = var.image
      resources {
        limits = { cpu = "1", memory = "1Gi" }
      }
      dynamic "env" {
        for_each = local.base_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = var.secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_service_iam_member" "lb_invoker" {
  name     = google_cloud_run_v2_service.api.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers" # reachable only through the LB (ingress setting); IAP + app auth enforce identity
}

# ---------------------------------------------------------------- load balancer + IAP + Cloud Armor
resource "google_compute_region_network_endpoint_group" "neg" {
  name                  = "ap-agent-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"
  cloud_run { service = google_cloud_run_v2_service.api.name }
}

resource "google_compute_security_policy" "armor" {
  name = "ap-agent-armor"
  rule {
    action   = "deny(403)"
    priority = 1000
    match {
      expr { expression = "evaluatePreconfiguredWaf('sqli-v33-stable') || evaluatePreconfiguredWaf('xss-v33-stable') || evaluatePreconfiguredWaf('lfi-v33-stable')" }
    }
  }
  rule {
    action   = "rate_based_ban"
    priority = 2000
    match {
      versioned_expr = "SRC_IPS_V1"
      config { src_ip_ranges = ["*"] }
    }
    rate_limit_options {
      conform_action   = "allow"
      exceed_action    = "deny(429)"
      enforce_on_key   = "IP"
      ban_duration_sec = 600
      rate_limit_threshold {
        count        = 300
        interval_sec = 60
      }
    }
  }
  rule {
    action   = "allow"
    priority = 2147483647
    match {
      versioned_expr = "SRC_IPS_V1"
      config { src_ip_ranges = ["*"] }
    }
  }
}

resource "google_compute_backend_service" "ui" {
  name                  = "ap-agent-ui"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTPS"
  security_policy       = google_compute_security_policy.armor.id
  backend { group = google_compute_region_network_endpoint_group.neg.id }
  iap { enabled = true }
}

resource "google_compute_backend_service" "hooks" {
  name                  = "ap-agent-webhooks"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTPS"
  security_policy       = google_compute_security_policy.armor.id
  backend { group = google_compute_region_network_endpoint_group.neg.id }
}

resource "google_iap_web_backend_service_iam_member" "access" {
  for_each            = toset(var.iap_members)
  web_backend_service = google_compute_backend_service.ui.name
  role                = "roles/iap.httpsResourceAccessor"
  member              = each.value
}

resource "google_compute_url_map" "lb" {
  name            = "ap-agent"
  default_service = google_compute_backend_service.ui.id
  host_rule {
    hosts        = [var.domain]
    path_matcher = "main"
  }
  path_matcher {
    name            = "main"
    default_service = google_compute_backend_service.ui.id
    path_rule {
      paths   = ["/webhooks/*", "/healthz"]
      service = google_compute_backend_service.hooks.id
    }
  }
}

resource "google_compute_managed_ssl_certificate" "cert" {
  name = "ap-agent-cert"
  managed { domains = [var.domain] }
}
resource "google_compute_target_https_proxy" "https" {
  name             = "ap-agent-https"
  url_map          = google_compute_url_map.lb.id
  ssl_certificates = [google_compute_managed_ssl_certificate.cert.id]
}
resource "google_compute_global_address" "ip" { name = "ap-agent-ip" }
resource "google_compute_global_forwarding_rule" "https" {
  name                  = "ap-agent-https"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  target                = google_compute_target_https_proxy.https.id
  ip_address            = google_compute_global_address.ip.id
  port_range            = "443"
}

# ---------------------------------------------------------------- jobs
resource "google_cloud_run_v2_job" "task" {
  for_each = local.jobs
  name     = "ap-agent-${each.key}"
  location = var.region
  template {
    template {
      service_account = each.value.executor ? google_service_account.executor.email : google_service_account.agent.email
      timeout         = "3600s"
      max_retries     = 0
      containers {
        image   = var.image
        command = each.value.cmd
        dynamic "env" {
          for_each = local.base_env
          content {
            name  = env.key
            value = env.value
          }
        }
        dynamic "env" {
          for_each = each.value.executor ? var.executor_secret_env : var.secret_env
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }
      }
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_service_account" "scheduler" { account_id = "ap-agent-scheduler" }

resource "google_cloud_run_v2_job_iam_member" "invoker" {
  for_each = google_cloud_run_v2_job.task
  name     = each.value.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "run" {
  for_each = local.jobs
  name     = "ap-agent-${each.key}"
  schedule = each.value.schedule
  region   = var.region
  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.task[each.key].name}:run"
    oauth_token { service_account_email = google_service_account.scheduler.email }
  }
}

# ---------------------------------------------------------------- alerting
resource "google_monitoring_notification_channel" "email" {
  display_name = "AP agent alerts"
  type         = "email"
  labels       = { email_address = var.alert_email }
}

resource "google_logging_metric" "errors" {
  name   = "ap_agent_errors"
  filter = "(resource.type=\"cloud_run_revision\" OR resource.type=\"cloud_run_job\") AND jsonPayload.severity=\"ERROR\" AND (resource.labels.service_name:\"ap-agent\" OR resource.labels.job_name:\"ap-agent\")"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_alert_policy" "errors" {
  display_name = "AP agent errors"
  combiner     = "OR"
  conditions {
    display_name = "error log rate"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.errors.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      duration        = "300s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email.id]
}

output "url" { value = "https://${var.domain}" }
output "lb_ip" { value = google_compute_global_address.ip.address }
