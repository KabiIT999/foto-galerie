# ─────────────────────────────────────────────────────────────────────────────
# main.tf – Terraform Infrastructure as Code fuer Foto-Galerie
#
# Erstellt folgende Azure-Ressourcen:
#   - Resource Group
#   - Storage Account (ZRS = 3-Zonen-Redundanz) + Container "photos"
#   - PostgreSQL Flexible Server (B1ms) + Datenbank "fotodb"
#   - App Service Plan (B1 Linux)
#   - Linux Web App (Docker Container)
#   - Auto-Scale Regeln (1-3 Instanzen bei CPU > 70% / < 30%)
#   - CDN Profile + Endpoint (Microsoft Classic)
# ─────────────────────────────────────────────────────────────────────────────

terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
  }
}

provider "azurerm" {
  features {}
}

# ── Resource Group ────────────────────────────────────────────────────────────
resource "azurerm_resource_group" "main" {
  name     = var.resource_group_name
  location = var.location
}

# ── Storage Account (ZRS = Zone-Redundant Storage) ───────────────────────────
resource "azurerm_storage_account" "main" {
  name                     = var.storage_account_name
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  account_tier             = "Standard"
  account_replication_type = "ZRS"   # 3 Zonen-Redundanz = Hochverfuegbarkeit
}

# ── Container "photos" (public blob access fuer CDN) ─────────────────────────
resource "azurerm_storage_container" "photos" {
  name                  = "photos"
  storage_account_name  = azurerm_storage_account.main.name
  container_access_type = "blob"  # Fotos oeffentlich lesbar (via CDN)
}

# ── PostgreSQL Flexible Server ────────────────────────────────────────────────
resource "azurerm_postgresql_flexible_server" "main" {
  name                   = "foto-galerie-db"
  resource_group_name    = azurerm_resource_group.main.name
  location               = azurerm_resource_group.main.location
  version                = "15"
  administrator_login    = "fotoadmin"
  administrator_password = var.db_admin_password
  sku_name               = "B_Standard_B1ms"  # Guenstigste Option fuer Entwicklung
  storage_mb             = 32768
  backup_retention_days  = 7
  zone                   = "1"
}

# ── Firewall: Azure-interne Dienste erlauben ──────────────────────────────────
resource "azurerm_postgresql_flexible_server_firewall_rule" "azure_services" {
  name             = "AllowAzureServices"
  server_id        = azurerm_postgresql_flexible_server.main.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

# ── PostgreSQL Datenbank ──────────────────────────────────────────────────────
resource "azurerm_postgresql_flexible_server_database" "fotodb" {
  name      = "fotodb"
  server_id = azurerm_postgresql_flexible_server.main.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# ── App Service Plan (B1 Linux) ───────────────────────────────────────────────
resource "azurerm_service_plan" "main" {
  name                = "foto-galerie-plan"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  os_type             = "Linux"
  sku_name            = "B1"  # Basic B1: unterstuetzt Auto-Scale (F1 nicht!)
}

# ── Linux Web App (Docker Container von Docker Hub) ───────────────────────────
resource "azurerm_linux_web_app" "main" {
  name                = var.app_name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  service_plan_id     = azurerm_service_plan.main.id

  site_config {
    application_stack {
      docker_image_name = var.docker_image
    }
    health_check_path = "/health"  # Azure prueft Verfuegbarkeit regelmaessig
  }

  # Umgebungsvariablen fuer den Container (werden als App Settings gesetzt)
  app_settings = {
    AZURE_STORAGE_CONNECTION_STRING = azurerm_storage_account.main.primary_connection_string
    DATABASE_URL                    = "postgresql://fotoadmin:${var.db_admin_password}@${azurerm_postgresql_flexible_server.main.fqdn}/fotodb?sslmode=require"
    CDN_URL                         = "https://${azurerm_cdn_endpoint.photos.name}.azureedge.net/photos"
    WEBSITES_PORT                   = "8000"
    APP_USERNAME                    = var.app_username
    APP_PASSWORD                    = var.app_password
  }
}

# ── Auto-Scale: 1 bis 3 Instanzen je nach CPU-Last ───────────────────────────
resource "azurerm_monitor_autoscale_setting" "api" {
  name                = "foto-galerie-autoscale"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  target_resource_id  = azurerm_service_plan.main.id

  profile {
    name = "default"
    capacity {
      default = 1
      minimum = 1
      maximum = 3
    }

    # Scale-OUT: CPU > 70% fuer 5 Min -> +1 Instanz
    rule {
      metric_trigger {
        metric_name        = "CpuPercentage"
        metric_resource_id = azurerm_service_plan.main.id
        time_grain         = "PT1M"
        statistic          = "Average"
        time_window        = "PT5M"
        time_aggregation   = "Average"
        operator           = "GreaterThan"
        threshold          = 70
      }
      scale_action {
        direction = "Increase"
        type      = "ChangeCount"
        value     = "1"
        cooldown  = "PT5M"
      }
    }

    # Scale-IN: CPU < 30% fuer 10 Min -> -1 Instanz
    rule {
      metric_trigger {
        metric_name        = "CpuPercentage"
        metric_resource_id = azurerm_service_plan.main.id
        time_grain         = "PT1M"
        statistic          = "Average"
        time_window        = "PT10M"
        time_aggregation   = "Average"
        operator           = "LessThan"
        threshold          = 30
      }
      scale_action {
        direction = "Decrease"
        type      = "ChangeCount"
        value     = "1"
        cooldown  = "PT10M"
      }
    }
  }
}

# ── CDN Profile (Microsoft Classic) ──────────────────────────────────────────
resource "azurerm_cdn_profile" "main" {
  name                = "foto-galerie-cdn"
  resource_group_name = azurerm_resource_group.main.name
  location            = "global"
  sku                 = "Standard_Microsoft"
}

# ── CDN Endpoint: Fotos aus Blob Storage weltweit schnell ausliefern ─────────
resource "azurerm_cdn_endpoint" "photos" {
  name                = "foto-galerie-photos"
  profile_name        = azurerm_cdn_profile.main.name
  resource_group_name = azurerm_resource_group.main.name
  location            = "global"

  origin {
    name      = "blob-origin"
    host_name = azurerm_storage_account.main.primary_blob_host
  }

  origin_host_header = azurerm_storage_account.main.primary_blob_host
}
