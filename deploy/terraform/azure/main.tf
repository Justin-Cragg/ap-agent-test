# Azure
#
#   Container App (API + approval/review UI) with Easy Auth (Entra ID). The
#   auth sidecar is the only ingress path, so the X-MS-CLIENT-PRINCIPAL header
#   can't be forged; /webhooks/* and /healthz are excluded and secret-checked by the app.
#   Container Apps Jobs on cron: poll, payment-run, purge
#   Payment executor job: separate managed identity and separate ERP credentials.
#   Blob Storage (lifecycle-expired), Key Vault, Microsoft Foundry, Log Analytics alerts.
#
# Secrets are created in Key Vault out-of-band and referenced by secret URI.

terraform {
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = ">= 3.110" }
    azapi   = { source = "azure/azapi", version = ">= 1.13" }
  }
}

provider "azurerm" {
  features {}
}

variable "location" {
  type    = string
  default = "eastus2"
}
variable "resource_group" {
  type    = string
  default = "rg-ap-agent"
}
variable "image" { type = string }
variable "foundry_resource" { type = string }
variable "foundry_resource_id" {
  type        = string
  description = "Resource id of the Foundry resource (for the Cognitive Services User role)"
}
variable "key_vault_id" { type = string }
variable "key_vault_url" { type = string }
variable "entra" {
  description = "Entra ID app registration used by Easy Auth"
  type = object({
    tenant_id                  = string
    client_id                  = string
    client_secret_kv_secret_id = string
  })
}
variable "allowed_ip_ranges" {
  type        = list(string)
  description = "Optional CIDRs allowed to reach the app (e.g. corporate egress + Graph/Pub/Sub ranges). Empty = any."
  default     = []
}
variable "plain_env" {
  type    = map(string)
  default = {}
}
variable "secret_env" {
  type        = map(string)
  description = "ENV_VAR => Key Vault secret URI for the agent (ERP user WITHOUT payment rights)"
  default     = {}
}
variable "executor_secret_env" {
  type        = map(string)
  description = "ENV_VAR => Key Vault secret URI for the payment executor (ERP user WITH payment rights)"
  default     = {}
}
variable "alert_email" { type = string }
variable "retention_days" {
  type    = number
  default = 2555
}
variable "poll_schedule" {
  type    = string
  default = "*/5 * * * *"
}
variable "payment_run_schedule" {
  type    = string
  default = "0 9 * * 2,4"
}

resource "azurerm_resource_group" "ap" {
  name     = var.resource_group
  location = var.location
}

# ---------------------------------------------------------------- identities
resource "azurerm_user_assigned_identity" "agent" {
  name                = "id-ap-agent"
  resource_group_name = azurerm_resource_group.ap.name
  location            = azurerm_resource_group.ap.location
}
resource "azurerm_user_assigned_identity" "executor" {
  name                = "id-ap-payment-executor"
  resource_group_name = azurerm_resource_group.ap.name
  location            = azurerm_resource_group.ap.location
}
resource "azurerm_role_assignment" "kv_agent" {
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets Officer" # reads config secrets, writes rotating OAuth tokens
  principal_id         = azurerm_user_assigned_identity.agent.principal_id
}
resource "azurerm_role_assignment" "kv_executor" {
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.executor.principal_id
}
resource "azurerm_role_assignment" "foundry" {
  scope                = var.foundry_resource_id
  role_definition_name = "Cognitive Services User"
  principal_id         = azurerm_user_assigned_identity.agent.principal_id
}

