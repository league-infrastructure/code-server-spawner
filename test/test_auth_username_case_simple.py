"""
Simple tests for username case sensitivity issues - testing models directly
"""
import pytest
import os
import sys
import tempfile

# Add the project root to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from cspawn.models import User


class TestUsernameNormalization:
    """Test username normalization without full app context"""
    
    def test_clean_username_function(self):
        """Test the User.clean_username static method"""
        # Test basic case normalization
        assert User.clean_username("TestUser") == "testuser"
        assert User.clean_username("TESTUSER") == "testuser"
        assert User.clean_username("testuser") == "testuser"
        
        # Test special characters and spaces
        assert User.clean_username("Test User") == "test-user"
        assert User.clean_username("Test@User") == "test-user"
        assert User.clean_username("Test.User") == "test-user"
        
        # Test edge cases
        assert User.clean_username("Test123") == "test123"
        assert User.clean_username("123Test") == "123test"
    
    def test_username_normalization_consistency(self):
        """Test that different input cases produce the same normalized result"""
        inputs = ["TestUser", "TESTUSER", "testuser", "TestUSER", "TESTuser"]
        normalized_results = [User.clean_username(inp) for inp in inputs]
        
        # All should be the same
        assert len(set(normalized_results)) == 1
        assert normalized_results[0] == "testuser"


if __name__ == "__main__":
    test = TestUsernameNormalization()
    test.test_clean_username_function()
    test.test_username_normalization_consistency()
    print("All tests passed!")