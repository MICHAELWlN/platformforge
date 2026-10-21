# Off by default. Enabling this and applying creates a GitHub OIDC pool and a
# delivery service account. It does not create a cluster, a repository, or a key.
module "github_delivery" {
  count  = var.enable_github_delivery ? 1 : 0
  source = "./modules/github_delivery"

  project_id        = var.project_id
  region            = var.region
  zone              = var.zone
  cluster_name      = var.name_prefix
  repository_id     = "platformforge"
  github_repository = var.github_repository
}
