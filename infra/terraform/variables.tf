variable "region" {
  type    = string
  default = "eu-west-3"
}
variable "name" {
  type    = string
  default = "vidp"
}
variable "instance_type" {
  type    = string
  default = "t3.small" # 2 vCPU / 2 Go : suffisant pour l'ingestion ; t3.medium pour le mode cloud_only
}
variable "arch" {
  type    = string
  default = "amd64" # "arm64" avec une instance Graviton (t4g.*)
}
variable "disk_gb" {
  type    = number
  default = 30
}
variable "ssh_public_key" {
  type    = string
  default = "~/.ssh/id_ed25519.pub"
}
variable "ssh_private_key" {
  type    = string
  default = "~/.ssh/id_ed25519"
}
variable "instance_profile_name" {
  type        = string
  description = "Profil d'instance existant (ex. LabInstanceProfile sur AWS Academy) ; vide = Terraform crée un rôle minimal"
  default     = ""
}
variable "admin_cidr" {
  type        = string
  description = "Votre IP publique /32 pour SSH"
  default     = "0.0.0.0/0" # à restreindre !
}
variable "edge_cidrs" {
  type        = list(string)
  description = "IP publiques des noeuds Edge (ingestion + Prometheus)"
  default     = ["0.0.0.0/0"] # à restreindre le jour de la démo : [\"IP_EDGE/32\"]
}
