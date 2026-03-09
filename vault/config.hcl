ui = true
disable_mlock = true
log_level = "info"

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = 1
}

storage "raft" {
  path    = "/vault/data"
  node_id = "vault-kmip-1"
}

api_addr     = "http://vault:8200"
cluster_addr = "http://vault:8201"
