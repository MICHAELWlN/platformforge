terraform {
  required_version = ">= 1.10.0, < 2.0.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.7"
    }
  }

  # Local state until a private, versioned GCS bucket is approved.
  # Switching backends later is a reviewed step (docs/TERRAFORM.md).
  # State files are gitignored. Do not commit them.
  backend "local" {
    path = "terraform.tfstate"
  }
}
