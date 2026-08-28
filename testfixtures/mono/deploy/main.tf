resource "aws_db_instance" "main" {
  identifier = "orders-db"
  password   = "SuperSecret123!"
}
resource "aws_iam_user" "ci" {
  name = "ci"
}
