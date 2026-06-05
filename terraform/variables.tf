variable "resource_group_name" {
  description = "Name der Azure Resource Group"
  default     = "foto-galerie-rg"
}

variable "location" {
  description = "Azure Region"
  default     = "Germany West Central"
}

variable "storage_account_name" {
  description = "Name des Azure Storage Accounts (global eindeutig)"
  default     = "fotogaleriekabi"
}

variable "app_name" {
  description = "Name des Azure App Service"
  default     = "app-fotogalerie-prod"
}

variable "docker_image" {
  description = "Docker Hub Image (DOCKERHUBNAME/foto-galerie:latest)"
  default     = "kabilank/foto-galerie:latest"
}

variable "db_admin_password" {
  description = "Passwort fuer den PostgreSQL-Admin-Benutzer"
  sensitive   = true
}

variable "app_username" {
  description = "Benutzername fuer die App-Login (HTTP Basic Auth)"
  default     = "admin"
}

variable "app_password" {
  description = "Passwort fuer die App-Login (HTTP Basic Auth)"
  sensitive   = true
  default     = "foto2024"
}