# ---------------------------------------------------------------- storage
resource "azurerm_storage_account" "ap" {
  name                            = substr(replace("stap${azurerm_resource_group.ap.name}", "-", ""), 0, 24)
  resource_group_name             = azurerm_resource_group.ap.name
  location                        = azurerm_resource_group.ap.location
  account_tier                    = "Standard"
  account_replication_type        = "ZRS"
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false
  min_tls_version                 = "TLS1_2"
  blob_properties {
    versioning_enabled = true
  }
}
resource "azurerm_storage_container" "invoices" {
  name                  = "invoices"
  storage_account_name  = azurerm_storage_account.ap.name
  container_access_type = "private"
}
resource "azurerm_storage_management_policy" "retention" {
  storage_account_id = azurerm_storage_account.ap.id
  rule {
    name    = "retention"
    enabled = true
    filters { blob_types = ["blockBlob"] }
    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = var.retention_days
      }
      version {
        delete_after_days_since_creation = 30
      }
    }
  }
}
resource "azurerm_role_assignment" "blob" {
  scope                = azurerm_storage_account.ap.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.agent.principal_id
}

# ---------------------------------------------------------------- environment
resource "azurerm_log_analytics_workspace" "ap" {
  name                = "log-ap-agent"
  resource_group_name = azurerm_resource_group.ap.name
  location            = azurerm_resource_group.ap.location
  retention_in_days   = 365
}

resource "azurerm_container_app_environment" "ap" {
  name                       = "cae-ap-agent"
  resource_group_name        = azurerm_resource_group.ap.name
  location                   = azurerm_resource_group.ap.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.ap.id
}

locals {
  env = merge({
    ENVIRONMENT               = "production"
    LLM_PROVIDER              = "foundry"
    FOUNDRY_RESOURCE          = var.foundry_resource
    BLOB_BACKEND              = "azure"
    BLOB_BUCKET               = azurerm_storage_container.invoices.name
    AZURE_STORAGE_ACCOUNT_URL = azurerm_storage_account.ap.primary_blob_endpoint
    SECRET_STORE              = "azure"
    AZURE_KEY_VAULT_URL       = var.key_vault_url
    AUTH_MODE                 = "easyauth"
    PAYMENT_EXECUTION         = "separate"
  }, var.plain_env)
  secret_names      = { for k, v in var.secret_env : k => replace(lower(k), "_", "-") }
  exec_secret_names = { for k, v in var.executor_secret_env : k => replace(lower(k), "_", "-") }
  jobs = {
    poll        = { cron = var.poll_schedule, executor = false }
    payment-run = { cron = var.payment_run_schedule, executor = false }
    purge       = { cron = "17 3 * * 0", executor = false }
    pay-execute = { cron = "*/10 * * * *", executor = true }
  }
  job_cmd = { poll = ["ap-agent", "poll"], payment-run = ["ap-agent", "payment-run"], purge = ["ap-agent", "purge"], pay-execute = ["ap-agent", "execute-payments"] }
}

resource "azurerm_container_app" "api" {
  name                         = "ca-ap-agent"
  resource_group_name          = azurerm_resource_group.ap.name
  container_app_environment_id = azurerm_container_app_environment.ap.id
  revision_mode                = "Single"
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.agent.id]
  }
  secret {
    name                = "entra-client-secret"
    key_vault_secret_id = var.entra.client_secret_kv_secret_id
    identity            = azurerm_user_assigned_identity.agent.id
  }
  dynamic "secret" {
    for_each = var.secret_env
    content {
      name                = local.secret_names[secret.key]
      key_vault_secret_id = secret.value
      identity            = azurerm_user_assigned_identity.agent.id
    }
  }
  ingress {
    external_enabled = true
    target_port      = 8080
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
    dynamic "ip_security_restriction" {
      for_each = var.allowed_ip_ranges
      content {
        name             = "allow-${ip_security_restriction.key}"
        action           = "Allow"
        ip_address_range = ip_security_restriction.value
      }
    }
  }
  template {
    min_replicas = 1
    max_replicas = 3
    container {
      name   = "ap-agent"
      image  = var.image
      cpu    = 1
      memory = "2Gi"
      dynamic "env" {
        for_each = merge(local.env, { AZURE_CLIENT_ID = azurerm_user_assigned_identity.agent.client_id })
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = var.secret_env
        content {
          name        = env.key
          secret_name = local.secret_names[env.key]
        }
      }
    }
  }
  depends_on = [azurerm_role_assignment.kv_agent]
}

