variable "db_password" { type = string }
resource "aws_db_instance" "ok" {
  password = var.db_password
}
