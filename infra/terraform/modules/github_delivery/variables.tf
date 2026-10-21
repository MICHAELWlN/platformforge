variable "project_id" {
  description = "GCP project that owns the delivery identity."
  type        = string
}

variable "region" {
  description = "Artifact Registry location. The repository itself is not created here."
  type        = string
}

variable "zone" {
  description = "Zone of the existing GKE cluster."
  type        = string
}

variable "cluster_name" {
  description = "Existing cluster name. This module does not create a cluster."
  type        = string
}

variable "repository_id" {
  description = "Existing Artifact Registry repository id."
  type        = string
}

variable "github_repository" {
  description = "GitHub repository allowed to impersonate the delivery account, as owner/name."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must look like owner/name."
  }
}
