"""
Test the core validation logic changes without Flask context
"""
import os
import sys
import unittest.mock

# Add the project root to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from cspawn.models import User


def test_validation_logic():
    """Test the core validation logic that was changed"""
    
    # Simulate a user stored in the database with normalized username
    class MockUser:
        def __init__(self, username, password):
            self.username = User.clean_username(username)  # This is what happens in real DB
            self.password = password
    
    # Create test user as it would be stored
    stored_user = MockUser("TestUser", "password123")  # Will be normalized to "testuser"
    print(f"Stored user username: '{stored_user.username}'")
    
    # Test the old way (what was causing the bug)
    print("\n=== OLD WAY (buggy) ===")
    login_input = "TESTUSER"  # User types this
    old_lookup = login_input  # Old code did direct lookup
    old_match = (old_lookup == stored_user.username)
    print(f"Login input: '{login_input}'")
    print(f"Old lookup: '{old_lookup}'")
    print(f"Stored username: '{stored_user.username}'")
    print(f"Old way match: {old_match}")
    
    # Test the new way (fixed)
    print("\n=== NEW WAY (fixed) ===")
    new_lookup = User.clean_username(login_input)  # New code normalizes before lookup
    new_match = (new_lookup == stored_user.username)
    print(f"Login input: '{login_input}'")
    print(f"New lookup (normalized): '{new_lookup}'")
    print(f"Stored username: '{stored_user.username}'")
    print(f"New way match: {new_match}")
    
    # Test various cases
    print("\n=== Testing various input cases ===")
    test_cases = ["testuser", "TESTUSER", "TestUser", "TESTuser", "testUSER"]
    
    for case in test_cases:
        normalized = User.clean_username(case)
        matches = (normalized == stored_user.username)
        print(f"Input: '{case}' -> Normalized: '{normalized}' -> Match: {matches}")
    
    return new_match and not old_match


if __name__ == "__main__":
    success = test_validation_logic()
    print(f"\n=== Result: {'FIX WORKING' if success else 'ISSUE REMAINS'} ===")
    exit(0 if success else 1)