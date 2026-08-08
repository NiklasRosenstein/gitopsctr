terraform {
  required_version = ">= 1.8.0"

  required_providers {
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 4.0"
    }
  }

  backend "local" {}
}

provider "docker" {}

variable "container_name" {
  type = string
}

variable "host_port" {
  type = number
}

variable "image" {
  type = string
}

resource "docker_image" "application" {
  name         = var.image
  keep_locally = true
}

resource "docker_container" "application" {
  name    = var.container_name
  image   = docker_image.application.image_id
  restart = "unless-stopped"

  ports {
    internal = 8080
    external = var.host_port
    ip       = "127.0.0.1"
  }
}

output "container_id" {
  value = docker_container.application.id
}

output "url" {
  value = "http://127.0.0.1:${var.host_port}"
}
