terraform {
  required_version = ">= 1.0"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.45"
    }
  }
}

provider "hcloud" {
  token = var.hcloud_token
}

# =============================================================================
# SSH Key
# =============================================================================

resource "hcloud_ssh_key" "mandari" {
  name       = "mandari-deploy"
  public_key = file(var.ssh_public_key_path)
}

# =============================================================================
# Private Network
# =============================================================================

resource "hcloud_network" "mandari" {
  name     = "mandari-network"
  ip_range = "10.0.0.0/16"
}

resource "hcloud_network_subnet" "mandari" {
  network_id   = hcloud_network.mandari.id
  type         = "cloud"
  network_zone = "eu-central"
  ip_range     = "10.0.1.0/24"
}

# =============================================================================
# Firewall
# =============================================================================

resource "hcloud_firewall" "mandari" {
  name = "mandari-firewall"

  # SSH
  rule {
    direction = "in"
    protocol  = "tcp"
    port      = "22"
    source_ips = [
      "0.0.0.0/0",
      "::/0"
    ]
  }

  # HTTP
  rule {
    direction = "in"
    protocol  = "tcp"
    port      = "80"
    source_ips = [
      "0.0.0.0/0",
      "::/0"
    ]
  }

  # HTTPS
  rule {
    direction = "in"
    protocol  = "tcp"
    port      = "443"
    source_ips = [
      "0.0.0.0/0",
      "::/0"
    ]
  }

  # PostgreSQL (internal only)
  rule {
    direction = "in"
    protocol  = "tcp"
    port      = "5432"
    source_ips = [
      "10.0.0.0/16"
    ]
  }

  # Redis (internal only)
  rule {
    direction = "in"
    protocol  = "tcp"
    port      = "6379"
    source_ips = [
      "10.0.0.0/16"
    ]
  }

  # ICMP (ping)
  rule {
    direction = "in"
    protocol  = "icmp"
    source_ips = [
      "0.0.0.0/0",
      "::/0"
    ]
  }
}

# =============================================================================
# VM 1 - Master
# =============================================================================

resource "hcloud_server" "master" {
  name        = "mandari-master"
  server_type = var.server_type
  image       = "ubuntu-24.04"
  location    = "fsn1"
  ssh_keys    = [hcloud_ssh_key.mandari.id]
  firewall_ids = [hcloud_firewall.mandari.id]

  labels = {
    role        = "master"
    app         = "mandari"
    environment = var.environment
  }

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }

  network {
    network_id = hcloud_network.mandari.id
    ip         = "10.0.1.10"
  }

  depends_on = [hcloud_network_subnet.mandari]
}

# =============================================================================
# VM 2 - Slave
# =============================================================================

resource "hcloud_server" "slave" {
  name        = "mandari-slave"
  server_type = var.server_type
  image       = "ubuntu-24.04"
  location    = "nbg1" # Different location for geo-redundancy
  ssh_keys    = [hcloud_ssh_key.mandari.id]
  firewall_ids = [hcloud_firewall.mandari.id]

  labels = {
    role        = "slave"
    app         = "mandari"
    environment = var.environment
  }

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }

  network {
    network_id = hcloud_network.mandari.id
    ip         = "10.0.1.11"
  }

  depends_on = [hcloud_network_subnet.mandari]
}

# =============================================================================
# Load Balancer
# =============================================================================

resource "hcloud_load_balancer" "mandari" {
  name               = "mandari-lb"
  load_balancer_type = var.load_balancer_type
  location           = "fsn1"

  labels = {
    app         = "mandari"
    environment = var.environment
  }
}

resource "hcloud_load_balancer_network" "mandari" {
  load_balancer_id = hcloud_load_balancer.mandari.id
  network_id       = hcloud_network.mandari.id
  ip               = "10.0.1.1"

  depends_on = [hcloud_network_subnet.mandari]
}

# =============================================================================
# Load Balancer Targets
# =============================================================================

resource "hcloud_load_balancer_target" "master" {
  type             = "server"
  load_balancer_id = hcloud_load_balancer.mandari.id
  server_id        = hcloud_server.master.id
  use_private_ip   = true

  depends_on = [hcloud_load_balancer_network.mandari]
}

resource "hcloud_load_balancer_target" "slave" {
  type             = "server"
  load_balancer_id = hcloud_load_balancer.mandari.id
  server_id        = hcloud_server.slave.id
  use_private_ip   = true

  depends_on = [hcloud_load_balancer_network.mandari]
}

# =============================================================================
# Load Balancer Services
# =============================================================================

# HTTP Service (redirects to HTTPS)
resource "hcloud_load_balancer_service" "http" {
  load_balancer_id = hcloud_load_balancer.mandari.id
  protocol         = "http"
  listen_port      = 80
  destination_port = 80

  health_check {
    protocol = "http"
    port     = 80
    interval = 10
    timeout  = 5
    retries  = 3
    http {
      path         = "/health"
      status_codes = ["2??", "3??"]
    }
  }
}

# HTTPS Service with managed certificate
resource "hcloud_load_balancer_service" "https" {
  load_balancer_id = hcloud_load_balancer.mandari.id
  protocol         = "https"
  listen_port      = 443
  destination_port = 80 # TLS termination at LB, forward to Caddy on port 80

  http {
    certificates    = [hcloud_managed_certificate.mandari.id]
    sticky_sessions = true
    cookie_name     = "MANDARI_LB"
    cookie_lifetime = 300
  }

  health_check {
    protocol = "http"
    port     = 80
    interval = 10
    timeout  = 5
    retries  = 3
    http {
      path         = "/health"
      status_codes = ["2??", "3??"]
    }
  }
}

# =============================================================================
# Managed Certificate
# =============================================================================

resource "hcloud_managed_certificate" "mandari" {
  name         = "mandari-cert"
  domain_names = var.domain_names
}

# =============================================================================
# Volumes (for persistent data)
# =============================================================================

resource "hcloud_volume" "master_data" {
  name      = "mandari-master-data"
  size      = var.volume_size
  server_id = hcloud_server.master.id
  automount = true
  format    = "ext4"

  labels = {
    role = "master"
    app  = "mandari"
  }
}

resource "hcloud_volume" "slave_data" {
  name      = "mandari-slave-data"
  size      = var.volume_size
  server_id = hcloud_server.slave.id
  automount = true
  format    = "ext4"

  labels = {
    role = "slave"
    app  = "mandari"
  }
}

# =============================================================================
# Reverse DNS
# =============================================================================

resource "hcloud_rdns" "master_ipv4" {
  server_id  = hcloud_server.master.id
  ip_address = hcloud_server.master.ipv4_address
  dns_ptr    = "master.${var.domain_names[0]}"
}

resource "hcloud_rdns" "slave_ipv4" {
  server_id  = hcloud_server.slave.id
  ip_address = hcloud_server.slave.ipv4_address
  dns_ptr    = "slave.${var.domain_names[0]}"
}
