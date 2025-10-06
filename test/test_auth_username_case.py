"""
Tests for username case sensitivity issues in authentication
"""
import pytest
from cspawn.models import User, Class, db
from cspawn.auth.forms import LoginForm


class TestUsernameCase:
    """Test cases for username case sensitivity handling"""
    
    def test_username_normalization_on_creation(self, app, temp_db):
        """Test that usernames are normalized when creating users"""
        with app.app_context():
            # Create a test class
            test_class = Class(
                class_code="TEST123",
                name="Test Class",
                can_register=True
            )
            temp_db.session.add(test_class)
            temp_db.session.commit()
            
            # Create a user with mixed case username
            user = User(
                username="TestUser",
                user_id="test_user_1",
                password="password123",
                is_student=True
            )
            temp_db.session.add(user)
            temp_db.session.commit()
            
            # Verify that the username was normalized (slugified)
            saved_user = User.query.filter_by(user_id="test_user_1").first()
            assert saved_user is not None
            # slugify should convert to lowercase and handle special chars
            assert saved_user.username == "testuser"
    
    def test_login_form_validation_case_insensitive(self, app, temp_db):
        """Test that login form validation works with different cases"""
        with app.app_context():
            # Create a test class
            test_class = Class(
                class_code="TEST123",
                name="Test Class",
                can_register=True
            )
            temp_db.session.add(test_class)
            temp_db.session.commit()
            
            # Create a user (username will be normalized)
            user = User(
                username="TestUser",  # Will be normalized to "testuser"
                user_id="test_user_1",
                password="password123",
                is_student=True
            )
            temp_db.session.add(user)
            temp_db.session.commit()
            
            # Test form validation with different cases
            form_data = {
                'username': 'TESTUSER',  # Different case
                'password': 'password123',
                'csrf_token': 'test_token'
            }
            
            with app.test_request_context('/', method='POST', data=form_data):
                form = LoginForm()
                form.csrf_token.data = 'test_token'  # Bypass CSRF for testing
                form.username.data = 'TESTUSER'
                form.password.data = 'password123'
                
                # This should not raise ValidationError since user exists (after normalization)
                try:
                    form.validate_username(form.username)
                    username_valid = True
                except Exception as e:
                    username_valid = False
                    print(f"Username validation failed: {e}")
                
                try:
                    form.validate_password(form.password)
                    password_valid = True
                except Exception as e:
                    password_valid = False
                    print(f"Password validation failed: {e}")
                
                # These should pass if case normalization is working
                assert username_valid, "Username validation should pass with different case"
                assert password_valid, "Password validation should pass when username case differs"
    
    def test_user_lookup_case_insensitive(self, app, temp_db):
        """Test that user lookups work with different username cases"""
        with app.app_context():
            # Create a user (username will be normalized)
            user = User(
                username="TestUser",  # Will be normalized to "testuser"
                user_id="test_user_1",
                password="password123",
                is_student=True
            )
            temp_db.session.add(user)
            temp_db.session.commit()
            
            # Try to find user with different cases
            user1 = User.query.filter_by(username=User.clean_username("testuser")).first()
            user2 = User.query.filter_by(username=User.clean_username("TESTUSER")).first()
            user3 = User.query.filter_by(username=User.clean_username("TestUser")).first()
            
            assert user1 is not None
            assert user2 is not None  
            assert user3 is not None
            assert user1.id == user2.id == user3.id