# Easy Auth (Entra ID). Unauthenticated browser requests are redirected to sign-in;
# webhook paths are excluded and checked by the app with WEBHOOK_SECRET.
resource "azapi_resource" "auth" {
  type      = "Microsoft.App/containerApps/authConfigs@2024-03-01"
  name      = "current"
  parent_id = azurerm_container_app.api.id
  body = jsonencode({
    properties = {
      platform = { enabled = true }
      globalValidation = {
        unauthenticatedClientAction = "RedirectToLoginPage"
        redirectToProvider          = "azureactivedirectory"
        excludedPaths               = ["/webhooks/gmail", "/webhooks/outlook", "/healthz"]
      }
      identityProviders = {
        azureActiveDirectory = {
          enabled = true
          registration = {
            clientId                = var.entra.client_id
            clientSecretSettingName = "entra-client-secret"
            openIdIssuer            = "https://login.microsoftonline.com/${var.entra.tenant_id}/v2.0"
          }
          validation = { allowedAudiences = ["api://${var.entra.client_id}"] }
        }
      }
    }
  })
}

resource "azurerm_container_app_job" "task" {
  for_each                     = local.jobs
  name                         = "caj-ap-${each.key}"
  resource_group_name          = azurerm_resource_group.ap.name
  location                     = azurerm_resource_group.ap.location
  container_app_environment_id = azurerm_container_app_environment.ap.id
  replica_timeout_in_seconds   = 3600
  replica_retry_limit          = 0
  identity {
    type         = "UserAssigned"
    identity_ids = [each.value.executor ? azurerm_user_assigned_identity.executor.id : azurerm_user_assigned_identity.agent.id]
  }
  schedule_trigger_config {
    cron_expression = each.value.cron
  }
  dynamic "secret" {
    for_each = each.value.executor ? var.executor_secret_env : var.secret_env
    content {
      name                = each.value.executor ? local.exec_secret_names[secret.key] : local.secret_names[secret.key]
      key_vault_secret_id = secret.value
      identity            = each.value.executor ? azurerm_user_assigned_identity.executor.id : azurerm_user_assigned_identity.agent.id
    }
  }
  template {
    container {
      name    = "ap-agent"
      image   = var.image
      cpu     = 1
      memory  = "2Gi"
      command = local.job_cmd[each.key]
      dynamic "env" {
        for_each = merge(local.env, { AZURE_CLIENT_ID = each.value.executor ? azurerm_user_assigned_identity.executor.client_id : azurerm_user_assigned_identity.agent.client_id })
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = each.value.executor ? var.executor_secret_env : var.secret_env
        content {
          name        = env.key
          secret_name = each.value.executor ? local.exec_secret_names[env.key] : local.secret_names[env.key]
        }
      }
    }
  }
  depends_on = [azurerm_role_assignment.kv_agent, azurerm_role_assignment.kv_executor]
}

# ---------------------------------------------------------------- alerting
resource "azurerm_monitor_action_group" "ap" {
  name                = "ag-ap-agent"
  resource_group_name = azurerm_resource_group.ap.name
  short_name          = "apagent"
  email_receiver {
    name          = "ap-ops"
    email_address = var.alert_email
  }
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "errors" {
  name                 = "ap-agent-errors"
  resource_group_name  = azurerm_resource_group.ap.name
  location             = azurerm_resource_group.ap.location
  scopes               = [azurerm_log_analytics_workspace.ap.id]
  severity             = 2
  evaluation_frequency = "PT5M"
  window_duration      = "PT5M"
  criteria {
    query                   = "ContainerAppConsoleLogs_CL | where Log_s has '\"severity\": \"ERROR\"'"
    time_aggregation_method = "Count"
    operator                = "GreaterThan"
    threshold               = 5
  }
  action { action_groups = [azurerm_monitor_action_group.ap.id] }
}

output "app_url" { value = "https://${azurerm_container_app.api.ingress[0].fqdn}" }
