# =============================================================================
# Server Outputs
# =============================================================================

output "master_ip" {
  description = "Public IPv4 address of master server"
  value       = hcloud_server.master.ipv4_address
}

output "master_ipv6" {
  description = "Public IPv6 address of master server"
  value       = hcloud_server.master.ipv6_address
}

output "slave_ip" {
  description = "Public IPv4 address of slave server"
  value       = hcloud_server.slave.ipv4_address
}

output "slave_ipv6" {
  description = "Public IPv6 address of slave server"
  value       = hcloud_server.slave.ipv6_address
}

output "master_private_ip" {
  description = "Private IP address of master server"
  value       = hcloud_server.master.network[*].ip
}

output "slave_private_ip" {
  description = "Private IP address of slave server"
  value       = hcloud_server.slave.network[*].ip
}

# =============================================================================
# Load Balancer Outputs
# =============================================================================

output "lb_ip" {
  description = "Public IPv4 address of load balancer"
  value       = hcloud_load_balancer.mandari.ipv4
}

output "lb_ipv6" {
  description = "Public IPv6 address of load balancer"
  value       = hcloud_load_balancer.mandari.ipv6
}

output "lb_id" {
  description = "ID of the load balancer"
  value       = hcloud_load_balancer.mandari.id
}

# =============================================================================
# Network Outputs
# =============================================================================

output "network_id" {
  description = "ID of the private network"
  value       = hcloud_network.mandari.id
}

output "subnet_id" {
  description = "ID of the subnet"
  value       = hcloud_network_subnet.mandari.id
}

# =============================================================================
# Volume Outputs
# =============================================================================

output "master_volume_id" {
  description = "ID of master data volume"
  value       = hcloud_volume.master_data.id
}

output "slave_volume_id" {
  description = "ID of slave data volume"
  value       = hcloud_volume.slave_data.id
}

output "master_volume_linux_device" {
  description = "Linux device path for master volume"
  value       = hcloud_volume.master_data.linux_device
}

output "slave_volume_linux_device" {
  description = "Linux device path for slave volume"
  value       = hcloud_volume.slave_data.linux_device
}

# =============================================================================
# Certificate Outputs
# =============================================================================

output "certificate_id" {
  description = "ID of the managed certificate"
  value       = hcloud_managed_certificate.mandari.id
}

output "certificate_domain_names" {
  description = "Domain names of the managed certificate"
  value       = hcloud_managed_certificate.mandari.domain_names
}

# =============================================================================
# Connection Info (for Ansible)
# =============================================================================

output "ansible_inventory" {
  description = "Ansible inventory snippet"
  value = <<-EOT
    [master]
    mandari-master ansible_host=${hcloud_server.master.ipv4_address} private_ip=10.0.0.3

    [slave]
    mandari-slave ansible_host=${hcloud_server.slave.ipv4_address} private_ip=10.0.0.4

    [all:vars]
    ansible_user=root
    ansible_python_interpreter=/usr/bin/python3
  EOT
}

# =============================================================================
# Summary
# =============================================================================

output "deployment_summary" {
  description = "Summary of deployed infrastructure"
  value = {
    load_balancer = {
      ip     = hcloud_load_balancer.mandari.ipv4
      ipv6   = hcloud_load_balancer.mandari.ipv6
      type   = hcloud_load_balancer.mandari.load_balancer_type
    }
    master = {
      ip         = hcloud_server.master.ipv4_address
      private_ip = "10.0.0.3"
      location   = hcloud_server.master.location
      type       = hcloud_server.master.server_type
    }
    slave = {
      ip         = hcloud_server.slave.ipv4_address
      private_ip = "10.0.0.4"
      location   = hcloud_server.slave.location
      type       = hcloud_server.slave.server_type
    }
    domains = var.domain_names
  }
}
