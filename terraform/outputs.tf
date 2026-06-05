output "app_url" {
  description = "URL des Azure App Service (Backend API)"
  value       = "https://${azurerm_linux_web_app.main.default_hostname}"
}

output "health_check_url" {
  description = "Health-Check Endpunkt der API"
  value       = "https://${azurerm_linux_web_app.main.default_hostname}/health"
}

output "cdn_url" {
  description = "CDN-URL fuer Foto-Auslieferung"
  value       = "https://${azurerm_cdn_endpoint.photos.name}.azureedge.net"
}

output "db_host" {
  description = "Hostname des PostgreSQL Servers"
  value       = azurerm_postgresql_flexible_server.main.fqdn
}

output "storage_account_name" {
  description = "Name des Storage Accounts"
  value       = azurerm_storage_account.main.name
}
