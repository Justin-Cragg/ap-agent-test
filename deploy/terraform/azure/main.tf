# Azure: Container App (API + approval UI), Container Apps Jobs on cron
# (mailbox poll, payment-run proposal), Blob Storage for invoice files,
# Key Vault for credentials, Microsoft Foundry for Claude (managed identity).
#
# Secrets are created in Key Vault out-of-band and referenced by secret URI
# in var.secret_env (ENV_VAR => https://<vault>.vault.azure.net/secrets/<name>).

terraform {
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = ">= 3.110" }
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
variable "foundry_resource" {
  type        = string
  description = "Microsoft Foundry resource name hosting the Claude deployment"
}
variable "key_vault_id" { type = string }
variable "plain_env" {
  type    = map(string)
  default = {}
}
variable "secret_env" {
  type    = map(string)
  default = {}
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

resource "azurerm_user_assigned_identity" "agent" {
  name                = "id-ap-agent"
  resource_group_name = azurerm_resource_group.ap.name
  location            = azurerm_resource_group.ap.location
}

resource "azurerm_role_assignment" "kv" {
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.agent.principal_id
}

resource "azurerm_storage_account" "ap" {
  name                            = substr(replace("stap${azurerm_resource_group.ap.name}", "-", ""), 0, 24)
  resource_group_name             = azurerm_resource_group.ap.name
  location                        = azurerm_resource_group.ap.location
  account_tier                    = "Standard"
  account_replication_type        = "ZRS"
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false
  blob_properties {
    versioning_enabled = true
  }
}
resource "azurerm_storage_container" "invoices" {
  name                  = "invoices"
  storage_account_name  = azurerm_storage_account.ap.name
  container_access_type = "private"
}
resource "azurerm_role_assignment" "blob" {
  scope                = azurerm_storage_account.ap.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.agent.principal_id
}

resource "azurerm_log_analytics_workspace" "ap" {
  name                = "log-ap-agent"
  resource_group_name = azurerm_resource_group.ap.name
  location            = azurerm_resource_group.ap.location
  retention_in_days   = 90
}

resource "azurerm_container_app_environment" "ap" {
  name                       = "cae-ap-agent"
  resource_group_name        = azurerm_resource_group.ap.name
  location                   = azurerm_resource_group.ap.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.ap.id
}

locals {
  env = merge({
    LLM_PROVIDER              = "foundry"
    FOUNDRY_RESOURCE          = var.foundry_resource
    AZURE_CLIENT_ID           = azurerm_user_assigned_identity.agent.client_id
    BLOB_BACKEND              = "azure"
    BLOB_BUCKET               = azurerm_storage_container.invoices.name
    AZURE_STORAGE_ACCOUNT_URL = azurerm_storage_account.ap.primary_blob_endpoint
  }, var.plain_env)
  secret_names = { for k, v in var.secret_env : k => replace(lower(k), "_", "-") }
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
  }
  template {
    min_replicas = 1
    max_replicas = 2
    container {
      name   = "ap-agent"
      image  = var.image
      cpu    = 1
      memory = "2Gi"
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
          name        = env.key
          secret_name = local.secret_names[env.key]
        }
      }
    }
  }
  depends_on = [azurerm_role_assignment.kv]
}

resource "azurerm_container_app_job" "task" {
  for_each                     = { poll = var.poll_schedule, payment-run = var.payment_run_schedule }
  name                         = "caj-ap-${each.key}"
  resource_group_name          = azurerm_resource_group.ap.name
  location                     = azurerm_resource_group.ap.location
  container_app_environment_id = azurerm_container_app_environment.ap.id
  replica_timeout_in_seconds   = 3600
  replica_retry_limit          = 0
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.agent.id]
  }
  schedule_trigger_config {
    cron_expression = each.value
  }
  dynamic "secret" {
    for_each = var.secret_env
    content {
      name                = local.secret_names[secret.key]
      key_vault_secret_id = secret.value
      identity            = azurerm_user_assigned_identity.agent.id
    }
  }
  template {
    container {
      name    = "ap-agent"
      image   = var.image
      cpu     = 1
      memory  = "2Gi"
      command = ["ap-agent", each.key]
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
          name        = env.key
          secret_name = local.secret_names[env.key]
        }
      }
    }
  }
  depends_on = [azurerm_role_assignment.kv]
}

output "app_url" { value = "https://${azurerm_container_app.api.ingress[0].fqdn}" }
