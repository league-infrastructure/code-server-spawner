"""
Tests to verify the username case sensitivity fix
"""
import os
import sys
import tempfile
import unittest.mock

# Add the project root to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from cspawn.models import User
from cspawn.auth.forms import LoginForm, UPRegistrationForm


class MockUser:
    """Mock user for testing"""
    def __init__(self, username, password):
        self.username = User.clean_username(username)  # Simulate the @validates decorator
        self.password = password


class MockQuery:
    """Mock query for testing"""
    def __init__(self, users):
        self.users = users
    
    def filter_by(self, username=None):
        matching_users = [u for u in self.users if u.username == username] if username else self.users
        return MockFilterResult(matching_users)


class MockFilterResult:
    """Mock filter result for testing"""
    def __init__(self, users):
        self.users = users
    
    def first(self):
        return self.users[0] if self.users else None


def test_login_form_case_insensitive():
    """Test that LoginForm validation works with different username cases"""
    # Create a mock user with normalized username
    test_user = MockUser("TestUser", "password123")  # Will be stored as "testuser"
    
    # Mock the User.query to return our test user
    with unittest.mock.patch('cspawn.auth.forms.User') as mock_user_class:
        mock_user_class.clean_username = User.clean_username
        mock_user_class.query = MockQuery([test_user])
        
        # Test form with different case
        form = LoginForm()
        form.username.data = "TESTUSER"  # Different case
        form.password.data = "password123"
        
        # Test username validation
        try:
            form.validate_username(form.username)
            username_valid = True
        except Exception as e:
            username_valid = False
            print(f"Username validation failed: {e}")
        
        # Test password validation
        try:
            form.validate_password(form.password)
            password_valid = True
        except Exception as e:
            password_valid = False
            print(f"Password validation failed: {e}")
        
        print(f"Username validation: {'PASS' if username_valid else 'FAIL'}")
        print(f"Password validation: {'PASS' if password_valid else 'FAIL'}")
        
        return username_valid and password_valid


def test_registration_form_case_insensitive():
    """Test that UPRegistrationForm validation works with different username cases"""
    # Create a mock user with normalized username
    existing_user = MockUser("TestUser", "password123")  # Will be stored as "testuser"
    
    # Mock the User.query to return our existing user
    with unittest.mock.patch('cspawn.auth.forms.User') as mock_user_class:
        mock_user_class.clean_username = User.clean_username
        mock_user_class.query = MockQuery([existing_user])
        
        # Try to register with same username but different case
        form = UPRegistrationForm()
        form.username.data = "TESTUSER"  # Different case but should conflict
        
        # Test username validation - should fail because user exists
        try:
            form.validate_username(form.username)
            validation_failed = False
        except Exception as e:
            validation_failed = True
            print(f"Username validation correctly failed: {e}")
        
        print(f"Registration conflict detection: {'PASS' if validation_failed else 'FAIL'}")
        
        return validation_failed


def test_normalization_consistency():
    """Test that username normalization is consistent"""
    test_cases = [
        ("TestUser", "testuser"),
        ("TESTUSER", "testuser"), 
        ("testuser", "testuser"),
        ("Test User", "test-user"),
        ("Test@User", "test-user"),
    ]
    
    all_passed = True
    for input_name, expected in test_cases:
        result = User.clean_username(input_name)
        passed = result == expected
        print(f"'{input_name}' -> '{result}' (expected '{expected}'): {'PASS' if passed else 'FAIL'}")
        all_passed = all_passed and passed
    
    return all_passed


if __name__ == "__main__":
    print("=== Testing Username Case Sensitivity Fix ===\n")
    
    print("1. Testing username normalization consistency:")
    test1_passed = test_normalization_consistency()
    
    print("\n2. Testing login form case insensitivity:")
    test2_passed = test_login_form_case_insensitive()
    
    print("\n3. Testing registration form case insensitivity:")
    test3_passed = test_registration_form_case_insensitive()
    
    print(f"\n=== Results ===")
    print(f"Normalization: {'PASS' if test1_passed else 'FAIL'}")
    print(f"Login validation: {'PASS' if test2_passed else 'FAIL'}")
    print(f"Registration validation: {'PASS' if test3_passed else 'FAIL'}")
    
    all_passed = test1_passed and test2_passed and test3_passed
    print(f"\nOverall: {'ALL TESTS PASSED' if all_passed else 'SOME TESTS FAILED'}")
    
    exit(0 if all_passed else 1)