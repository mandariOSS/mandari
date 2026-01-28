# =============================================================================
# Hetzner Cloud Configuration
# =============================================================================

variable "hcloud_token" {
  description = "Hetzner Cloud API Token"
  type        = string
  sensitive   = true
}

variable "ssh_public_key_path" {
  description = "Path to SSH public key for server access"
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

# =============================================================================
# Server Configuration
# =============================================================================

variable "server_type" {
  description = "Hetzner server type (cx31 = 2 vCPU, 8GB RAM, 80GB SSD)"
  type        = string
  default     = "cx31"

  validation {
    condition     = can(regex("^(cx[1234][1-9]|cpx[1234][1-9]|ccx[1234][1-9])$", var.server_type))
    error_message = "Server type must be a valid Hetzner Cloud server type."
  }
}

variable "load_balancer_type" {
  description = "Hetzner load balancer type"
  type        = string
  default     = "lb11"
}

variable "volume_size" {
  description = "Size of data volumes in GB"
  type        = number
  default     = 50
}

# =============================================================================
# Environment Configuration
# =============================================================================

variable "environment" {
  description = "Deployment environment (production, staging)"
  type        = string
  default     = "production"

  validation {
    condition     = contains(["production", "staging", "development"], var.environment)
    error_message = "Environment must be one of: production, staging, development."
  }
}

variable "domain_names" {
  description = "Domain names for managed certificate"
  type        = list(string)
  default     = ["mandari.de"]
}

# =============================================================================
# Network Configuration
# =============================================================================

variable "network_zone" {
  description = "Hetzner network zone"
  type        = string
  default     = "eu-central"
}

variable "master_private_ip" {
  description = "Private IP for master server"
  type        = string
  default     = "10.0.1.10"
}

variable "slave_private_ip" {
  description = "Private IP for slave server"
  type        = string
  default     = "10.0.1.11"
}
