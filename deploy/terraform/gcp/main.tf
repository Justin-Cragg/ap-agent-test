# Google Cloud: Cloud Run service (API + approval UI), Cloud Run jobs on a
# Cloud Scheduler cron (mailbox poll, payment-run proposal), GCS for invoice
# files, Secret Manager for credentials, Vertex AI for Claude.
#
# Secrets are created out-of-band (so values never land in Terraform state):
#   echo -n "$VALUE" | gcloud secrets create ap-agent-api-token --data-file=-
# and mapped to env vars with var.secret_env.

terraform {
  required_providers {
    google = { source = "hashicorp/google", version = ">= 5.30" }
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
variable "image" {
  type        = string
  description = "Container image, e.g. us-docker.pkg.dev/PROJECT/ap/ap-agent:1.0"
}
variable "plain_env" {
  type        = map(string)
  description = "Non-secret env vars (ERP_PROVIDER, MAIL_PROVIDER, COMPANY_NAME, ...)"
  default     = {}
}
variable "secret_env" {
  type        = map(string)
  description = "ENV_VAR => Secret Manager secret id (API_TOKEN, DATABASE_URL, ERP/mail credentials)"
  default     = {}
}
variable "poll_schedule" {
  type    = string
  default = "*/5 * * * *"
}
variable "payment_run_schedule" {
  type    = string
  default = "0 9 * * 2,4"
}

locals {
  env = merge({
    LLM_PROVIDER      = "vertex"
    VERTEX_PROJECT_ID = var.project_id
    VERTEX_REGION     = "global"
    BLOB_BACKEND      = "gcs"
    BLOB_BUCKET       = google_storage_bucket.invoices.name
  }, var.plain_env)
}

resource "google_project_service" "apis" {
  for_each           = toset(["run.googleapis.com", "cloudscheduler.googleapis.com", "secretmanager.googleapis.com", "aiplatform.googleapis.com"])
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "agent" {
  account_id   = "ap-agent"
  display_name = "AP agent runtime"
}

resource "google_project_iam_member" "vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.agent.email}"
}

resource "google_secret_manager_secret_iam_member" "access" {
  for_each  = var.secret_env
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent.email}"
}

resource "google_storage_bucket" "invoices" {
  name                        = "${var.project_id}-ap-invoices"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning { enabled = true }
}

resource "google_storage_bucket_iam_member" "rw" {
  bucket = google_storage_bucket.invoices.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.agent.email}"
}

resource "google_cloud_run_v2_service" "api" {
  name     = "ap-agent"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL" # put IAP or a load balancer with IAP in front for SSO approvals
  template {
    service_account = google_service_account.agent.email
    timeout         = "900s"
    containers {
      image = var.image
      resources {
        limits = { cpu = "1", memory = "1Gi" }
      }
      dynamic "env" {
        for_each = local.env
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

resource "google_cloud_run_v2_job" "task" {
  for_each = { poll = ["ap-agent", "poll"], payment-run = ["ap-agent", "payment-run"] }
  name     = "ap-agent-${each.key}"
  location = var.region
  template {
    template {
      service_account = google_service_account.agent.email
      timeout         = "3600s"
      max_retries     = 0
      containers {
        image   = var.image
        command = each.value
        dynamic "env" {
          for_each = local.env
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
  }
  depends_on = [google_project_service.apis]
}

resource "google_service_account" "scheduler" {
  account_id = "ap-agent-scheduler"
}

resource "google_cloud_run_v2_job_iam_member" "invoker" {
  for_each = google_cloud_run_v2_job.task
  name     = each.value.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "run" {
  for_each = { poll = var.poll_schedule, payment-run = var.payment_run_schedule }
  name     = "ap-agent-${each.key}"
  schedule = each.value
  region   = var.region
  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.task[each.key].name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }
}

output "service_url" { value = google_cloud_run_v2_service.api.uri }
