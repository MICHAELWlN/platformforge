variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "name_prefix" {
  description = "Prefix used to build service-account IDs."
  type        = string
}

variable "workload_k8s_namespace" {
  description = "Namespace of the Kubernetes service account in the Workload Identity binding."
  type        = string
}

variable "workload_k8s_service_account" {
  description = "Kubernetes service account in the Workload Identity binding."
  type        = string
}
