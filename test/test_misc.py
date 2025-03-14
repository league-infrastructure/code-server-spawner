from cspawn.util.app_support import role_from_email

from cspawn.util.test_fixture import CSUnitTest


class TestMisc(CSUnitTest):
    def test_role_from_email(self):
        test_cases = [
            ("admin", "eric.busboom@jointheleague.org"),
            ("admin", "admin@jointheleague.org"),
            ("admin", "it@jointheleague.org"),
            ("instructor", "john.doe@jointheleague.org"),
            ("instructor", "jane.smith@jointheleague.org"),
            ("instructor", "random.name@jointheleague.org"),
            ("student", "student1@students.jointheleague.org"),
            ("student", "student2@students.jointheleague.org"),
            ("student", "student3@students.jointheleague.org"),
            ("public", "user1@example.com"),
            ("public", "user2@example.com"),
            ("public", "user3@example.com"),
        ]

        for expected_role, email in test_cases:
            with self.subTest(email=email):
                role = role_from_email(self.app.app_config, email)
                self.assertEqual(role, expected_role)
